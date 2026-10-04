"""A stopped server that still looks open: players see it in the lobby, and trying to join starts it.

Per server (`Server.wake`): `off` = never; `window` = only inside an event window (see schedule.open_window); `always`.
While a server is stopped and its wake mode allows it, the manager itself holds the server's ports instead of acServer:
- the HTTP port (game port + 1) answers like acServer would with nobody on it: `/INFO` (name, track, cars, slots,
  sessions; `clients` 0) from the copy of the real answer kept in `info.json` (`supervisor`), and `/JSON|...` with the
  entry list's cars and skins, so Content Manager shows the server open and empty. Browsing the lobby wakes nothing;
- the game port (UDP and TCP, the same number): the first datagram or connection closes all the listeners, starts the
  server (`schedule.wake`) and leaves the ports to acServer. That first attempt gets no answer, the player tries again
  a few seconds later.
Only Assetto Corsa wakes it. A player's game or Content Manager first asks the lobby (`/INFO`, `/JSON|…`, user agent
«Assetto Corsa Launcher»); a connection to the game port is accepted as a wake only from an address that did that in the
last AC_SEEN_TTL seconds. UDP never wakes anything: the one thing the game sends there is the ping `0xC8`, which the
manager answers like acServer does (`0xC8` + the HTTP port, little endian) so Content Manager shows the server as
reachable (Join clickable); anything else is ignored. Outside the allowed times nothing listens, so a port scan cannot
start anything. Limited to MAX_PER_HOUR wakes and COOLDOWN seconds between two.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time
from email.utils import formatdate
from pathlib import Path

from sqlmodel import Session, select

from app import schedule, supervisor
from app.config import settings
from app.db import engine
from app.models import Server
from app.servers import _ports

log = logging.getLogger("acmanager.wake")
SYNC_EVERY = 5.0  # seconds between looks at which servers should be listened for
COOLDOWN = 30.0
START_HOLD = 20.0
HTTP_IDLE = 15.0     # seconds an HTTP connection may sit without a request
AC_SEEN_TTL = 15 * 60   # how long an address that asked the lobby as the game does may wake the server
AC_AGENT = "assetto corsa"
PING = 0xC8
MAX_PER_HOUR = 6
HOST = "0.0.0.0"  # tests aim it at loopback


def _instance_dir(server_id: int) -> Path:
    return Path(settings.data_dir) / "instances" / str(server_id)


def facade_info(s: Server) -> dict:
    """What acServer's /INFO would say with nobody on: the last real answer (info.json) when there is one, else built from the config."""
    try:
        info = json.loads((_instance_dir(s.id) / "info.json").read_text())
    except (OSError, ValueError):
        srv, ports = s.config.get("SERVER", {}), _ports(s.base_port)
        track, layout = srv.get("TRACK", ""), srv.get("CONFIG_TRACK") or ""
        sessions = [(1, "PRACTICE", s.config.get("PRACTICE", {}).get("TIME")), (2, "QUALIFY", s.config.get("QUALIFY", {}).get("TIME")),
                    (3, "RACE", s.config.get("RACE", {}).get("LAPS") or s.config.get("RACE", {}).get("TIME"))]
        sessions = [x for x in sessions if x[1] in s.config]
        info = {"ip": "", "port": ports["tcp"], "cport": ports["http"], "name": srv.get("NAME", s.name), "clients": 0,
                "maxclients": int(srv.get("MAX_CLIENTS") or len(s.entry_list) or 0), "track": f"{track}-{layout}" if layout else track,
                "cars": [c for c in str(srv.get("CARS", "")).split(";") if c], "timeofday": 0, "session": 0,
                "sessiontypes": [t for t, _, _ in sessions], "durations": [d or 0 for _, _, d in sessions], "timeleft": 0,
                "country": ["na", "na"], "pass": bool(srv.get("PASSWORD")), "timestamp": 0, "json": None, "l": False,
                "pickup": bool(int(srv.get("PICKUP_MODE_ENABLED", 1))), "tport": ports["udp"], "timed": False, "extra": False,
                "pit": False, "inverted": 0}
    info.update(clients=0, session=0)
    info["timeleft"] = (info["durations"][0] * 60) if info.get("durations") else 0   # the first session, in full
    return info


def facade_cars(s: Server) -> dict:
    """Body of /JSON|<guid>: the entry list's cars and skins, nobody connected."""
    return {"Cars": [{"Model": e.get("MODEL", ""), "Skin": e.get("SKIN", ""), "DriverName": e.get("DRIVERNAME", ""), "DriverTeam": e.get("TEAM", ""),
                      "DriverNation": "", "IsConnected": False, "IsRequestedGUID": False, "IsEntryList": bool(e.get("DRIVERNAME"))}
                     for e in s.entry_list]}


def _abort(writer: asyncio.StreamWriter) -> None:
    """Close with a reset (SO_LINGER 0): the side that closes first keeps the port in TIME_WAIT for a minute, and acServer,
    started right after, must be able to bind it."""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()


