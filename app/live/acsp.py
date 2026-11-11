"""ACSP UDP plugin protocol: parse acServer live events, send admin commands.

Reference: Kunos acServer UDP plugin interface (stable since AC 1.x). Strings
are length-prefixed (1 byte char count) with each character widened to 4
bytes — the server serializes its wchar_t buffers naively over the wire.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import struct
import time
import urllib.request
from collections import deque

from app import csp, metrics
from app.config import settings
from app.live.board import LiveBoard
from app.live.field import Field
from app.live.tracklen import TrackLength, lookup, oval_known, store_detected

log = logging.getLogger("acmanager.acsp")

TELEMETRY_TTL = 2.0  # s a sample stays in the live map after the app stops sending
TELEMETRY_MIN_INTERVAL = 0.05  # s; the app sends ~8 Hz

# server -> plugin (events)
NEW_SESSION = 50
NEW_CONNECTION = 51
CONNECTION_CLOSED = 52
CAR_UPDATE = 53
CAR_INFO = 54
END_SESSION = 55
VERSION = 56
CHAT = 57
CLIENT_LOADED = 58
SESSION_INFO = 59
ERROR = 60
LAP_COMPLETED = 73
CLIENT_EVENT = 130

# plugin -> server (commands)
REALTIMEPOS_INTERVAL = 200
GET_CAR_INFO = 201
SEND_CHAT = 202
BROADCAST_CHAT = 203
GET_SESSION_INFO = 204
SET_SESSION_INFO = 205
KICK_USER = 206
NEXT_SESSION = 207
RESTART_SESSION = 208
ADMIN_COMMAND = 209

COLLISION_WITH_CAR = 10
COLLISION_WITH_ENV = 11


TRAIL = 50  # position samples kept per car (~10 s at the 5 Hz asked for)
POS_INTERVAL_MS = 200  # car positions to ask the server for (5 Hz is plenty for a map)


def _read_string(buf: bytes, pos: int) -> tuple[str, int]:
    n = buf[pos]
    pos += 1
    return bytes(buf[pos : pos + n * 4 : 4]).decode("latin-1"), pos + n * 4


def _read_sstring(buf: bytes, pos: int) -> tuple[str, int]:
    """acServer's 1-byte-per-char strings (track, session name, car model/skin, weather)."""
    n = buf[pos]
    return bytes(buf[pos + 1 : pos + 1 + n]).decode("latin-1"), pos + 1 + n


def _write_string(s: str) -> bytes:
    data = s.encode("latin-1", errors="replace")
    return bytes([len(data)]) + b"".join(bytes([b, 0, 0, 0]) for b in data)


def _read_session_info(buf: bytes, pos: int) -> tuple[dict, int]:
    version, session_index, current_session_index, session_count = struct.unpack_from(
        "<4B", buf, pos
    )
    pos += 4
    server_name, pos = _read_string(buf, pos)
    track, pos = _read_sstring(buf, pos)
    track = csp.clean_track(track)   # (a server that requires a minimum CSP build names its track «csp/<build>/../<track>»)
    track_config, pos = _read_sstring(buf, pos)
    name, pos = _read_sstring(buf, pos)
    (session_type,) = struct.unpack_from("<B", buf, pos)
    pos += 1
    time_min, laps, wait_time = struct.unpack_from("<3H", buf, pos)
    pos += 6
    ambient_temp, road_temp = struct.unpack_from("<2B", buf, pos)
    pos += 2
    weather, pos = _read_sstring(buf, pos)
    (elapsed_ms,) = struct.unpack_from("<i", buf, pos)
    pos += 4
    return {
        "version": version,
        "session_index": session_index,
        "current_session_index": current_session_index,
        "session_count": session_count,
        "server_name": server_name,
        "track": track,
        "track_config": track_config,
        "name": name,
        "session_type": session_type,
        "time_min": time_min,
        "laps": laps,
        "wait_time": wait_time,
        "ambient_temp": ambient_temp,
        "road_temp": road_temp,
        "weather": weather,
        "elapsed_ms": elapsed_ms,
    }, pos


