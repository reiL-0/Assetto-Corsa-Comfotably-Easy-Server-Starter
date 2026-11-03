"""Spawn and supervise acServer processes. One process per server id.

The game servers must outlive the manager: a manager restart (deploy, crash, `systemctl restart acm`) must not drop the
people who are racing. So each acServer is started in its own session with its output going to `server.log` in its
instance directory (never a pipe: a pipe closed by a dying manager would kill the server on its next write), and
`server.pid` records who it is. On boot `adopt` takes those still-running processes back: it checks the pid really is
this instance's acServer, tails the log again, reconnects the ACSP plugin socket and asks acServer who is connected.
(systemd must not kill them either: the service uses KillMode=process.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

from app import integrity, metrics
from app.config import settings
from app.live import acsp
from app.live.logboard import LogBoard

log = logging.getLogger("acmanager.supervisor")

IDLE_POLL = 15.0  # seconds between idle checks
SAMPLE_EVERY = 60.0  # how often the number of players on track is recorded
WATCH_EVERY = 0.25  # how often the process (and the log file) is looked at
STOP_GRACE = 10.0  # SIGTERM -> SIGKILL
INFO_EVERY = 60.0  # how often the server's own /INFO answer is copied to info.json (what app/wake.py shows while it is stopped)


def _fetch_info(http_port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/INFO", timeout=2) as r:
        return r.read().decode()


def _alive(pid: int) -> bool:
    """True for a live process; a zombie (finished, not yet waited for) is not alive."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


HELPER_TIMEOUT = 5.0   # seconds the helper that signals another user's acServer may take (see _signal)
START_TOLERANCE = 5.0   # seconds: how far a process' start time may be from the one `server.pid` recorded and still be that process


def _proc_started(pid: int) -> float:
    """Unix time the process started (boot time + the start tick of /proc/<pid>/stat)."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    btime = next((int(line.split()[1]) for line in open("/proc/stat") if line.startswith("btime")), None)
    if btime is None:
        raise ValueError("no btime in /proc/stat")
    return btime + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def _is_our_acserver(pid: int, cwd: Path, started_at: float | None = None) -> bool:
    """The pid is an acServer running in this instance directory (guards against a reused pid).
    The name of the binary and the owner must fit: the manager's own user, or `settings.acserver_uid` (a server started before that was switched on still runs as the manager's user and
    must still be taken back, or it would be left running with people in it and no owner). The directory it runs in is compared when the kernel lets us read it; it does not for another
    user's process (that is the point of `acserver_uid`), and then the start time recorded in `server.pid` stands in for it."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        want = shlex.split(settings.acserver_cmd)
        if os.path.basename(argv[0].decode()) != os.path.basename(want[0]):
            return False
        if os.stat(f"/proc/{pid}").st_uid not in {os.geteuid(), settings.acserver_uid}:
            return False
        try:
            return Path(os.readlink(f"/proc/{pid}/cwd")).resolve() == cwd.resolve()
        except PermissionError:
            return isinstance(started_at, (int, float)) and abs(_proc_started(pid) - started_at) < START_TOLERANCE
    except (OSError, IndexError, ValueError):
        return False


def protect_instance(cwd: Path) -> None:
    """With `acserver_uid` the acServer is another user that shares the instance folder (group-writable: it writes results there). Two things keep it from rewriting what the manager
    keeps there: the sticky bit on the folder (nobody but the owner of a file may rename or delete it, so server.pid cannot be swapped) and server.pid itself read-only for the group
    (seen on a real machine: the binary overwrote it, and `adopt` could no longer take the server back). Nothing changes without `acserver_uid`.
    It runs BEFORE the process is spawned and makes the file if it is not there: the pid is written after the spawn, and a file that was ever group-writable while the acServer was
    already running could be held open by it. If the folder is not ours (the manager's user was changed: chown the instances), it says so and goes on: the protection is for the
    server's own recovery, and a service that does not start is worse."""
    if not settings.acserver_uid:
        return
    try:
        os.chmod(cwd, 0o3770)
        pid = cwd / "server.pid"
        if not pid.exists():
            pid.touch(mode=0o640)
        os.chmod(pid, 0o640)
    except PermissionError:
        log.warning("cannot protect %s: it is not ours (after a change of the manager's user, chown the instances); server.pid stays writable for the acServer's group", cwd)


