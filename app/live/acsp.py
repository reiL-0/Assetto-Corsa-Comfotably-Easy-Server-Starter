"""ACSP UDP plugin protocol: parse acServer live events, send admin commands.

Reference: Kunos acServer UDP plugin interface (stable since AC 1.x). Strings
are length-prefixed (1 byte char count) with each character widened to 4
bytes — the server serializes its wchar_t buffers naively over the wire.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import deque

from app import metrics
from app.live.board import LiveBoard

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
    gear, rpm = struct.unpack_from("<2H", buf, pos)
    pos += 4
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


class ACSPClient(asyncio.DatagramProtocol):
    """One UDP endpoint per running server: parses events, sends commands.

    Keeps a ring buffer of raw events (for the live WS feed) plus the latest
    session info and per-car snapshot (for live-map/timing GET endpoints).
    """

    def __init__(self, server_id: int, max_events: int = 1000) -> None:
        self.server_id = server_id
        self.restore_when_session_changes: tuple[int, bytes] | None = None  # (session index, its original SET_SESSION_INFO): see app/timeline.py
        self.car_slots = 0  # > 0 only when re-attached to a server that was already running (see connect)
        self.events: deque[dict] = deque(maxlen=max_events)
        self.n_events = 0  # total ever appended; the deque forgets old ones, /live needs a cursor
        self.cars: dict[int, dict] = {}
        self.telemetry: dict[int, dict] = {}  # car_id -> in-game app sample (own car, ~8 Hz)
        self.session: dict = {}
        self.board = LiveBoard()  # live timing table, read by app.live.acsm
        self.transport: asyncio.DatagramTransport | None = None
        self._hello: asyncio.Task | None = None

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
            self.session = event  # drivers stay connected across sessions; keep their guid/name
            from app import timeline   # (late: timeline needs this module's encoders)
            timeline.on_session_event(self.server_id, event)
            if t == "new_session" and self.restore_when_session_changes and event["session_index"] != self.restore_when_session_changes[0]:
                self.send(self.restore_when_session_changes[1])   # the shortened session is over: its original length again
                self.restore_when_session_changes = None
        elif t == "new_connection":
            self.cars[event["car_id"]] = event
        elif t == "connection_closed":
            self.cars.pop(event["car_id"], None)
        elif t == "car_info" and event["is_connected"] and event["driver_guid"]:
            # the answer to GET_CAR_INFO after re-attaching to a running server: someone who joined before us. Not a join (no metric).
            joined = {**event, "type": "new_connection"}
            self.board.apply(joined)
            self.cars[event["car_id"]] = joined
        elif t == "car_update":
            self.cars.setdefault(event["car_id"], {}).update(event)

    def _record(self, e: dict) -> None:
        """Feed the metrics log with the events worth counting."""
        t, sid = e["type"], self.server_id
        track = self.board.session.get("track")
        if t == "new_connection":
            metrics.log(sid, "join", guid=e["driver_guid"], name=e["driver_name"], car=e["car_model"], track=track)
        elif t == "connection_closed":
            metrics.log(sid, "leave", guid=e["driver_guid"], name=e["driver_name"], car=e["car_model"], track=track)
        elif t == "lap_completed":
            d = self.board._by_car(e["car_id"])
            metrics.log(sid, "lap", guid=d.guid if d else None, name=d.name if d else None, car=d.model if d else None,
                        track=track, value=e["laptime_ms"])
        elif t == "new_session":
            metrics.log(sid, "session", name=e["name"], track=track, value=e["session_type"])

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
    await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=(host, local_port),
        remote_addr=(host, remote_port),
    )
    protocol._hello = asyncio.create_task(protocol.hello())
    return protocol