def _http_handler(server_id: int, ac_seen):
    """HTTP the way acServer does it (Go's server): HTTP/1.1 keep-alive, a Date header, compact UTF-8 JSON, 200 with an empty body
    for anything else. The client closes the connection; the session ends after IDLE seconds without a request. (Closing with a
    reset while the client still had the body to read made Content Manager lose it: «not enough information».)"""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            while True:
                line = (await asyncio.wait_for(reader.readline(), HTTP_IDLE)).decode(errors="replace")
                if not line.strip():
                    break
                path = line.split(" ")[1] if line.count(" ") >= 2 else ""
                agent, close = "", False
                for _ in range(40):   # the headers, up to the blank line
                    h = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace")
                    if h in ("\r\n", "\n", ""):
                        break
                    name, _, value = h.partition(":")
                    if name.lower() == "user-agent":
                        agent = value.strip()
                    elif name.lower() == "connection" and "close" in value.lower():
                        close = True
                log.info("lobby query server=%s %s from %s agent=%r", server_id, path, peer, agent)
                if AC_AGENT in agent.lower() and path.startswith(("/INFO", "/JSON")):
                    ac_seen(peer)
                with Session(engine) as sess:
                    s = sess.get(Server, server_id)
                    body = _dump(facade_info(s)) if path.startswith("/INFO") else _dump(facade_cars(s)) if path.startswith("/JSON") else ""
                data = body.encode()
                date = formatdate(usegmt=True)
                writer.write(f"HTTP/1.1 200 OK\r\nDate: {date}\r\nContent-Length: {len(data)}\r\nContent-Type: text/plain; charset=utf-8\r\n".encode()
                             + (b"Connection: close\r\n" if close else b"") + b"\r\n" + data)
                await writer.drain()
                if close:
                    break
        except (asyncio.TimeoutError, OSError, IndexError, AttributeError):
            pass
        finally:
            writer.close()
    return handle


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))   # as Go writes it


class _Udp(asyncio.DatagramProtocol):
    """UDP on the game port: answers the game's ping, ignores the rest. It never wakes the server."""

    def __init__(self, pong: bytes) -> None:
        self.pong, self.transport = pong, None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if data == bytes([PING]):
            self.transport.sendto(self.pong, addr)


class Waker:
    def __init__(self) -> None:
        self.listening: dict[int, tuple[asyncio.DatagramTransport, asyncio.AbstractServer, asyncio.AbstractServer | None]] = {}
        self.woken: dict[int, list[float]] = {}
        self.holdoff: dict[int, float] = {}   # server id -> do not hold its ports before this time (it is starting)
        self.ac_seen: dict[str, float] = {}   # address -> when it last asked the lobby as the game does
        self._busy: set[int] = set()

    def release(self, server_id: int) -> None:
        """The server is about to be started by someone else (the panel, a schedule): free its ports for acServer, and do not take them
        back until it is up."""
        self._unbind(server_id)
        self.holdoff[server_id] = time.time() + START_HOLD

    def _note_ac(self, ip: str) -> None:
        now = time.time()
        self.ac_seen = {a: t for a, t in self.ac_seen.items() if now - t < AC_SEEN_TTL}
        self.ac_seen[ip] = now

    def _allowed(self, server_id: int, now: float) -> bool:
        recent = [t for t in self.woken.get(server_id, []) if now - t < 3600]
        self.woken[server_id] = recent
        return len(recent) < MAX_PER_HOUR and (not recent or now - recent[-1] >= COOLDOWN)

    async def _bind(self, server_id: int, port: int, http_port: int | None = None) -> None:
        loop = asyncio.get_running_loop()
        hit = lambda: loop.create_task(self.trigger(server_id))  # noqa: E731

        async def on_tcp(reader, writer) -> None:
            ip = (writer.get_extra_info("peername") or ("?",))[0]
            known = time.time() - self.ac_seen.get(ip, 0) < AC_SEEN_TTL
            log.info("tcp connection to the game port of server %s from %s: %s", server_id, ip, "Assetto Corsa, waking" if known else "not the game, ignored")
            _abort(writer)
            if known:
                hit()

        pong = bytes([PING]) + (http_port or 0).to_bytes(2, "little")
        udp, _ = await loop.create_datagram_endpoint(lambda: _Udp(pong), local_addr=(HOST, port))
        try:
            tcp = await asyncio.start_server(on_tcp, HOST, port)
        except OSError:
            udp.close()
            raise
        http = None
        if http_port:
            try:
                http = await asyncio.start_server(_http_handler(server_id, self._note_ac), HOST, http_port)
            except OSError as e:   # waking matters more than the lobby look: go on without it
                log.info("cannot answer the lobby on %s for server %s: %s", http_port, server_id, e)
        self.listening[server_id] = (udp, tcp, http)

    def _unbind(self, server_id: int) -> None:
        for part in self.listening.pop(server_id, ()):
            if part:
                part.close()

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
        """Hold the ports of exactly the servers that are stopped, whose wake mode allows it now, and still allowed a wake."""
        now = now if now is not None else time.time()
        with Session(engine) as sess:
            want = {}
            for s in sess.exec(select(Server)).all():
                inst = supervisor.get(s.id)
                allowed = s.wake == "always" or (s.wake == "window" and schedule.open_window(sess, s.id, now))
                if allowed and not (inst and inst.running) and self._allowed(s.id, now) and now >= self.holdoff.get(s.id, 0):
                    want[s.id] = (_ports(s.base_port)["udp"], _ports(s.base_port)["http"])
        for sid in set(self.listening) - set(want):
            self._unbind(sid)
        for sid, (port, http_port) in want.items():
            if sid not in self.listening and sid not in self._busy:
                try:
                    await self._bind(sid, port, http_port)
                except OSError as e:  # the port is still held (acServer shutting down, or something else): next round
                    log.info("cannot listen on %s for server %s yet: %s", port, sid, e)

    def close(self) -> None:
        for sid in list(self.listening):
            self._unbind(sid)


waker = Waker()
supervisor.before_start.append(waker.release)   # any start of a server frees the ports the waker holds for it


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
