"""A driver without the game: joins an acServer by its own wire protocol (TCP handshake, UDP car updates, laps) and
reads the sessions the way the real client does (acs.exe: ACClient::handshakeTCP / onNewSession).

Format of the packets: cross-checked with the decompiled client and server (assetto-decomp) and with AssettoServer's
open implementation of the same protocol. Stdlib only. The rules the real client applies are mirrored in
`FakeDriver.spawn` and `FakeDriver.grid_slot`, so a test can assert what a player would see.

    d = FakeDriver("127.0.0.1", 19600, guid=76561198000000001, name="Ana")
    d.connect()          # handshake + UDP association; d.sessions / d.session_type / d.session_index are filled
    d.drive(0.5)         # sends car updates for half a second (keeps the server believing the car is alive)
    d.lap(91234)         # a completed lap of 1:31.234
    d.disconnect()
"""
from __future__ import annotations

import socket
import struct
import threading
import time

PROTOCOL = 202
RACE, PRACTICE, QUALIFY, BOOK = 3, 1, 2, 0   # session Type values of the wire protocol (0 = booking)

# packet ids (the same in both directions where it says so)
REQUEST_NEW_CONNECTION, NEW_CAR_CONNECTION, CAR_LIST_REQUEST, CAR_LIST = 0x3D, 0x3E, 0x3F, 0x40
CLEAN_EXIT, CHECKSUM, POSITION_UPDATE, LAP_COMPLETED = 0x43, 0x44, 0x46, 0x49
CURRENT_SESSION, RACE_OVER, CAR_DISCONNECTED, CAR_CONNECT = 0x4A, 0x4B, 0x4D, 0x4E
SESSION_REQUEST, WELCOME, PING_PONG = 0x4F, 0x51, 0xF8
REFUSALS = {0x3B: "blacklisted", 0x3C: "wrong password", 0x42: "unsupported protocol", 0x45: "no slots available",
            0x6E: "session closed", 0x6F: "auth failed"}


def _s8(text: str) -> bytes:
    raw = text.encode("utf-8")
    return bytes([len(raw)]) + raw


def _s32(text: str) -> bytes:
    return bytes([len(text)]) + text.encode("utf-32-le")


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise ValueError("packet too short")
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def unpack(self, fmt: str):
        v = struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))
        return v[0] if len(v) == 1 else v

    def s8(self) -> str:
        return self.take(self.unpack("B")).decode("utf-8", "replace")

    def s32(self) -> str:
        return self.take(4 * self.unpack("B")).decode("utf-32-le", "replace")

    def rest(self) -> bytes:
        return self.d[self.p:]


class Session:
    def __init__(self, type_, laps, time_min, name="", index=0):
        self.type, self.laps, self.time, self.name, self.index = type_, laps, time_min, name, index

    def __repr__(self):
        return f"Session({self.index}:{self.name!r} type={self.type} {self.time}min {self.laps}laps)"


class Refused(Exception):
    pass