def _read_connection(buf: bytes, pos: int) -> tuple[dict, int]:
    driver_name, pos = _read_string(buf, pos)
    driver_guid, pos = _read_string(buf, pos)
    car_id = buf[pos]
    pos += 1
    car_model, pos = _read_sstring(buf, pos)
    car_skin, pos = _read_sstring(buf, pos)
    return {
        "car_id": car_id,
        "driver_name": driver_name,
        "driver_guid": driver_guid,
        "car_model": car_model,
        "car_skin": car_skin,
    }, pos


def _read_car_info(buf: bytes, pos: int) -> tuple[dict, int]:
    car_id, is_connected = struct.unpack_from("<2B", buf, pos)
    pos += 2
    car_model, pos = _read_string(buf, pos)
    car_skin, pos = _read_string(buf, pos)
    driver_name, pos = _read_string(buf, pos)
    driver_team, pos = _read_string(buf, pos)
    driver_guid, pos = _read_string(buf, pos)
    return {
        "car_id": car_id,
        "is_connected": bool(is_connected),
        "car_model": car_model,
        "car_skin": car_skin,
        "driver_name": driver_name,
        "driver_team": driver_team,
        "driver_guid": driver_guid,
    }, pos


def _read_car_update(buf: bytes, pos: int) -> tuple[dict, int]:
    car_id = buf[pos]
    pos += 1
    x, y, z, vx, vy, vz = struct.unpack_from("<6f", buf, pos)
    pos += 24
    # acServer 1.x sends gear as ONE byte (33-byte datagram, captured from a real server); a 34-byte one with a 2-byte gear is what this parser
    # used to demand, which made every real position packet fail and get dropped (the live map and the stewards saw no positions)
    if len(buf) - pos >= 8:
        gear, rpm = struct.unpack_from("<2H", buf, pos)
        pos += 4
    else:
        gear, rpm = buf[pos], struct.unpack_from("<H", buf, pos + 1)[0]
        pos += 3
    (spline_pos,) = struct.unpack_from("<f", buf, pos)
    pos += 4
    return {
        "car_id": car_id,
        "pos": [x, y, z],
        "velocity": [vx, vy, vz],
        "gear": gear,
        "rpm": rpm,
        "spline_pos": spline_pos,
    }, pos


def _read_lap_completed(buf: bytes, pos: int) -> tuple[dict, int]:
    car_id = buf[pos]
    pos += 1
    (laptime_ms,) = struct.unpack_from("<I", buf, pos)
    pos += 4
    cuts = buf[pos]
    pos += 1
    cars_count = buf[pos]
    pos += 1
    leaderboard = []
    for _ in range(cars_count):
        cid, lt, laps, completed = struct.unpack_from("<BIHB", buf, pos)
        pos += 8
        leaderboard.append(
            {"car_id": cid, "laptime_ms": lt, "laps": laps, "has_completed": bool(completed)}
        )
    grip_level = None
    if pos + 4 <= len(buf):
        (grip_level,) = struct.unpack_from("<f", buf, pos)
        pos += 4
    return {
        "car_id": car_id,
        "laptime_ms": laptime_ms,
        "cuts": cuts,
        "leaderboard": leaderboard,
        "grip_level": grip_level,
    }, pos


def _read_client_event(buf: bytes, pos: int) -> tuple[dict, int]:
    event_type = buf[pos]
    pos += 1
    car_id = buf[pos]
    pos += 1
    other_car_id = None
    if event_type == COLLISION_WITH_CAR:
        other_car_id = buf[pos]
        pos += 1
    (speed,) = struct.unpack_from("<f", buf, pos)
    pos += 4
    world_pos = list(struct.unpack_from("<3f", buf, pos))
    pos += 12
    rel_pos = list(struct.unpack_from("<3f", buf, pos))
    pos += 12
    return {
        "event_type": event_type,
        "car_id": car_id,
        "other_car_id": other_car_id,
        "speed": speed,
        "world_pos": world_pos,
        "rel_pos": rel_pos,
    }, pos


