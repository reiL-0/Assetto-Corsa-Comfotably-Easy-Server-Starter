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
import signal
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


def _is_our_acserver(pid: int, cwd: Path) -> bool:
    """The pid is an acServer running in this instance directory (guards against a reused pid)."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        want = shlex.split(settings.acserver_cmd)
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve() == cwd.resolve() and os.path.basename(argv[0].decode()) == os.path.basename(want[0])
    except (OSError, IndexError, ValueError):
        return False


class Instance:
    def __init__(self, server_id: int, cwd: Path, *, proc: asyncio.subprocess.Process | None = None, pid: int | None = None,
                 started_at: float | None = None, log_from: int = 0, http_port: int | None = None) -> None:
        self.server_id = server_id
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
            metrics.log(self.server_id, "server_crash", value=self.exit_code, name=f"up {int(self.uptime)}s")
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
                metrics.log(self.server_id, "online", value=sum(1 for d in self.acsp.board.drivers if d.connected))

    async def _idle_watch(self) -> None:
        """Stop the process once ACSP has shown no connected cars for idle_stop_seconds."""
        last_active = time.time()
        while self.running:
            await asyncio.sleep(IDLE_POLL)
            # no ACSP socket = can't tell, so never counts as idle
            if not self.acsp or self.acsp.cars:
                last_active = time.time()
            elif time.time() - last_active > settings.idle_stop_seconds:
                await self.stop(reason="idle")

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
            metrics.log(self.server_id, "server_stop", name=reason, value=self.uptime)
            os.kill(self.pid, signal.SIGTERM)
            deadline = time.time() + timeout
            while self.running and time.time() < deadline:
                await asyncio.sleep(0.1)
            if self.running:
                os.kill(self.pid, signal.SIGKILL)
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
before_start: list = []   # called with the server id right before an acServer is spawned (app/wake.py frees the ports it holds for it)


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


async def start(
    server_id: int,
    cwd: Path,
    *,
    cpu_percent: int | None = None,
    mem_mb: int | None = None,
    cmd: str | None = None,   # argv string of this server's acServer version (app/binaries.py); None = ACM_ACSERVER_CMD
    acsp_local_port: int | None = None,
    acsp_remote_port: int | None = None,
    acsp_host: str = "127.0.0.1",
    http_port: int | None = None,
) -> Instance:
    current = _instances.get(server_id)
    if current and current.running:
        raise RuntimeError("already running")
    if not (cmd or settings.acserver_cmd):
        raise RuntimeError("ACM_ACSERVER_CMD is not configured")
    for hook in before_start:
        hook(server_id)
    if before_start:
        await asyncio.sleep(0.1)   # (closing a UDP endpoint takes effect on the next loop iteration)
    log_path = cwd / "server.log"
    if log_path.exists():
        log_path.replace(cwd / "server.log.1")  # the previous run stays readable for one more start
    with open(log_path, "wb") as out:
        proc = await asyncio.create_subprocess_exec(
            *limit_prefix(server_id, cpu_percent, mem_mb), *shlex.split(cmd or settings.acserver_cmd),
            cwd=cwd,
            env={**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"},   # (systemd-run --user finds its bus there)
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,  # its own session: a signal or exit of the manager does not reach it
        )
    inst = Instance(server_id, cwd, proc=proc, http_port=http_port)
    (cwd / "server.pid").write_text(json.dumps({"pid": proc.pid, "started_at": inst.started_at}))
    _instances[server_id] = inst   # registered before the plugin socket: a failure there must not leave a running acServer nobody owns
    if acsp_local_port and acsp_remote_port:
        await inst.connect_acsp(acsp_remote_port, acsp_local_port, acsp_host)
    metrics.log(server_id, "server_start")
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
) -> Instance | None:
    """Take back an acServer left running by a previous manager process. None when there is nothing to take back."""
    try:
        info = json.loads((cwd / "server.pid").read_text())
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not settings.acserver_cmd or not _alive(pid) or not _is_our_acserver(pid, cwd):
        (cwd / "server.pid").unlink(missing_ok=True)  # stale: the process is gone (or the pid belongs to something else now)
        return None
    log_size = (cwd / "server.log").stat().st_size if (cwd / "server.log").exists() else 0
    inst = Instance(server_id, cwd, pid=pid, started_at=info.get("started_at"), log_from=max(0, log_size - 16384), http_port=http_port)
    _instances[server_id] = inst
    if acsp_local_port and acsp_remote_port:
        await inst.connect_acsp(acsp_remote_port, acsp_local_port, acsp_host, car_slots)
    metrics.log(server_id, "server_adopted", name=f"pid {pid}")
    return inst


async def stop(server_id: int) -> None:
    inst = _instances.get(server_id)
    if inst:
        await inst.stop()


def live() -> list[Instance]:
    """Running instances with a connected ACSP plugin socket."""
    return [i for i in _instances.values() if i.running and i.acsp]


def get(server_id: int) -> Instance | None:
    return _instances.get(server_id)