class FakeDriver:
    def __init__(self, host: str, port: int, *, guid: int, name: str, car: str = "bmw_m3_e30", password: str = "",
                 checksums=None):
        self.host, self.port, self.guid, self.name, self.car, self.password = host, port, guid, name, car, password
        self.checksums = checksums            # callable(paths) -> bytes to send as the checksum packet, or None = don't send
        self.session_id = -1                  # my slot (SessionID)
        self.sessions: list[Session] = []     # the list of the handshake (what CM / the UI shows)
        self.current = Session(BOOK, 0, 0)    # what NEW_SESSION says (this is what the real client obeys)
        self.grid_list: list[int] = []        # SessionIDs of the last NEW_SESSION, in grid order
        self.new_sessions = 0                 # how many NEW_SESSION packets arrived
        self.refused: str | None = None
        self.events: list[tuple] = []         # (monotonic, kind, data) of everything interesting the server sent
        self.tcp = self.udp = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._lap_no = 0
        self._seq = 0
        self._pos = (0.0, 0.0, 0.0)
        self._speed = 0.0                     # m/s
        self._norm = 0.0
        self._t0 = time.monotonic()

    # -- what the real client decides from what it received --------------------------------------------------------
    @property
    def session_type(self) -> int:
        return self.current.type

    @property
    def session_index(self) -> int:
        """Index in the handshake list: ACClient::getCurrentSessionIndex finds the FIRST entry of the same type (0 if none)."""
        for i, s in enumerate(self.sessions):
            if s.type == self.current.type:
                return i
        return 0

    @property
    def spawn(self) -> str:
        """CarAvatar::onNewSession: 'START' for a Race, 'PIT' for everything else (convertRemoteSession)."""
        return "START" if self.current.type == RACE else "PIT"

    @property
    def grid_slot(self) -> int | None:
        """setSpawnPositionIndex: my position in the NEW_SESSION list (only applied by the client in a Race)."""
        return self.grid_list.index(self.session_id) if self.session_id in self.grid_list else None

    # -- connection -----------------------------------------------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> "FakeDriver":
        self.tcp = socket.create_connection((self.host, self.port), timeout=timeout)
        body = (bytes([REQUEST_NEW_CONNECTION]) + struct.pack("<H", PROTOCOL) + _s8(str(self.guid)) + _s32(self.name)
                + _s8("") + _s8("") + _s8(self.car) + _s8(self.password))
        self._send_tcp(body)
        pak = self._read_tcp()
        pid = pak[0]
        if pid in REFUSALS:
            self.refused = REFUSALS[pid]
            raise Refused(self.refused)
        if pid != NEW_CAR_CONNECTION:
            raise Refused(f"unexpected packet 0x{pid:02x}")
        self._parse_handshake(_Reader(pak, 1))
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.settimeout(1.0)
        self.udp.connect((self.host, self.udp_port))
        self.udp.send(bytes([CAR_CONNECT, self.session_id]))
        self.tcp.settimeout(0.5)
        self._threads = [threading.Thread(target=self._tcp_loop, daemon=True),
                         threading.Thread(target=self._udp_loop, daemon=True)]
        for t in self._threads:
            t.start()
        if self.checksums is not None:
            self._send_tcp(bytes([CHECKSUM]) + self.checksums(self.checksum_paths))
        self._send_tcp(bytes([CAR_LIST_REQUEST, 0]))
        return self

    def _parse_handshake(self, r: _Reader):
        self.server_name = r.s32()
        self.udp_port = r.unpack("H")
        self.refresh_hz = r.unpack("B")
        self.track, self.track_config, self.car_model, self.car_skin = r.s8(), r.s8(), r.s8(), r.s8()
        r.take(4)                                   # sun angle
        r.take(2 + 1 + 1 + 1 + 1 + 1 + 1)           # tyres out, blankets, tc, abs, stability, autoclutch, jump start
        r.take(4 * 3 + 1 + 1)                       # damage/fuel/tyre rates, virtual mirror, max contacts/km
        self.race_over_ms, self.result_screen_ms = r.unpack("ii")
        r.take(1 + 1 + 2 + 2 + 2)                   # extra lap, gas penalty, pit window start/end, inverted grid
        self.session_id = r.unpack("B")
        count = r.unpack("B")
        self.sessions = [Session(*r.unpack("BHH"), index=i) for i in range(count)]
        name = r.s8()
        idx, typ, tmin, laps = r.unpack("BBHH")
        self.current = Session(typ, laps, tmin, name, idx)
        r.unpack("f")                               # grip
        self.session_id = r.unpack("B")
        self.session_time_ms = r.unpack("q")
        self.checksum_paths = [r.s8() for _ in range(r.unpack("B"))]

    # -- I/O -------------------------------------------------------------------------------------------------------
    def _send_tcp(self, body: bytes):
        with self._lock:
            self.tcp.sendall(struct.pack("<H", len(body)) + body)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.tcp.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _read_tcp(self) -> bytes:
        (n,) = struct.unpack("<H", self._recv_exact(2))
        return self._recv_exact(n)

    def _tcp_loop(self):
        while not self._stop.is_set():
            try:
                pak = self._read_tcp()
            except socket.timeout:
                continue
            except (OSError, ConnectionError):
                self.events.append((time.monotonic(), "closed", None))
                return
            try:
                self._on_tcp(pak)
            except ValueError:
                self.events.append((time.monotonic(), "bad", pak[:1].hex()))

    def _on_tcp(self, pak: bytes):
        pid, r = pak[0], _Reader(pak, 1)
        if pid == CURRENT_SESSION:
            name = r.s8()
            idx, typ, tmin, laps = r.unpack("BBHH")
            r.unpack("f")
            body = r.rest()
            ids, start = list(body[:-8]), struct.unpack("<q", body[-8:])[0]
            self.current = Session(typ, laps, tmin, name, idx)
            self.grid_list = ids
            self.new_sessions += 1
            self._lap_no = 0
            self.events.append((time.monotonic(), "new_session", (idx, typ, tmin, laps, ids, start)))
        elif pid == RACE_OVER:
            self.events.append((time.monotonic(), "race_over", pak[1:]))
        elif pid == CAR_LIST:
            self.events.append((time.monotonic(), "car_list", pak[1:]))
        elif pid == WELCOME:
            self.events.append((time.monotonic(), "welcome", None))
        else:
            self.events.append((time.monotonic(), f"tcp_{pid:02x}", pak[1:]))

    def _udp_loop(self):
        while not self._stop.is_set():
            try:
                pak = self.udp.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if pak and pak[0] == PING_PONG and len(pak) >= 9:     # answer the pings like the game does
                try:
                    self.udp.send(pak[:1] + pak[1:5] + struct.pack("<I", int((time.monotonic() - self._t0) * 1000)) + pak[1:5])
                except OSError:
                    return

    # -- driving ---------------------------------------------------------------------------------------------------
    def set_speed(self, kmh: float):
        self._speed = kmh / 3.6

    def place(self, x: float, y: float, z: float, norm: float | None = None):
        self._pos = (x, y, z)
        if norm is not None:
            self._norm = norm

    def update(self):
        """One car update (what the game sends 18 times a second)."""
        self._seq = (self._seq + 1) & 0xFF
        x, y, z = self._pos
        pak = struct.pack("<BI3f3f3f4BBBHB I h B f", self._seq, int((time.monotonic() - self._t0) * 1000),
                          x, y, z, 0.0, 0.0, 0.0, 0.0, 0.0, self._speed,
                          0, 0, 0, 0, 127, 127, 3000 if self._speed else 800, 2 if self._speed else 0,
                          0, 0, 0, self._norm)
        self.udp.send(bytes([POSITION_UPDATE]) + pak)

    def drive(self, seconds: float, hz: int = 18):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.update()
            time.sleep(1 / hz)

    def lap(self, ms: int, cuts: int = 0, splits=()):
        """Report a completed lap (what the game sends when it crosses the line)."""
        self._lap_no += 1
        # a real lap is stamped when the line is crossed, so two laps are always >= one lap time apart (acServer discards equal stamps)
        stamp = max(int((time.monotonic() - self._t0) * 1000), getattr(self, "_last_stamp", 0) + ms)
        self._last_stamp = stamp
        body = (bytes([LAP_COMPLETED]) + struct.pack("<II", stamp, ms) + bytes([len(splits)])
                + b"".join(struct.pack("<I", s) for s in splits) + bytes([cuts, self._lap_no & 0xFF]))
        self._send_tcp(body)

    def wait_event(self, kind: str, timeout: float = 10.0, since: float | None = None):
        """The first event of that kind after `since` (default: now), or None."""
        since = time.monotonic() if since is None else since
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for t, k, data in list(self.events):
                if k == kind and t >= since:
                    return data
            time.sleep(0.05)
        return None

    def disconnect(self):
        self._stop.set()
        try:
            self._send_tcp(bytes([CLEAN_EXIT]))
        except Exception:
            pass
        for s in (self.tcp, self.udp):
            try:
                s and s.close()
            except OSError:
                pass


if __name__ == "__main__":
    import sys
    host, port = (sys.argv[1], int(sys.argv[2])) if len(sys.argv) > 2 else ("127.0.0.1", 19600)
    d = FakeDriver(host, port, guid=76561198000000001, name="Fake").connect()
    print("slot", d.session_id, "sessions", d.sessions, "current", d.current, "spawn", d.spawn)
    d.drive(2)
    print("events", [(k) for _, k, _ in d.events])
    d.disconnect()
