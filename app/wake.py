"""A stopped server that still looks open: players see it in the lobby, and trying to join starts it.

Per server (`Server.wake`): `off` = never; `window` = only inside an event window (see schedule.open_window); `always`.
The HTTP port (game port + 1) is always the manager's, never acServer's (that one listens on `http_internal`, see
`server_service.ports`): while the server runs the manager relays `/INFO` and `/JSON|…` to acServer (swapping its own port number for the
public one) and answers `/api/details`, the Content Manager wrapper's page whose `description` is the server's welcome text; it keeps
answering through a start. While a server is stopped and its wake mode allows it, the manager also holds the game port instead of acServer:
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
import http.client
import ipaddress
import json
import logging
import re
import socket
import struct
import time
import urllib.request
from urllib.parse import unquote, urlsplit
from email.utils import formatdate
from pathlib import Path

from sqlmodel import Session, select

from app import requirements, schedule, supervisor, timeline
from app.config import settings
from app.db import engine
from app.models import Event, Server, Setting
from app.services.server_service import ports as _ports
from fastapi import HTTPException

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


EXTENDED_SEPARATOR = "ℹ"   # Content Manager does NOT read the "extra" flag of /INFO: it takes the port of /api/details from the END OF THE SERVER NAME, "<name> ℹ<port>"


_MARKER = re.compile(rf"\s*{EXTENDED_SEPARATOR}\s*\d+\s*$")   # only a TRAILING "ℹ<digits>": a name like "Liga ℹ información" is a real name


def clean_name(name) -> str:
    return _MARKER.sub("", str(name or ""))


def lobby_info(s: Server, info: dict) -> dict:
    """What /INFO tells a player: our public HTTP port as `cport` and the name ending in " ℹ<port>" (an old or duplicated marker is replaced) so Content
    Manager asks that port for /api/details (the description). The marker is only for /INFO; /api/details keeps the clean name (the league site shows it)."""
    port = _ports(s.base_port)["http"]
    info["cport"], info["extra"] = port, True
    info["name"] = f"{clean_name(info.get('name'))} {EXTENDED_SEPARATOR}{port}"
    return info


def _preview(s: Server):
    """Unloaded event in the wake window: derive the same eligible grid as wake, without sending notices."""
    if s.wake == "off":
        return None
    try:
        with Session(engine) as db:
            sc = schedule.open_window(db, s.id, time.time())
            if not sc or sc.loaded or not (ev := db.get(Event, sc.event_id)):
                return None
            from app.league import session_for
            return session_for(db, ev, sc.start_at, notify=False)
    except HTTPException as exc:
        log.warning("cannot preview pending event for server %s: %s", s.id, exc.detail)
        return None
    except Exception:
        log.warning("cannot preview pending event for server %s", s.id, exc_info=True)
        return None


def _preview_server(s: Server, plan):
    """Build an in-memory server view; never mutate the stored configuration or start acServer."""
    from types import SimpleNamespace
    srv = dict(s.config.get("SERVER", {}))
    cars = list(dict.fromkeys(e.model for e in plan.entries)) if plan.entries else plan.cars
    srv.update(NAME=plan.name, TRACK=plan.track, CONFIG_TRACK=plan.track_config,
               CARS=";".join(cars), MAX_CLIENTS=len(plan.entries) or plan.max_clients,
               PASSWORD=plan.password, LOCKED_ENTRY_LIST=int(plan.locked),
               PICKUP_MODE_ENABLED=int(plan.pickup))
    from app import csp
    if build := csp.min_build_for(s.csp_off):
        srv["TRACK"] = f"csp/{build}/../{plan.track}"
    cfg = {"SERVER": srv}
    if plan.practice_min:
        cfg["PRACTICE"] = {"NAME": "Practice", "TIME": plan.practice_min, "IS_OPEN": 1}
    if plan.qualify_min:
        cfg["QUALIFY"] = {"NAME": "Qualify", "TIME": plan.qualify_min, "IS_OPEN": 1}
    if plan.race_laps:
        cfg["RACE"] = {"NAME": "Race", "LAPS": plan.race_laps, "WAIT_TIME": plan.race_wait_s, "IS_OPEN": 1}
    elif plan.race_min:
        cfg["RACE"] = {"NAME": "Race", "LAPS": 0, "TIME": plan.race_min, "WAIT_TIME": plan.race_wait_s, "IS_OPEN": 1}
    entries = ([{"MODEL": e.model, "SKIN": e.skin, "DRIVERNAME": e.driver_name,
                 "TEAM": e.team, "GUID": e.guid} for e in plan.entries] if plan.entries else
               [{"MODEL": car, "SKIN": ""} for car in
                ([car for car, count in plan.car_counts.items() for _ in range(count)] if plan.car_counts else
                 [cars[i % len(cars)] for i in range(plan.max_clients)])])
    return SimpleNamespace(id=s.id, base_port=s.base_port, name=s.name, config=cfg,
                           entry_list=entries, welcome=plan.welcome, anchor_index=None, anchor_at=None,
                           session={"race_est_min": plan.race_est_min or 0})


def facade_info(s: Server, preview: bool = True) -> dict:
    """What acServer's /INFO would say with nobody on: the last real answer (info.json) when there is one, else built from the config."""
    if preview and (plan := _preview(s)):
        return facade_info(_preview_server(s, plan), preview=False)
    try:
        info = json.loads((_instance_dir(s.id) / "info.json").read_text()) if preview else {}
        if not info:
            raise ValueError("build preview from event")
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
                "pickup": bool(int(srv.get("PICKUP_MODE_ENABLED", 1))), "tport": ports["udp"], "timed": False, "extra": False,   # (timed: set from the configuration below, for a snapshot too)
                "pit": False, "inverted": 0}
    race = s.config.get("RACE") or {}
    info["timed"] = bool(race) and not int(race.get("LAPS") or 0)   # a race by TIME (LAPS=0): Content Manager reads the race's duration as minutes only when this is true, else as laps; the snapshot's is the last run's, not this configuration's
    info["cport"] = _ports(s.base_port)["http"]   # the snapshot came from acServer's own HTTP port: players are told ours
    info["extra"] = True   # the lobby flag; Content Manager does NOT use it to find /api/details: that is the " ℹ<port>" marker (lobby_info)
    info.update(clients=0, session=0)
    if timeline.config_durations(s.config):   # the snapshot may have been taken while a session was shortened to catch up with the clock
        info["durations"] = timeline.config_durations(s.config)
    info["timeleft"] = (info["durations"][0] * 60) if info.get("durations") else 0   # the first session, in full
    pos = timeline.server_position(s)
    if pos:   # the session clock keeps running while the server is off (app/timeline.py)
        info.update(session=pos["index"], timeleft=int(pos["remaining_s"]))
    return info