def child_env() -> dict[str, str]:
    """The environment acServer starts with: the manager's, without the manager's own settings. Every setting is an `ACM_*` variable (`OPR_*` the website's), and the secrets are among them:
    the binary is the game's, or a client's, and has no business reading them (nor does `/proc/<manager>/environ` stay the only place they are hidden)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ACM_", "OPR_"))}
    env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"   # (systemd-run --user finds its bus there)
    return env


def spawn_prefix() -> list[str]:
    """`setpriv` words that run acServer as `settings.acserver_uid` (and the group of the same number), no supplementary groups, with no capabilities and unable to gain any. [] when unset.
    The capabilities need explicit care: a process that changes from one non-root user to another keeps its permitted and ambient sets, so without `--inh-caps=-all --ambient-caps=-all`
    the child would still hold the CAP_SETUID the manager needs to make this very switch, and could become the manager's user again. `setpriv` execs in place: same pid, same argv[0]."""
    uid = settings.acserver_uid
    if not uid:
        return []
    if not shutil.which("setpriv"):
        raise RuntimeError("ACM_ACSERVER_UID is set but `setpriv` (util-linux) is not installed")
    return ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "--"]


async def _signal(pid: int, sig: int) -> None:
    """Send a signal to an acServer. kill(2) needs the sender to be the target's user or to hold CAP_KILL. A manager that holds CAP_KILL (recommended for hosting: `AmbientCapabilities=CAP_SETUID CAP_SETGID
    CAP_KILL`) signals directly, so stopping a server never depends on a process of the client's user, which the client's binary could freeze. Without it the signal is sent by a short-lived process
    that has become that user, with the CAP_SETUID the manager already needs to start it: a minimal environment (it runs as the client's user: no secrets), and a limit, because the binary can stop
    that helper (SIGSTOP) and then `stop()` would wait for ever. Raises ProcessLookupError like os.kill; TimeoutError when the helper is frozen."""
    try:
        os.kill(pid, sig)
        return
    except PermissionError:
        pass
    try:
        owner = os.stat(f"/proc/{pid}").st_uid
    except FileNotFoundError:
        raise ProcessLookupError(pid) from None
    if owner != settings.acserver_uid:
        raise PermissionError(f"pid {pid} belongs to uid {owner}: neither the manager's nor ACM_ACSERVER_UID")
    p = await asyncio.create_subprocess_exec("setpriv", f"--reuid={owner}", f"--regid={owner}", "--clear-groups", "--inh-caps=-all", "--ambient-caps=-all", "--",
                                             "kill", f"-{int(sig)}", str(pid), env={"PATH": os.environ.get("PATH") or "/usr/sbin:/usr/bin:/sbin:/bin"},
                                             stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        failed = await asyncio.wait_for(p.wait(), HELPER_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError(f"the helper that signals pid {pid} did not finish in {HELPER_TIMEOUT:g} s (frozen by the server? give the manager CAP_KILL)") from None
    if failed:
        raise ProcessLookupError(pid)


class Instance:
    def __init__(self, server_id: int, cwd: Path, *, proc: asyncio.subprocess.Process | None = None, pid: int | None = None,
                 started_at: float | None = None, log_from: int = 0, http_port: int | None = None,
                 nascar_config: dict | None = None) -> None:
        self.server_id = server_id
        self.nascar_config = nascar_config  # snapshot of Server.session.nascar; replaced on apply, read by ACSP without I/O
        self.pace_steam_id: str | None = None   # Server.session.pace_steam_id until pace_car_check names him (then None); set on start / apply / adopt
        self.cwd = cwd
        self.proc = proc  # only for a process this manager spawned; an adopted one is known by its pid alone
        self.pid = proc.pid if proc else pid
        self.started_at = started_at or time.time()
        self.log: deque[str] = deque(maxlen=settings.log_lines)
        self.acsp: acsp.ACSPClient | None = None
        self.director = None   # app.live.cspweather.WeatherDirector while the server has a weather plan
        self.logboard = LogBoard()   # the leaderboard read from the log: what the site shows while there is no ACSP socket
        self._acsp_retry: asyncio.Task | None = None
        self.exit_code: int | None = None
        self.http_port = http_port
        self._stopping = False
        self._log_pos = log_from
        self._reader = asyncio.create_task(self._watch())
        self._idle = asyncio.create_task(self._idle_watch()) if settings.idle_stop_seconds else None
        self._sampler = asyncio.create_task(self._sample_online())
        self._info = asyncio.create_task(self._info_loop()) if http_port else None

    async def _snapshot_info(self) -> None:
        """Keep the server's own /INFO answer on disk: while it is stopped the manager answers players' lobby queries with it."""
        try:
            raw = await asyncio.to_thread(_fetch_info, self.http_port)
            json.loads(raw)
            (self.cwd / "info.json").write_text(raw)
        except (OSError, ValueError):
            pass

    async def _info_loop(self) -> None:
        await asyncio.sleep(10)   # acServer takes a few seconds to open its HTTP port
        while self.running:
            await self._snapshot_info()
            await asyncio.sleep(INFO_EVERY)

    @property
    def log_path(self) -> Path:
        return self.cwd / "server.log"

    @property
    def pid_path(self) -> Path:
        return self.cwd / "server.pid"

    def _tail(self) -> None:
        """New complete lines of server.log into the ring buffer."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._log_pos)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1
        self._log_pos += end
        for line in data[:end].decode(errors="replace").splitlines():
            self.log.append(line)
            integrity.on_log_line(self.server_id, line)
            self.logboard.feed(line)

    async def _watch(self) -> None:
        """Follow the log, and notice when the process ends on its own (not through `stop`)."""
        while True:
            self._tail()
            if not self.running:
                break
            await asyncio.sleep(WATCH_EVERY)
        self._tail()
        if self.proc:
            self.exit_code = self.proc.returncode
        if not self._stopping and self.exit_code not in (0, -15):  # ended on its own and not by a stop/terminate
            metrics.enqueue(self.server_id, "server_crash", value=self.exit_code, name=f"up {int(self.uptime)}s")
        self.pid_path.unlink(missing_ok=True)
        if self.director:
            self.director.stop()
        if self.acsp:   # its socket would still hold our local port and the next start could not bind it
            self.acsp.close()
        if self._acsp_retry:
            self._acsp_retry.cancel()

    async def _sample_online(self) -> None:
        """One sample a minute of how many are on track: the series behind peak / player-minutes."""
        while self.running:
            await asyncio.sleep(SAMPLE_EVERY)
            if self.running and self.acsp:
                metrics.enqueue(self.server_id, "online", value=sum(1 for d in self.acsp.board.drivers if d.connected))

    async def _idle_watch(self) -> None:
        """Stop the process once ACSP has shown no connected cars for idle_stop_seconds."""
        last_active = time.time()
        while self.running:
            await asyncio.sleep(IDLE_POLL)
            # no ACSP socket = can't tell, so never counts as idle
            if not self.acsp or self.acsp.cars:
                last_active = time.time()
            elif time.time() - last_active > settings.idle_stop_seconds:
                await stop(self.server_id, reason="idle", only=self)   # through the per-server lock, and only while this is still the registered instance
                notify_stopped(self.server_id)
                return

    @property
    def running(self) -> bool:
        if self.proc:
            return self.proc.returncode is None
        return self.pid is not None and _alive(self.pid)

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    async def stop(self, timeout: float = STOP_GRACE, reason: str = "manual") -> None:
        if self.running:
            self._stopping = True
            if self.http_port:
                await self._snapshot_info()
            metrics.enqueue(self.server_id, "server_stop", name=reason, value=self.uptime)
            await _signal(self.pid, signal.SIGTERM)
            deadline = time.time() + timeout
            while self.running and time.time() < deadline:
                await asyncio.sleep(0.1)
            if self.running:
                await _signal(self.pid, signal.SIGKILL)
                while self.running:
                    await asyncio.sleep(0.1)
        self.pid_path.unlink(missing_ok=True)
        self._reader.cancel()
        self._tail()
        for task in (self._idle, self._sampler, self._info):
            if task and task is not asyncio.current_task():
                task.cancel()
        if self.acsp:
            self.acsp.close()
        if self._acsp_retry:
            self._acsp_retry.cancel()

    def set_weather_plan(self, plan: dict | None, server_cfg: dict | None = None) -> None:
        """Start (or replace, or with None stop) the weather plan played to CSP clients on this running server."""
        from app.live.cspweather import WeatherDirector   # (late: it imports acsp, like this module)
        if self.director:
            self.director.stop()
        self.director = WeatherDirector(self, plan, server_cfg) if plan and self.running else None

    async def connect_acsp(self, remote_port: int, local_port: int, host: str, car_slots: int = 0) -> None:
        """Open our side of the plugin socket. If the port is not free (a socket of an earlier run closing) try a few times, then keep
        trying in the background: the server runs either way (the site falls back to the log meanwhile) and must never be left unowned."""
        for attempt in range(3):
            try:
                self.acsp = await acsp.connect(self.server_id, remote_port, local_port, host, car_slots=car_slots)
                return
            except OSError as e:
                log.warning("ACSP socket for server %s not ready (%s), try %s", self.server_id, e, attempt + 1)
                await asyncio.sleep(1)
        self._acsp_retry = asyncio.create_task(self._retry_acsp(remote_port, local_port, host, car_slots))

    async def _retry_acsp(self, remote_port: int, local_port: int, host: str, car_slots: int) -> None:
        while self.running and not self.acsp:
            await asyncio.sleep(5)
            try:
                self.acsp = await acsp.connect(self.server_id, remote_port, local_port, host, car_slots=car_slots)
                log.info("ACSP socket for server %s connected after retrying", self.server_id)
            except OSError:
                continue


# ponytail: in-memory registry, single process. After a manager restart `adopt` rebuilds it from the pid files.
_instances: dict[int, Instance] = {}
_locks: dict[int, asyncio.Lock] = {}   # one per server id: start / stop of the same server never overlap (API, scheduler and wake all come through here)
before_start: list = []   # called with the server id right before an acServer is spawned (app/wake.py frees the ports it holds for it)


def _lock(server_id: int) -> asyncio.Lock:
    return _locks.setdefault(server_id, asyncio.Lock())


def limit_prefix(server_id: int, cpu_percent: int | None, mem_mb: int | None) -> list[str]:
    """`systemd-run --scope` words that put acServer in its own cgroup with these limits (the kernel enforces them, not us). The scope execs acServer
    in place, so the pid, `server.pid` and `adopt` work unchanged. [] when there is nothing to limit or `settings.limits_scope` is off."""
    if not (cpu_percent or mem_mb) or settings.limits_scope not in ("user", "system"):
        return []
    p = ["systemd-run", "--scope", "--quiet", "--collect", f"--unit=acserver-{server_id}"] + (["--user"] if settings.limits_scope == "user" else [])
    if cpu_percent:
        p += ["-p", f"CPUQuota={cpu_percent}%"]
    if mem_mb:
        p += ["-p", f"MemoryMax={mem_mb}M", "-p", "MemorySwapMax=0"]
    return p


async def start(server_id: int, cwd: Path, **kw) -> Instance:
    """Spawn acServer. Serialised per server: the "already running?" check and the registration of the new instance are
    separated by awaits, so without the lock two simultaneous starts would both pass the check and spawn two processes."""
    async with _lock(server_id):
        return await _start(server_id, cwd, **kw)


async def _start(
    server_id: int,
    cwd: Path,
    *,
    cpu_percent: int | None = None,
    mem_mb: int | None = None,
    acsp_local_port: int | None = None,
    acsp_remote_port: int | None = None,
    acsp_host: str = "127.0.0.1",
    http_port: int | None = None,
    nascar_config: dict | None = None,
) -> Instance:
    current = _instances.get(server_id)
    if current and current.running:
        raise RuntimeError("already running")
    if not settings.acserver_cmd:
        raise RuntimeError("ACM_ACSERVER_CMD is not configured")
    if program_lock.locked():
        raise RuntimeError("the acServer program is being installed: try again in a moment")
    for hook in before_start:
        hook(server_id)
    if before_start:
        await asyncio.sleep(0.1)   # (closing a UDP endpoint takes effect on the next loop iteration)
    protect_instance(cwd)
    log_path = cwd / "server.log"
    if log_path.exists():
        log_path.replace(cwd / "server.log.1")  # the previous run stays readable for one more start
    with open(log_path, "wb") as out:
        proc = await asyncio.create_subprocess_exec(
            *limit_prefix(server_id, cpu_percent, mem_mb), *spawn_prefix(), *shlex.split(settings.acserver_cmd),
            cwd=cwd,
            env=child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,  # its own session: a signal or exit of the manager does not reach it
        )
    inst = Instance(server_id, cwd, proc=proc, http_port=http_port, nascar_config=nascar_config)
    record = {"pid": proc.pid, "started_at": inst.started_at}   # (started_at is taken once the spawn returns: a stalled event loop would move it; proc_started is the process' own, from /proc)
    try:
        record["proc_started"] = _proc_started(proc.pid)
    except (OSError, IndexError, ValueError):
        pass
    (cwd / "server.pid").write_text(json.dumps(record))   # (the file exists and is 0640 since protect_instance, before the spawn)
    _instances[server_id] = inst   # registered before the plugin socket: a failure there must not leave a running acServer nobody owns
    if acsp_local_port and acsp_remote_port:
        await inst.connect_acsp(acsp_remote_port, acsp_local_port, acsp_host)
    metrics.enqueue(server_id, "server_start")
    return inst


async def adopt(
    server_id: int,
    cwd: Path,
    *,
    acsp_local_port: int | None = None,
    acsp_remote_port: int | None = None,
    car_slots: int = 0,
    acsp_host: str = "127.0.0.1",
    http_port: int | None = None,
    nascar_config: dict | None = None,
) -> Instance | None:
    """Take back an acServer left running by a previous manager process. None when there is nothing to take back."""
    try:
        info = json.loads((cwd / "server.pid").read_text())
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not settings.acserver_cmd or not _alive(pid) or not _is_our_acserver(pid, cwd, info.get("proc_started", info.get("started_at"))):
        (cwd / "server.pid").unlink(missing_ok=True)  # stale: the process is gone (or the pid belongs to something else now)
        return None
    log_size = (cwd / "server.log").stat().st_size if (cwd / "server.log").exists() else 0
    inst = Instance(server_id, cwd, pid=pid, started_at=info.get("started_at"), log_from=max(0, log_size - 16384), http_port=http_port,
                    nascar_config=nascar_config)
    _instances[server_id] = inst
    if acsp_local_port and acsp_remote_port:
        await inst.connect_acsp(acsp_remote_port, acsp_local_port, acsp_host, car_slots)
    metrics.enqueue(server_id, "server_adopted", name=f"pid {pid}")
    return inst


async def stop(server_id: int, reason: str = "manual", only: Instance | None = None) -> None:
    """`only`: stop the server only if that exact instance is still the registered one, checked once the lock is ours (a restart may
    have registered a new instance while we waited: an automatic stop aimed at the old one must not hit it)."""
    async with _lock(server_id):
        inst = _instances.get(server_id)
        if inst and (only is None or inst is only):
            await inst.stop(reason=reason)


stopped_hooks: list = []   # called with the server id when a SESSION is stopped (the director's stop, the end of an event, the idle stop): what belonged to the session goes with it (app/racecontrol.py: NASCAR arming)


def notify_stopped(server_id: int) -> None:
    for hook in stopped_hooks:
        try:
            hook(server_id)
        except Exception:   # noqa: BLE001 - a hook must never keep a stop from finishing
            log.exception("stopped hook failed for server %s", server_id)


program_lock = threading.Lock()   # held by app/binary.py while it replaces the acServer program: a start waits for it (the replacement itself is one atomic rename, so the worst a start that slips in sees is the old or the new complete file)


def any_running() -> bool:
    """Whether any acServer of this manager is running (app/binary.py refuses to replace the program under a race)."""
    return any(i.running for i in _instances.values())


def live() -> list[Instance]:
    """Running instances with a connected ACSP plugin socket."""
    return [i for i in _instances.values() if i.running and i.acsp]


def get(server_id: int) -> Instance | None:
    return _instances.get(server_id)
