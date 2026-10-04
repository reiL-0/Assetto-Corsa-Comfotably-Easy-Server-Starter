"""Wake a stopped server when a player tries to join, but only inside an event window (see schedule.open_window).

While a server is stopped and its window is open, the manager itself listens on the server's game port (UDP and TCP, the
same number) instead of acServer. The first datagram or connection closes those listeners, starts the server
(`schedule.wake`) and leaves the port to acServer; that first attempt gets no answer, the player tries again a few
seconds later. Outside a window nothing listens, so a port scan cannot start anything. Inside one it is limited to
MAX_PER_HOUR wakes and COOLDOWN seconds between two.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlmodel import Session, select

from app import schedule, supervisor
from app.db import engine
from app.models import Server
from app.servers import _ports

log = logging.getLogger("acmanager.wake")
SYNC_EVERY = 5.0  # seconds between looks at which servers should be listened for
COOLDOWN = 30.0
MAX_PER_HOUR = 3
HOST = "0.0.0.0"  # tests aim it at loopback


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, hit) -> None:
        self.hit = hit

    def datagram_received(self, data: bytes, addr) -> None:
        self.hit()


class Waker:
    def __init__(self) -> None:
        self.listening: dict[int, tuple[asyncio.DatagramTransport, asyncio.AbstractServer]] = {}
        self.woken: dict[int, list[float]] = {}
        self._busy: set[int] = set()

    def _allowed(self, server_id: int, now: float) -> bool:
        recent = [t for t in self.woken.get(server_id, []) if now - t < 3600]
        self.woken[server_id] = recent
        return len(recent) < MAX_PER_HOUR and (not recent or now - recent[-1] >= COOLDOWN)

    async def _bind(self, server_id: int, port: int) -> None:
        loop = asyncio.get_running_loop()
        hit = lambda: loop.create_task(self.trigger(server_id))  # noqa: E731

        async def on_tcp(reader, writer) -> None:
            writer.close()
            hit()

        udp, _ = await loop.create_datagram_endpoint(lambda: _Udp(hit), local_addr=(HOST, port))
        try:
            tcp = await asyncio.start_server(on_tcp, HOST, port)
        except OSError:
            udp.close()
            raise
        self.listening[server_id] = (udp, tcp)

    def _unbind(self, server_id: int) -> None:
        pair = self.listening.pop(server_id, None)
        if pair:
            pair[0].close()
            pair[1].close()

    async def trigger(self, server_id: int, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if server_id in self._busy or not self._allowed(server_id, now):
            return False
        self._busy.add(server_id)
        try:
            self._unbind(server_id)  # the port must be free before acServer binds it
            self.woken[server_id].append(now)
            return await schedule.wake(server_id, now)
        finally:
            self._busy.discard(server_id)

    async def sync(self, now: float | None = None) -> None:
        """Listen exactly on the servers that are stopped, inside an event window and still allowed a wake."""
        now = now if now is not None else time.time()
        with Session(engine) as sess:
            want = {}
            for s in sess.exec(select(Server)).all():
                inst = supervisor.get(s.id)
                if (not (inst and inst.running)) and schedule.open_window(sess, s.id, now) and self._allowed(s.id, now):
                    want[s.id] = _ports(s.base_port)["udp"]
        for sid in set(self.listening) - set(want):
            self._unbind(sid)
        for sid, port in want.items():
            if sid not in self.listening and sid not in self._busy:
                try:
                    await self._bind(sid, port)
                except OSError as e:  # the port is still held (acServer shutting down, or something else): next round
                    log.info("cannot listen on %s for server %s yet: %s", port, sid, e)

    def close(self) -> None:
        for sid in list(self.listening):
            self._unbind(sid)


waker = Waker()


async def run_forever() -> None:
    try:
        while True:
            try:
                await waker.sync()
            except Exception:
                log.exception("wake sync failed")
            await asyncio.sleep(SYNC_EVERY)
    finally:
        waker.close()