def parse(buf: bytes) -> dict:
    """Decode one ACSP UDP datagram from acServer into an event dict."""
    packet_id, pos = buf[0], 1
    if packet_id == NEW_SESSION:
        data, _ = _read_session_info(buf, pos)
        return {"type": "new_session", **data}
    if packet_id == SESSION_INFO:
        data, _ = _read_session_info(buf, pos)
        return {"type": "session_info", **data}
    if packet_id == NEW_CONNECTION:
        data, _ = _read_connection(buf, pos)
        return {"type": "new_connection", **data}
    if packet_id == CONNECTION_CLOSED:
        data, _ = _read_connection(buf, pos)
        return {"type": "connection_closed", **data}
    if packet_id == CAR_UPDATE:
        data, _ = _read_car_update(buf, pos)
        return {"type": "car_update", **data}
    if packet_id == CAR_INFO:
        data, _ = _read_car_info(buf, pos)
        return {"type": "car_info", **data}
    if packet_id == END_SESSION:
        filename, _ = _read_string(buf, pos)
        return {"type": "end_session", "filename": filename}
    if packet_id == VERSION:
        return {"type": "version", "version": buf[pos]}
    if packet_id == CHAT:
        car_id = buf[pos]
        message, _ = _read_string(buf, pos + 1)
        return {"type": "chat", "car_id": car_id, "message": message}
    if packet_id == CLIENT_LOADED:
        return {"type": "client_loaded", "car_id": buf[pos]}
    if packet_id == ERROR:
        message, _ = _read_string(buf, pos)
        return {"type": "error", "message": message}
    if packet_id == LAP_COMPLETED:
        data, _ = _read_lap_completed(buf, pos)
        return {"type": "lap_completed", **data}
    if packet_id == CLIENT_EVENT:
        data, _ = _read_client_event(buf, pos)
        return {"type": "client_event", **data}
    return {"type": "unknown", "packet_id": packet_id}


def _cmd(packet_id: int, *parts: bytes) -> bytes:
    return bytes([packet_id]) + b"".join(parts)


def encode_send_chat(car_id: int, message: str) -> bytes:
    return _cmd(SEND_CHAT, bytes([car_id]), _write_string(message))


def encode_broadcast_chat(message: str) -> bytes:
    return _cmd(BROADCAST_CHAT, _write_string(message))


def encode_kick_user(car_id: int) -> bytes:
    return _cmd(KICK_USER, bytes([car_id]))


def encode_next_session() -> bytes:
    return _cmd(NEXT_SESSION)


def encode_restart_session() -> bytes:
    return _cmd(RESTART_SESSION)


def encode_admin_command(command: str) -> bytes:
    return _cmd(ADMIN_COMMAND, _write_string(command))


def encode_get_session_info(index: int = -1) -> bytes:
    return _cmd(GET_SESSION_INFO, struct.pack("<h", index))


def encode_set_session_info(index: int, name: str, session_type: int, laps: int, time_min: int, wait_s: int) -> bytes:
    """Redefine session `index`: name, type (1 practice, 2 qualify, 3 race), laps, length in MINUTES (checked against a running server: the time left
    of the session being played is this length minus what has elapsed) and the pre-race wait in seconds."""
    return _cmd(SET_SESSION_INFO, bytes([index]), _write_string(name), bytes([session_type]), struct.pack("<3I", laps, time_min, wait_s))


def encode_get_car_info(car_id: int) -> bytes:
    return _cmd(GET_CAR_INFO, bytes([car_id]))


