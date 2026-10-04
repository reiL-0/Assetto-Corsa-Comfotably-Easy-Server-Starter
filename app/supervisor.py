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
import os
import shlex
import signal
import time
from collections import deque
from pathlib import Path

from app import metrics
from app.config import settings
from app.live import acsp

IDLE_POLL = 15.0  # seconds between idle checks
SAMPLE_EVERY = 60.0  # how often the number of players on track is recorded
WATCH_EVERY = 0.25  # how often the process (and the log file) is looked at
STOP_GRACE = 10.0  # SIGTERM -> SIGKILL


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
                 started_at: float | None = None, log_from: int = 0) -> None:
        self.server_id = server_id
        self.cwd = cwd
        self.proc = proc  # only for a process this manager spawned; an adopted one is known by its pid alone
        self.pid = proc.pid if proc else pid
        self.started_at = started_at or time.time()
        self.log: deque[str] = deque(maxlen=settings.log_lines)
        self.acsp: acsp.ACSPClient | None = None
        self.exit_code: int | None = None
        self._stopping = False
        self._log_pos = log_from
        self._reader = asyncio.create_task(self._watch())
        self._idle = asyncio.create_task(self._idle_watch()) if settings.idle_stop_seconds else None
        self._sampler = asyncio.create_task(self._sample_online())

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
        for task in (self._idle, self._sampler):
            if task and task is not asyncio.current_task():
                task.cancel()
        if self.acsp:
            self.acsp.close()


# ponytail: in-memory registry, single process. After a manager restart `adopt` rebuilds it from the pid files.
_instances: dict[int, Instance] = {}


async def start(
    server_id: int,
    cwd: Path,
    *,
    acsp_local_port: int | None = None,
    acsp_remote_port: int | None = None,
    acsp_host: str = "127.0.0.1",
) -> Instance:
    current = _instances.get(server_id)
    if current and current.running:
        raise RuntimeError("already running")
    if not settings.acserver_cmd:
        raise RuntimeError("ACM_ACSERVER_CMD is not configured")
    log_path = cwd / "server.log"
    if log_path.exists():
        log_path.replace(cwd / "server.log.1")  # the previous run stays readable for one more start
    with open(log_path, "wb") as out:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(settings.acserver_cmd),
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,  # its own session: a signal or exit of the manager does not reach it
        )
    inst = Instance(server_id, cwd, proc=proc)
    (cwd / "server.pid").write_text(json.dumps({"pid": proc.pid, "started_at": inst.started_at}))
    if acsp_local_port and acsp_remote_port:
        inst.acsp = await acsp.connect(server_id, acsp_remote_port, acsp_local_port, acsp_host)
    _instances[server_id] = inst
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
    inst = Instance(server_id, cwd, pid=pid, started_at=info.get("started_at"), log_from=max(0, log_size - 16384))
    if acsp_local_port and acsp_remote_port:
        inst.acsp = await acsp.connect(server_id, acsp_remote_port, acsp_local_port, acsp_host, car_slots=car_slots)
    _instances[server_id] = inst
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
