"""The TCP leg: each client connection is passed on to acServer's internal TCP port, and the relay edits what comes back.

Client -> server bytes are copied untouched. Server -> client bytes are cut into frames (`protocol.split_frames`) so the relay can:
- rewrite the UDP port in the handshake answer (acServer announces its internal one; the client must send UDP to the relay's public port);
- add a CSP handshake frame (minimum CSP build + «server-driven WeatherFX») right after the handshake answer or the car list.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable

from app.relay import protocol as p

log = logging.getLogger("acmanager.relay")


def rewrite_udp_port(payload: bytes, port: int) -> bytes:
    """HandshakeResponse: [id][server name: length byte + 4 bytes per char][UDP port: u16]... -> the same with `port`."""
    pos = 1 + 1 + payload[1] * 4
    return payload[:pos] + struct.pack("<H", port) + payload[pos + 2:]


class TcpRelay:
    def __init__(self, upstream_port: int, public_udp_port: int, *, inject_after: str | None = "handshake", min_csp: int = 0,
                 weather_fx: bool = True, on_connect: Callable[[], None] | None = None) -> None:
        self.upstream_port, self.public_udp_port = upstream_port, public_udp_port
        self.inject_after, self.min_csp, self.weather_fx = inject_after, min_csp, weather_fx
        self.on_connect = on_connect
        self.n_conns = self.n_injected = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.n_conns += 1
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        except OSError:
            writer.close()
            return
        if self.on_connect:
            self.on_connect()
        tasks = [asyncio.ensure_future(self._copy(reader, up_w)), asyncio.ensure_future(self._edit(up_r, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            up_w.close()
            writer.close()

    @staticmethod
    async def _copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()

    async def _edit(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        buf = bytearray()
        while data := await src.read(65536):
            buf += data
            for payload in p.split_frames(buf):
                pid = payload[0] if payload else -1
                if pid == p.NEW_CAR_CONNECTION:
                    payload = rewrite_udp_port(payload, self.public_udp_port)
                dst.write(p.frame(payload))
                if self.inject_after and ((pid == p.NEW_CAR_CONNECTION and self.inject_after == "handshake") or (pid == p.CAR_LIST and self.inject_after == "car_list")):
                    dst.write(p.frame(p.csp_handshake_in(self.min_csp, self.weather_fx)))
                    self.n_injected += 1
            await dst.drain()
