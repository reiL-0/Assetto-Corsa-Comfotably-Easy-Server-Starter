"""The UDP leg: clients talk to the relay's public port, the relay talks to acServer, and can add packets of its own.

Each client address gets its own socket towards acServer's internal UDP port, so acServer sees one distinct endpoint per client (it
learns it from the client's first datagram, `CAR_CONNECT`). Replies go back out of the public socket, so the client sees one server
address; packets the relay injects leave from that same socket.
"""

from __future__ import annotations

import asyncio
import time

IDLE = 90.0   # seconds of silence after which a client's upstream socket is closed


class _Upstream(asyncio.DatagramProtocol):
    def __init__(self, relay: UdpRelay, client: tuple) -> None:
        self.relay, self.client = relay, client

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.relay.public.sendto(data, self.client)


class UdpRelay(asyncio.DatagramProtocol):
    def __init__(self, upstream_port: int, upstream_host: str = "127.0.0.1") -> None:
        self.upstream = (upstream_host, upstream_port)
        self.public: asyncio.DatagramTransport | None = None
        self.clients: dict[tuple, tuple[asyncio.DatagramTransport, float]] = {}
        self.established: set[tuple] = set()   # clients that sent CAR_CONNECT: past the handshake, safe to inject into
        self.n_in = self.n_out = 0

    def connection_made(self, transport) -> None:
        self.public = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.n_in += 1
        up = self.clients.get(addr)
        if up is None:
            asyncio.ensure_future(self._open(addr, data))
            return
        self.clients[addr] = (up[0], time.monotonic())
        if data[:1] == b"\x4e":
            self.established.add(addr)
        up[0].sendto(data)

    async def _open(self, addr: tuple, first: bytes) -> None:
        if addr in self.clients:
            return
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: _Upstream(self, addr), remote_addr=self.upstream)
        self.clients[addr] = (transport, time.monotonic())
        if first[:1] == b"\x4e":
            self.established.add(addr)
        transport.sendto(first)

    def inject(self, packet: bytes) -> int:
        """Send a packet of our own to every client past the handshake; returns how many."""
        for addr in self.established:
            self.public.sendto(packet, addr)
            self.n_out += 1
        return len(self.established)

    def reap(self) -> None:
        now = time.monotonic()
        for addr, (transport, seen) in list(self.clients.items()):
            if now - seen > IDLE:
                transport.close()
                del self.clients[addr]
                self.established.discard(addr)