def facade_cars(s: Server, guid: str = "") -> dict:
    """Body of /JSON|<guid>: the entry list's cars and skins, nobody connected (checked byte for byte against the running acServer's).
    `IsRequestedGUID` is the slot booked for the asker (its GUID, one of the `;`-separated ones, is the one in the path): Content Manager takes a
    server without pickup (every locked league event) as «Wait for the next booking», with Join disabled, unless one slot comes back booked for the player."""
    if plan := _preview(s):
        s = _preview_server(s, plan)
    return {"Cars": [{"Model": e.get("MODEL", ""), "Skin": e.get("SKIN", ""), "DriverName": e.get("DRIVERNAME", ""), "DriverTeam": e.get("TEAM", ""),
                      "DriverNation": "", "IsConnected": False, "IsRequestedGUID": bool(guid) and guid in str(e.get("GUID", "")).split(";"),
                      "IsEntryList": True}   # acServer says True for every slot of the entry list, named or not
                     for e in s.entry_list]}


def _abort(writer: asyncio.StreamWriter) -> None:
    """Close with a reset (SO_LINGER 0): the side that closes first keeps the port in TIME_WAIT for a minute, and acServer,
    started right after, must be able to bind it."""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()


UPSTREAM_TTL = 5.0   # seconds an answer of acServer's HTTP port is reused: every player's game asks the lobby every ~15 s and each ask lands on acServer's main loop
_upstream_cache: dict[tuple[int, str], tuple[float, bytes]] = {}


