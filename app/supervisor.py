"""Spawn and supervise acServer processes. One process per server id."""

from __future__ import annotations

import asyncio
import shlex
import time
from collections import deque
from pathlib import Path

from app import acsp
from app.config import settings

IDLE_POLL = 15.0  # seconds between idle checks


class Instance:
    def __init__(self, server_id: int, proc: asyncio.subprocess.Process) -> None:
        self.server_id = server_id
        self.proc = proc
        self.started_at = time.time()
        self.log: deque[str] = deque(maxlen=settings.log_lines)
        self.acsp: acsp.ACSPClient | None = None
        self._reader = asyncio.create_task(self._drain())
        self._idle = asyncio.create_task(self._idle_watch()) if settings.idle_stop_seconds else None

    async def _drain(self) -> None:
        assert self.proc.stdout is not None
        async for raw in self.proc.stdout:
            self.log.append(raw.decode(errors="replace").rstrip("\n"))

    async def _idle_watch(self) -> None:
        """Stop the process once ACSP has shown no connected cars for idle_stop_seconds."""
        last_active = time.time()
        while self.running:
            await asyncio.sleep(IDLE_POLL)
            # no ACSP socket = can't tell, so never counts as idle
            if not self.acsp or self.acsp.cars:
                last_active = time.time()
            elif time.time() - last_active > settings.idle_stop_seconds:
                await self.stop()

    @property
    def running(self) -> bool:
        return self.proc.returncode is None

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    async def stop(self, timeout: float = 10.0) -> None:
        if self.running:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self._reader.cancel()
        if self._idle and self._idle is not asyncio.current_task():
            self._idle.cancel()
        if self.acsp:
            self.acsp.close()


# ponytail: in-memory registry, single process. Lost on manager restart;
# fine until there's a reason to run multiple workers or reattach on boot.
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
    proc = await asyncio.create_subprocess_exec(
        *shlex.split(settings.acserver_cmd),
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    inst = Instance(server_id, proc)
    if acsp_local_port and acsp_remote_port:
        inst.acsp = await acsp.connect(server_id, acsp_remote_port, acsp_local_port, acsp_host)
    _instances[server_id] = inst
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
