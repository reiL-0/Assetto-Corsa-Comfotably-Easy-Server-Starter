"""The TCP leg: each client connection is passed on to acServer's internal TCP port, and the relay edits what comes back.

Client -> server bytes are copied untouched. Server -> client bytes are cut into frames (`protocol.split_frames`) so the relay can:
- rewrite the UDP port in the handshake answer (acServer announces its internal one; the client must send UDP to the relay's public port);
- put the CSP minimum build in front of the track name (`csp/<build>/../<track>`, as a real AssettoServer does: captured);
- (optional, off by default: a real AssettoServer does NOT send it at join, captured) add a CSP handshake frame (minimum CSP build + «server-driven WeatherFX») at the point the client expects it: AssettoServer sends it inside its
  «first update» burst, i.e. right after the vanilla weather frame (`inject_after="weather"`). Sent earlier (after the handshake answer) the client logs
  «Requesting car list :: unexpected packet received» and ignores it (first spike); WeatherFX then stays in «fallback mode».
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


def rewrite_track(payload: bytes, min_csp: int) -> bytes:
    """HandshakeResponse: after the UDP port (u16) and the refresh rate (u8) comes the track name (UTF-8, length byte). With a minimum CSP build the
    real AssettoServer sends `csp/<build>/../<track>` there (captured from a real join); Content Manager and CSP read it, acServer itself keeps
    its plain track because the relay edits only what the client is told."""
    pos = 1 + 1 + payload[1] * 4 + 2 + 1
    n = payload[pos]
    track = payload[pos + 1:pos + 1 + n]
    if not min_csp or track.startswith(b"csp/"):
        return payload
    new = b"csp/%d/../" % min_csp + track
    return payload[:pos] + bytes([len(new)]) + new + payload[pos + 1 + n:]


class TcpRelay:
    def __init__(self, upstream_port: int, public_udp_port: int, *, inject_after: str | None = None, min_csp: int = 0,
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
        buf, done = bytearray(), False
        trigger = {"handshake": p.NEW_CAR_CONNECTION, "car_list": p.CAR_LIST, "weather": p.WEATHER_UPDATE}.get(self.inject_after or "")
        while data := await src.read(65536):
            buf += data
            for payload in p.split_frames(buf):
                pid = payload[0] if payload else -1
                if pid == p.NEW_CAR_CONNECTION:
                    payload = rewrite_track(rewrite_udp_port(payload, self.public_udp_port), self.min_csp)
                dst.write(p.frame(payload))
                if trigger is not None and pid == trigger and not done:   # once per connection
                    done = True
                    dst.write(p.frame(p.csp_handshake_in(self.min_csp, self.weather_fx)))
                    self.n_injected += 1
            await dst.drain()
