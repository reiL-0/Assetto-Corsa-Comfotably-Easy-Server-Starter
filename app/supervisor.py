"""Spawn and supervise acServer processes. One process per server id."""

from __future__ import annotations

import asyncio
import shlex
import time
from collections import deque
from pathlib import Path

from app.config import settings


class Instance:
    def __init__(self, server_id: int, proc: asyncio.subprocess.Process) -> None:
        self.server_id = server_id
        self.proc = proc
        self.started_at = time.time()
        self.log: deque[str] = deque(maxlen=settings.log_lines)
        self._reader = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        assert self.proc.stdout is not None
        async for raw in self.proc.stdout:
            self.log.append(raw.decode(errors="replace").rstrip("\n"))

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


# ponytail: in-memory registry, single process. Lost on manager restart;
# fine until there's a reason to run multiple workers or reattach on boot.
_instances: dict[int, Instance] = {}


async def start(server_id: int, cwd: Path) -> Instance:
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
    _instances[server_id] = inst
    return inst


async def stop(server_id: int) -> None:
    inst = _instances.get(server_id)
    if inst:
        await inst.stop()


def get(server_id: int) -> Instance | None:
    return _instances.get(server_id)