def _upstream(port: int, path: str) -> bytes:
    """acServer's own answer (its internal HTTP port), reused for UPSTREAM_TTL seconds; empty when it does not answer."""
    now = time.monotonic()
    hit = _upstream_cache.get((port, path))
    if hit and now - hit[0] < UPSTREAM_TTL:
        return hit[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
            body = r.read()
    except (OSError, ValueError, http.client.HTTPException):   # (InvalidURL is an HTTPException: a path with no leading slash or odd characters is an empty answer)
        return b""
    if len(_upstream_cache) > 200:   # /JSON|<guid> has one key per player that ever asked
        _upstream_cache.clear()
    _upstream_cache[(port, path)] = (now, body)
    return body


def _league_of(server_id: int) -> int | None:
    """The league of the event visible on this server, loaded or pending within its wake window."""
    try:
        with Session(engine) as sess:
            sc = schedule.open_window(sess, server_id, time.time())
            if sc:
                event = sess.get(Event, sc.event_id)
                return event.league_id if event else None
            return None
    except Exception:   # the lobby page must answer even when the lookup fails: it shows the global list, and the error is logged
        log.warning("could not find the league of server %s; showing the global requirements", server_id, exc_info=True)
        return None


def details(s: Server, live: bytes | None, host: str = "") -> dict:
    """Body of /api/details, with the same pending-event view as the stopped lobby."""
    plan = _preview(s) if not live else None
    if plan:
        s = _preview_server(s, plan)
    try:
        info = json.loads(live) if live else facade_info(s, preview=not plan)
    except ValueError:
        info = facade_info(s)
    info["cport"], info["extra"] = _ports(s.base_port)["http"], True
    info["name"] = clean_name(info.get("name"))   # clean, whatever the source was
    info["wrappedPort"] = info["cport"]   # Content Manager keeps asking THIS port for the details (ServerInformationExtended.PortExtended)
    req = requirements.lines(requirements.current(_league_of(s.id)))   # what the player must install, under the welcome text: the league's list while its event is on this server
    info["description"] = s.welcome + ("\n\nRequisitos:\n" + "\n".join(req) if req else "")
    if host:
        with Session(engine) as db:
            image = db.get(Setting, f"loading_image:{s.id}")
        if image and image.value.get("filename") == f"{s.id}.png":
            info["loadingImageUrl"] = f"http://{host}/loading-image"
    return info


def _loading_image(server_id: int) -> bytes:
    with Session(engine) as db:
        image = db.get(Setting, f"loading_image:{server_id}")
    if image and image.value.get("filename") == f"{server_id}.png":
        try:
            return (Path(settings.data_dir) / "loading-images" / f"{server_id}.png").read_bytes()
        except OSError:
            log.warning("loading image unavailable for server %s", server_id, exc_info=True)
    return b""


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
                agent, close, host = "", False, ""
                for _ in range(40):   # the headers, up to the blank line
                    h = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace")
                    if h in ("\r\n", "\n", ""):
                        break
                    name, _, value = h.partition(":")
                    if name.lower() == "host":
                        host = value.strip()
                    elif name.lower() == "user-agent":
                        agent = value.strip()
                    elif name.lower() == "connection" and "close" in value.lower():
                        close = True
                log.info("lobby query server=%s %s from %s agent=%r", server_id, path, peer, agent)
                if AC_AGENT in agent.lower() and path.startswith(("/INFO", "/JSON")):
                    ac_seen(peer)
                def get_server():
                    with Session(engine) as sess:
                        return sess.get(Server, server_id)
                s = await asyncio.to_thread(get_server)
                inst = supervisor.get(server_id)
                internal = _ports(s.base_port)["http_internal"]
                running = bool(inst and inst.running)
                ctype = "text/plain; charset=utf-8"
                if path == "/loading-image":
                    data = await asyncio.to_thread(_loading_image, server_id)
                    ctype = "image/png"
                elif path.startswith("/api/details"):
                    ctype = "application/json; charset=utf-8"
                    live = await asyncio.to_thread(_upstream, internal, "/INFO") if running else None
                    advertised = urlsplit(settings.public_url).hostname if settings.public_url else ""
                    hostname, sep, given_port = host.rpartition(":")
                    is_valid_host = False
                    if sep and given_port == str(_ports(s.base_port)["http"]):
                        if advertised and hostname.lower() == advertised.lower():
                            is_valid_host = True
                        else:
                            try:
                                ip = ipaddress.ip_address(hostname)
                                is_valid_host = not (ip.is_multicast or ip.is_unspecified or (ip.is_loopback and peer not in ("127.0.0.1", "::1")))
                            except ValueError:
                                is_valid_host = bool(re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9.-]{0,253}[a-zA-Z0-9])?", hostname)) and ".." not in hostname
                    image_host = host if is_valid_host else ""
                    data = _dump(await asyncio.to_thread(details, s, live, image_host)).encode()
                elif running and path.startswith(("/INFO", "/JSON")):   # the real acServer answers (only what the lobby asks: this port has no login); its own HTTP port number is swapped for ours in /INFO
                    data = await asyncio.to_thread(_upstream, internal, path)
                    if path.startswith("/INFO") and data:
                        try:
                            info = json.loads(data)
                            data = _dump(lobby_info(s, info)).encode()
                        except ValueError:
                            pass
                else:
                    if path.startswith("/INFO"):
                        body = await asyncio.to_thread(facade_info, s)
                        data = _dump(lobby_info(s, body)).encode()
                    elif path.startswith("/JSON"):
                        body = await asyncio.to_thread(facade_cars, s, unquote(path.partition("?")[0]).partition("|")[2])
                        data = _dump(body).encode()
                    else:
                        data = b""
                date = formatdate(usegmt=True)
                writer.write(f"HTTP/1.1 200 OK\r\nDate: {date}\r\nContent-Length: {len(data)}\r\nContent-Type: {ctype}\r\n".encode()
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
        self.listening: dict[int, tuple[asyncio.DatagramTransport, asyncio.AbstractServer]] = {}   # the game port, while stopped and waking is allowed
        self.http: dict[int, asyncio.AbstractServer] = {}   # the public HTTP port: the lobby look while stopped, a relay to acServer while running
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

    async def _bind_http(self, server_id: int, http_port: int) -> None:
        try:
            self.http[server_id] = await asyncio.start_server(_http_handler(server_id, self._note_ac), HOST, http_port)
        except OSError as e:   # still held (acServer of an older start, or something else): next round
            log.info("cannot answer on the HTTP port %s for server %s yet: %s", http_port, server_id, e)

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
        self.listening[server_id] = (udp, tcp)

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
            want, want_http = {}, {}
            for s in sess.exec(select(Server)).all():
                inst = supervisor.get(s.id)
                running = bool(inst and inst.running)
                allowed = s.wake == "always" or (s.wake == "window" and schedule.open_window(sess, s.id, now))
                if allowed and not running and self._allowed(s.id, now) and now >= self.holdoff.get(s.id, 0):
                    want[s.id] = (_ports(s.base_port)["udp"], _ports(s.base_port)["http"])
                if running or allowed or now < self.holdoff.get(s.id, 0):   # the HTTP port keeps answering through a start
                    want_http[s.id] = _ports(s.base_port)["http"]
        for sid in set(self.listening) - set(want):
            self._unbind(sid)
        for sid in set(self.http) - set(want_http):
            self.http.pop(sid).close()
        for sid, http_port in want_http.items():
            if sid not in self.http and sid not in self._busy:
                await self._bind_http(sid, http_port)
        for sid, (port, http_port) in want.items():
            if sid not in self.listening and sid not in self._busy:
                try:
                    await self._bind(sid, port, http_port)
                except OSError as e:  # the port is still held (acServer shutting down, or something else): next round
                    log.info("cannot listen on %s for server %s yet: %s", port, sid, e)

    def close(self) -> None:
        for sid in list(self.listening):
            self._unbind(sid)
        for sid in list(self.http):
            self.http.pop(sid).close()


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