def encode_realtime_pos_interval(interval_ms: int) -> bytes:
    return _cmd(REALTIMEPOS_INTERVAL, struct.pack("<H", interval_ms))


CLAIM_PAUSE = 15.0   # s between two answers to the same car's OPRHELLO


def _claim_post(guid: str, code: str) -> bool:
    """Tell the website (blocking: run it in a thread) that `code` may be redeemed for `guid`'s telemetry token. False on any failure."""
    from app.config import settings   # (late, like the others here)
    req = urllib.request.Request(settings.claim_url, data=json.dumps({"guid": guid, "code": code}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + settings.claim_secret, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 204
    except OSError:   # (URLError and HTTPError are OSErrors; a refusal is as good as an outage here)
        return False


class ACSPClient(asyncio.DatagramProtocol):
    """One UDP endpoint per running server: parses events, sends commands.

    Keeps a ring buffer of raw events (for the live WS feed) plus the latest
    session info and per-car snapshot (for live-map/timing GET endpoints).
    """

    def __init__(self, server_id: int, max_events: int = 1000) -> None:
        self.server_id = server_id
        self.restore_when_session_changes: tuple[int, bytes] | None = None  # (session index, its original SET_SESSION_INFO): see app/timeline.py
        self.keep_off = False  # a new set-up was applied to this running server: its practice is no longer the one to keep (app/services/server_service.apply)
        self.on_client_loaded = None  # callable(car_id) or None: a car finished loading (app/live/cspweather.py greets it with the current weather)
        self.car_slots = 0  # > 0 only when re-attached to a server that was already running (see connect)
        self.events: deque[dict] = deque(maxlen=max_events)
        self.n_events = 0  # total ever appended; the deque forgets old ones, /live needs a cursor
        self.cars: dict[int, dict] = {}
        self.trail: dict[int, deque] = {}  # car_id -> last TRAIL (time, pos, velocity) samples: a contact is reported 1.5-2.5 s late, app/stewards looks back in here
        self.telemetry: dict[int, dict] = {}  # car_id -> in-game app sample (own car, ~8 Hz)
        self.session: dict = {}
        self.session_t0 = 0.0   # wall clock of the start of the session being played (from `elapsed_ms`), to know when a timed race is over
        self.winner_done = False   # the winner of this race has been announced
        self.board = LiveBoard()  # live timing table, read by app.live.acsm
        self.field = Field()  # where every car is along the track (laps + spline, unwrapped): the caution procedures read it, app/live/caution.py
        self.oval = False          # the layout being raced is an oval (the admin's word, its tags, or the shape of the laps): no danger warning there
        self._oval_known = False   # something has said so (False: still a road layout until the laps show otherwise)
        self.length = TrackLength()  # the track's length in metres (the blue flag and the danger warning say HOW FAR): from ui_track.json or estimated from the cars, app/live/tracklen.py
        self._length_logged: float | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self._hello: asyncio.Task | None = None
        self._claim_last: dict[int, float] = {}  # car_id -> monotonic time of the last automatic-token request answered (see `_claim_issue`)
        self._claim_tasks: set[asyncio.Task] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            event = parse(data)
        except (IndexError, struct.error):
            return
        self._apply(event)
        self._push(event)

    def _push(self, event: dict) -> None:
        self.events.append(event)
        self.n_events += 1

    def _apply(self, event: dict) -> None:
        t = event["type"]
        self.board.apply(event)
        self._record(event)
        if t in ("new_session", "session_info"):
            from app import supervisor   # late: supervisor owns this client and the applied preset snapshot
            from app.live.racecontrol import _lock, state_of
            inst = supervisor.get(self.server_id)
            nascar = getattr(inst, "nascar_config", None) if t == "new_session" and event.get("session_type") == 3 else None
            with _lock:
                if t == "session_info":
                    if event.get("session_index", event.get("current_session_index")) == event.get("current_session_index"):   # (only the description of the CURRENT session: the answer about another one, e.g. to the redefinition of session 0 the timeline makes when a race starts, must not turn the race into a practice)
                        self.session.update(event)
                else:
                    self.session = event
                    self.field.clear()
                    self.length.reset()
                    self.oval, self._oval_known = False, False
                    self._length_logged = None
                    self._look_up_length(event.get("track"), event.get("track_config") or None)
                    self.winner_done = False
                self.session_t0 = time.time() - (event.get("elapsed_ms") or 0) / 1000
                rc = state_of(self.server_id, True, self.session, create=nascar is not None)
                if nascar is not None:
                    try:
                        rc.configure_nascar(nascar["total"], None, tuple(nascar["shares"]) if nascar.get("shares") is not None else None)
                    except (ValueError, OverflowError) as e:
                        log.warning("NASCAR preset could not arm server %s: %s", self.server_id, e)
                    else:
                        from app.racecontrol import rolling_start
                        rolling_start(rc, self.cars)
            if event.get("session_type") != 1 and (t == "new_session" or self.board.has_kept()):   # qualify / race (also when found already running): what the practice kept is over
                from app import practicekeep
                practicekeep.clear(self.server_id)
                self.board.drop_kept()
            if event.get("session_type") == 2:
                self._kick_qualy_banned(list(self.cars.values()))
            from app import timeline   # (late: timeline needs this module's encoders)
            timeline.on_session_event(self.server_id, event)
            if t == "new_session" and self.restore_when_session_changes and event["session_index"] != self.restore_when_session_changes[0]:
                self.send(self.restore_when_session_changes[1])   # the shortened session is over: its original length again
                self.restore_when_session_changes = None
        elif t == "lap_completed":
            for row in event["leaderboard"]:
                cid, laps = row["car_id"], row["laps"]
                self.field.completed_laps[cid] = max(laps, self.field.completed_laps.get(cid, laps))
            if (self.session or {}).get("session_type") == 1 and not self.keep_off:   # the practice is kept across an idle stop (app/practicekeep.py)
                from app import practicekeep
                practicekeep.save(self.server_id, self.board, event.get("grip_level"))
            from app.live.racecontrol import on_lap, state_of
            from app.racecontrol import nascar_actions  # adapter owns ACSP I/O, like timeline above
            rc0 = state_of(self.server_id, True, self.session)
            pace = rc0.pace_car if rc0 else None   # (the pace car runs laps too: it is not the leader of anything)
            leader_laps = max((row["laps"] for row in event["leaderboard"] if row["car_id"] != pace), default=0)
            rc, actions = on_lap(self.server_id, leader_laps, event["car_id"])
            rcb = state_of(self.server_id, True, self.session, create=(self.session or {}).get("session_type") == 3)   # (a race always has its state: the blue flag needs the pace)
            if rcb:   # the pace of the blue flag: this lap, before the pit marker closes
                rcb.lap_done(event["car_id"], event["laptime_ms"], event.get("cuts") or 0, next((r["laps"] for r in event["leaderboard"] if r["car_id"] == event["car_id"]), 0), time.time(), str((self.cars.get(event["car_id"]) or {}).get("driver_guid") or ""))
            from app.live.racecontrol import on_cuts
            on_cuts(self.server_id, self.session, event["car_id"], event.get("cuts") or 0, False)   # Always close the lap pit marker, including laps without server cuts.
            if actions:
                nascar_actions(self.server_id, rc, actions, event["car_id"])
            if not (rc and rc.nascar):
                from app.racecontrol import plain_winner
                plain_winner(self, event)   # (any other race: the first car to finish is the winner too)
            if rc and (rc.pace_car_in or rc.flag in ("vsc", "fcy")):
                from app.racecontrol import auto_green
                auto_green(self.server_id, rc, event["car_id"])
            if rc and (rc.caution or rc.formation):
                from app.racecontrol import auto_caution
                auto_caution(self.server_id, rc, event["car_id"])
        elif t == "end_session":
            from app.racecontrol import claim_stages   # the NASCAR stage order of the race that just ended becomes that result's own
            claim_stages(self.server_id, event["filename"])
        elif t == "new_connection":
            self.field.drop(event["car_id"]); self.length.estimator.forget(event["car_id"])   # (the slot may be a new driver: they start from their own position)
            self.cars[event["car_id"]] = event
            from app import bans   # (late, like timeline above)
            if bans.is_banned(event["driver_guid"]):
                self.send(encode_kick_user(event["car_id"]))
                metrics.enqueue(self.server_id, "ban_kick", guid=event["driver_guid"], name=event["driver_name"])
            elif (self.session or {}).get("session_type") == 2:
                self._kick_qualy_banned([event])
        elif t == "connection_closed":
            self.cars.pop(event["car_id"], None)
            self.field.drop(event["car_id"]); self.length.estimator.forget(event["car_id"])
        elif t == "client_loaded":
            if self.on_client_loaded:
                self.on_client_loaded(event["car_id"])
            self._claim_probe_greet(event["car_id"])
        elif t == "chat" and event["message"].lstrip("\t$ ").startswith(settings.claim_word("HELLO")):
            self._claim_issue(event["car_id"])
        elif t == "chat" and event["message"].startswith(settings.claim_word("PROBE")) and self._claim_probe_on():
            log.info("claim probe: server %s saw chat from car %s (slot stamped by acServer): %r", self.server_id, event["car_id"], event["message"][:80])
            self._claim_probe_greet(event["car_id"])   # the script listens now: answer with a private code (the one sent at client_loaded arrives before the script runs)
        elif t == "car_info" and event["is_connected"] and event["driver_guid"]:
            # the answer to GET_CAR_INFO after re-attaching to a running server: someone who joined before us. Not a join (no metric).
            joined = {**event, "type": "new_connection"}
            self.board.apply(joined)
            self.cars[event["car_id"]] = joined
        elif t == "client_event":
            from app.live.racecontrol import state_of   # (late, like the others)
            rc = state_of(self.server_id, True, self.session)
            if rc:
                rc.contact(time.time(), event["car_id"], event["other_car_id"], event["speed"])
        elif t == "car_update":
            self.cars.setdefault(event["car_id"], {}).update(event)
            d = self.board._by_car(event["car_id"])
            cid, mono = event["car_id"], time.monotonic()
            self.field.update(cid, event["spline_pos"], d.laps if d else None, mono)
            v = event.get("velocity")
            if (self.length.configured is None or not self._oval_known) and v and len(v) >= 3:   # (a configured length needs no estimate; the shape of the laps is read until something has said if the layout is an oval)
                self.length.estimator.observe(cid, self.field.progress[cid], math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2), mono, self.field.epoch[cid], self.field.generation.get(cid, 0), math.atan2(v[0], v[2]))
                if not self._oval_known and (shape := self.length.estimator.oval) is not None:
                    self.oval, self._oval_known = shape, True
                    log.info("server %s: the laps say this layout is %s", self.server_id, "an oval" if shape else "a road layout")
                    try:
                        asyncio.get_running_loop().run_in_executor(None, store_detected, self.session.get("track"), self.session.get("track_config") or None, shape)
                    except RuntimeError:
                        pass
                m = self.length.metres()
                if m is not None and self._length_logged is None:
                    self._length_logged = m
                    log.info("track length for server %s: about %.0f m (%s)", self.server_id, m, self.length.source())
            self.trail.setdefault(event["car_id"], deque(maxlen=TRAIL)).append((time.time(), event["pos"], event["velocity"]))
        from app.stewards import engine   # (late: the detectors read this module's constants)
        engine.on_event(self, event)

    def _look_up_length(self, track: str | None, config: str | None) -> None:
        """The track's own `ui_track.json`, read OFF the event loop (this runs in the datagram callback, on the loop that receives the UDP: a slow disk must not hold it up). The answer counts only if the
        session is still the one that asked."""
        token = self.session

        def apply(m: float | None, oval: bool | None = None) -> None:
            if self.session is token:
                if oval is not None and not self._oval_known:
                    self.oval, self._oval_known = oval, True
                if m is not None:
                    self.length.configured = m

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:   # no loop (a test, a script): there is nothing to hold up
            apply(lookup(track, config), oval_known(track, config))
            return
        fut = loop.run_in_executor(None, lambda: (lookup(track, config), oval_known(track, config)))

        def done(f: "asyncio.Future[tuple[float | None, bool | None]]") -> None:
            try:
                apply(*f.result())
            except Exception:
                log.exception("server %s: the track length could not be looked up", self.server_id)
        fut.add_done_callback(done)

    def _claim_probe_on(self) -> bool:
        from app.config import settings   # (late, like the others here)
        from app.csp import server_listed
        return server_listed(self.server_id, settings.csp_probe_servers)

    def _claim_probe_greet(self, car_id: int) -> None:
        """TEMPORARY, only on the servers of ACM_CSP_PROBE_SERVERS: answers three questions about the automatic telemetry token before it is built (the token would travel
        by this channel). Sends each car that finishes loading its OWN random code as a hidden chat message to that car alone, and logs it; the probe script
        (static/csp/opr_rc_probe.lua) reports what its client received and sends a chat of its own, which `_apply` logs with the slot acServer stamped on it.
        Compare the logs of two clients: each should have received only its code (private), and the slot of the chat must be the sender's."""
        if not self._claim_probe_on():
            return
        code = f"{self.server_id}-{car_id}-{secrets.token_hex(3)}"
        self.send(encode_send_chat(car_id, "\t\t\t\t$" + settings.claim_word("PROBE:") + code))
        log.info("claim probe: server %s sent code %s to car %s only", self.server_id, code, car_id)

    def _claim_issue(self, car_id: int) -> None:
        """Automatic telemetry token, step 2 (the website's app/telemetry_claim.py explains the whole flow). A driver's script said OPRHELLO in the chat; `car_id` is the number the
        acServer stamped on it, so `self.cars[car_id]["driver_guid"]` is who really is in that car, whatever the script claims. On every server unless ACM_CSP_CLAIM_SERVERS says otherwise (csp.server_listed), at most one
        answer per car every CLAIM_PAUSE s. The website is told {guid, code} first; only when it accepted is the code sent to THAT car alone, and only if the same Steam ID
        still sits in the car (a slot that changed hands meanwhile gets nothing). The code is never logged."""
        from app.config import settings   # (late, like the others here)
        from app.csp import server_listed
        if not server_listed(self.server_id, settings.csp_claim_servers) or not settings.claim_secret:
            return
        guid, now = (self.cars.get(car_id) or {}).get("driver_guid"), time.monotonic()
        if not guid or now - self._claim_last.get(car_id, float("-inf")) < CLAIM_PAUSE:
            return
        self._claim_last[car_id] = now
        code = secrets.token_urlsafe(12)
        task = asyncio.ensure_future(self._claim_deliver(car_id, str(guid), code))
        self._claim_tasks.add(task)
        task.add_done_callback(self._claim_tasks.discard)

    async def _claim_deliver(self, car_id: int, guid: str, code: str) -> None:
        if not await asyncio.to_thread(_claim_post, guid, code):
            log.warning("claim: server %s: the website did not take the code for car %s", self.server_id, car_id)
            return
        if str((self.cars.get(car_id) or {}).get("driver_guid")) != guid:
            return
        self.send(encode_send_chat(car_id, "\t\t\t\t$" + settings.claim_word("CLAIM:") + code))

    def _kick_qualy_banned(self, cars: list[dict]) -> None:
        """The league keeps some drivers from qualifying (app/league.py): they are kicked while the server is in qualifying and can come
        back for the race, without a qualifying time (so they start last)."""
        from app import league   # (late: league imports servers, which imports this module)
        for car in cars:
            if car.get("driver_guid") and league.qualy_banned(self.server_id, car["driver_guid"]):
                self.send(encode_kick_user(car["car_id"]))
                metrics.enqueue(self.server_id, "qualy_ban_kick", guid=car["driver_guid"], name=car.get("driver_name"))

    def _record(self, e: dict) -> None:
        """Feed the metrics log with the events worth counting."""
        t, sid = e["type"], self.server_id
        track = self.board.session.get("track")
        if t == "new_connection":
            metrics.enqueue(sid, "join", guid=e["driver_guid"], name=e["driver_name"], car=e["car_model"], track=track)
        elif t == "connection_closed":
            metrics.enqueue(sid, "leave", guid=e["driver_guid"], name=e["driver_name"], car=e["car_model"], track=track)
        elif t == "lap_completed":
            d = self.board._by_car(e["car_id"])
            metrics.enqueue(sid, "lap", guid=d.guid if d else None, name=d.name if d else None, car=d.model if d else None,
                        track=track, value=e["laptime_ms"], cuts=e["cuts"])
        elif t == "new_session":
            metrics.enqueue(sid, "session", name=e["name"], track=track, value=e["session_type"])

    def add_telemetry(self, car_id: int, sample: dict) -> bool:
        """Store an in-game-app sample and stream it. False if it came too soon (rate limit)."""
        now = time.time()
        prev = self.telemetry.get(car_id)
        if prev and now - prev["ts"] < TELEMETRY_MIN_INTERVAL:
            return False
        sample = {**sample, "ts": now}
        self.telemetry[car_id] = sample
        self._push({"type": "telemetry", "car_id": car_id, **sample})
        return True

    def snapshot(self) -> dict[int, dict]:
        """cars + their fresh app telemetry (under `telemetry`)."""
        now = time.time()
        return {
            i: {**c, "telemetry": t} if (t := self.telemetry.get(i)) and now - t["ts"] < TELEMETRY_TTL else c
            for i, c in self.cars.items()
        }

    def send(self, data: bytes) -> None:
        if self.transport:
            self.transport.sendto(data)

    async def hello(self) -> None:
        """The plugin socket is bound right after acServer is spawned, so its startup packets are lost and it does not
        know us yet. Ask for the session and for car positions until it answers (it only sends positions on request)."""
        for _ in range(30):
            self.send(encode_get_session_info(-1))
            self.send(encode_realtime_pos_interval(POS_INTERVAL_MS))
            await asyncio.sleep(1)
            if self.session:
                self.send(encode_realtime_pos_interval(POS_INTERVAL_MS))
                for car_id in range(self.car_slots):  # re-attached to a running server: it will not announce the cars already on it
                    self.send(encode_get_car_info(car_id))
                return

    def close(self) -> None:
        if self._hello:
            self._hello.cancel()
        if self.transport:
            self.transport.close()


async def connect(
    server_id: int, remote_port: int, local_port: int, host: str = "127.0.0.1", car_slots: int = 0
) -> ACSPClient:
    """Bind our side of the plugin socket and target acServer's local port. `car_slots` > 0 when the server is already
    running: its car slots are asked for who sits in them."""
    loop = asyncio.get_running_loop()
    protocol = ACSPClient(server_id)
    protocol.car_slots = car_slots
    from app import practicekeep   # late: it imports the board, and the board is imported here
    if kept := practicekeep.load(server_id):
        practicekeep.seed(protocol.board, kept.drivers)
    await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=(host, local_port),
        remote_addr=(host, remote_port),
    )
    protocol._hello = asyncio.create_task(protocol.hello())
    return protocol
