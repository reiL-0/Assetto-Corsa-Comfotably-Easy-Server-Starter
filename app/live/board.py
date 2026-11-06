"""Live timing state built from ACSP events: who is on track, their laps, and where they are."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

NS = 1_000_000  # ACSM reports lap times in nanoseconds; ACSP gives milliseconds
NO_TIME = 999_999_999  # acServer's smallest "no lap yet" sentinel (also 0xFFFFFFFF)
RACE = 3  # ACSP / ACSM session type


@dataclass
class Driver:
    car_id: int
    name: str = ""
    guid: str = ""
    model: str = ""
    skin: str = ""
    connected: bool = True
    best_ms: int = 0
    last_ms: int = 0
    laps: int = 0
    total_ms: int = 0
    top_kmh: float = 0.0
    valid_laps: int | None = None  # laps without cuts (None: this board does not know, as when it is read from the log); the best of them is `best_valid_ms` (0 = none)
    best_valid_ms: int = 0
    kept_ms: int = 0  # best lap brought from the practice kept across an idle stop (app/practicekeep.py): the server's own table starts empty and must not lower it
    best_splits: list[int] = field(default_factory=list)  # [s1_ms, s2_ms, s3_ms] for best_ms
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    spline: float = 0.0


class LiveBoard:
    """Fed by `ACSPClient._apply`; read by `app.live.acsm` (ACSM-compatible JSON)."""

    def __init__(self) -> None:
        self.session: dict = {}  # last new_session / session_info event
        self.session_at = 0.0  # wall clock when it arrived: elapsed time keeps running from there
        self.keep_next = False  # the drivers of the kept practice (app/practicekeep.py) were seeded: the next practice session does not wipe them
        self.drivers: list[Driver] = []
        self._sector_track: dict[int, dict] = {}  # car_id -> {"last_t", "last_s", "t_start", "t_s1", "t_s2", "pending"}

    def has_kept(self) -> bool:
        return self.keep_next or any(d.kept_ms for d in self.drivers)

    @staticmethod
    def _reset(d: Driver) -> None:
        d.best_ms = d.last_ms = d.laps = d.total_ms = d.kept_ms = 0
        d.best_splits = []
        d.top_kmh = 0.0
        d.valid_laps, d.best_valid_ms = None, 0

    def drop_kept(self) -> None:
        """The kept practice does not belong to this session: its drivers go (one already back on track starts from zero)."""
        self.keep_next = False
        self._sector_track.clear()
        self.drivers = [d for d in self.drivers if d.connected]
        for d in self.drivers:
            if d.kept_ms:
                self._reset(d)

    def _by_car(self, car_id: int) -> Driver | None:
        """Newest driver that used this car slot (slots are reused by whoever joins next)."""
        return next((d for d in reversed(self.drivers) if d.car_id == car_id and d.connected), None)

    def _track_sectors(self, car_id: int, s: float, now: float) -> None:
        B1, B2 = 1.0 / 3.0, 2.0 / 3.0
        st = self._sector_track.get(car_id)
        if st is None:
            self._sector_track[car_id] = {"last_t": now, "last_s": s, "t_start": None, "t_s1": None, "t_s2": None, "pending": None}
            return

        t_prev, s_prev = st["last_t"], st["last_s"]
        dt = now - t_prev
        st["last_t"], st["last_s"] = now, s

        if dt <= 0 or dt > 3.0:  # hole in telemetry or clock jump
            st.update(t_start=None, t_s1=None, t_s2=None, pending=None)
            return

        # Check wrap-around crossing at start/finish (1.0 -> 0.0)
        if s_prev > 0.85 and s < 0.15:
            step = (s + 1.0) - s_prev
            if 0 < step < 0.25:
                t_cross = t_prev + ((1.0 - s_prev) / step) * dt
                if st["t_start"] is not None and st["t_s1"] is not None and st["t_s2"] is not None:
                    dt1 = st["t_s1"] - st["t_start"]
                    dt2 = st["t_s2"] - st["t_s1"]
                    dt3 = t_cross - st["t_s2"]
                    total = t_cross - st["t_start"]
                    if dt1 > 0 and dt2 > 0 and dt3 > 0 and total > 0:
                        st["pending"] = (dt1 / total, dt2 / total)
                st["t_start"] = t_cross
                st["t_s1"] = None
                st["t_s2"] = None
            else:
                st.update(t_start=None, t_s1=None, t_s2=None, pending=None)
            return

        # Normal forward progress
        if s >= s_prev:
            step = s - s_prev
            if step > 0.25:  # teleport
                st.update(t_start=None, t_s1=None, t_s2=None, pending=None)
                return
            if s_prev < B1 <= s and st["t_start"] is not None:
                st["t_s1"] = t_prev + ((B1 - s_prev) / step) * dt
            if s_prev < B2 <= s and st["t_s1"] is not None:
                st["t_s2"] = t_prev + ((B2 - s_prev) / step) * dt
        else:
            if s_prev - s > 0.05:  # reverse or spin
                st.update(t_start=None, t_s1=None, t_s2=None, pending=None)

    def apply(self, e: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        t = e["type"]
        if t in ("new_session", "session_info"):
            self.session, self.session_at = e, now
            if t == "new_session" and not (self.keep_next and e.get("session_type") == 1):  # each session starts its own table; whoever is still connected stays
                self.drop_kept()
                self._sector_track.clear()
                self.drivers = [d for d in self.drivers if d.connected]
                for d in self.drivers:
                    self._reset(d)
            self.keep_next = False   # the first session event (new_session or the session_info that finds it) is the one the kept practice belongs to; a later one is another session
        elif t == "new_connection":
            self._sector_track.pop(e["car_id"], None)
            old = next((d for d in self.drivers if e["driver_guid"] and d.guid == e["driver_guid"]), None)
            d = old or Driver(car_id=e["car_id"])
            d.car_id, d.connected = e["car_id"], True
            d.name, d.guid = e["driver_name"], e["driver_guid"]
            d.model, d.skin = e["car_model"], e["car_skin"]
            if not old:
                self.drivers.append(d)
        elif t == "connection_closed":
            self._sector_track.pop(e["car_id"], None)
            d = self._by_car(e["car_id"])
            if d:
                d.connected = False
        elif t == "car_update":
            cid = e["car_id"]
            d = self._by_car(cid)
            if d:
                d.pos, d.spline = tuple(e["pos"]), e["spline_pos"]
                d.top_kmh = max(d.top_kmh, math.hypot(*e["velocity"]) * 3.6)  # ponytail: top speed of the session, ACSM gives it for the best lap
                self._track_sectors(cid, e["spline_pos"], now)
        elif t == "lap_completed":
            cid = e["car_id"]
            d = self._by_car(cid)
            lap_ms = e["laptime_ms"]
            lap_splits: list[int] | None = None
            st = self._sector_track.get(cid)
            if st and 0 < lap_ms < NO_TIME:
                pending = st.pop("pending", None)
                if pending:
                    r1, r2 = pending
                    s1 = round(r1 * lap_ms)
                    s2 = round(r2 * lap_ms)
                    s3 = lap_ms - (s1 + s2)
                    if s1 > 0 and s2 > 0 and s3 > 0:
                        lap_splits = [s1, s2, s3]
                elif st["t_start"] is not None and st["t_s1"] is not None and st["t_s2"] is not None:
                    total = now - st["t_start"]
                    dt1 = st["t_s1"] - st["t_start"]
                    dt2 = st["t_s2"] - st["t_s1"]
                    dt3 = now - st["t_s2"]
                    if total > 0 and dt1 > 0 and dt2 > 0 and dt3 > 0:
                        s1 = round((dt1 / total) * lap_ms)
                        s2 = round((dt2 / total) * lap_ms)
                        s3 = lap_ms - (s1 + s2)
                        if s1 > 0 and s2 > 0 and s3 > 0:
                            lap_splits = [s1, s2, s3]
                    st["t_start"] = now
                    st["t_s1"] = None
                    st["t_s2"] = None

            if d:
                d.last_ms, d.laps = lap_ms, d.laps + 1
                d.total_ms += lap_ms
                valid = e.get("cuts", 0) == 0 and 0 < lap_ms < NO_TIME
                d.valid_laps = (d.valid_laps or 0) + valid
                if valid:
                    d.best_valid_ms = min(d.best_valid_ms or NO_TIME, lap_ms)
                if not e.get("leaderboard"):  # fallback if acServer emits no leaderboard rows
                    if 0 < lap_ms < NO_TIME:
                        old_best = d.best_ms
                        d.best_ms = min(lap_ms, d.kept_ms) if d.kept_ms else lap_ms
                        if lap_splits and (d.best_ms == lap_ms or d.best_ms != old_best or not d.best_splits):
                            d.best_splits = lap_splits
            for row in e["leaderboard"]:  # the server's own table: best lap and lap count of every car
                r = self._by_car(row["car_id"])
                if r:
                    r.laps = max(r.laps, row["laps"])
                    if 0 < row["laptime_ms"] < NO_TIME:
                        old_best = r.best_ms
                        r.best_ms = min(row["laptime_ms"], r.kept_ms) if r.kept_ms else row["laptime_ms"]
                        if row["car_id"] == cid and lap_splits and (r.best_ms == row["laptime_ms"] or r.best_ms != old_best or not r.best_splits):
                            r.best_splits = lap_splits

    def elapsed_ms(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return int(self.session.get("elapsed_ms", 0) + max(0.0, now - self.session_at) * 1000)

    def _entry(self, d: Driver, position: int) -> dict:
        best_lap = (d.best_ms * NS) if 0 < d.best_ms < NO_TIME else 0
        last_lap = (d.last_ms * NS) if 0 < d.last_ms < NO_TIME else 0
        best_valid = (d.best_valid_ms * NS) if 0 < d.best_valid_ms < NO_TIME else 0
        splits = {
            "0": {"SplitTime": d.best_splits[0] * NS, "Cuts": 0},
            "1": {"SplitTime": d.best_splits[1] * NS, "Cuts": 0},
            "2": {"SplitTime": d.best_splits[2] * NS, "Cuts": 0},
        } if len(d.best_splits) == 3 and 0 < d.best_ms < NO_TIME else {}
        return {
            "CarInfo": {
                "DriverName": d.name, "DriverGUID": d.guid, "DriverInitials": "", "TeamName": "",
                "CarModel": d.model, "CarSkin": d.skin, "CarID": d.car_id, "RaceNumber": 0,
            },
            "IsInPits": False,  # ACSP does not say
            "Position": position,
            "Split": "",
            "Ping": 0,  # ACSP does not say
            "TotalNumLaps": d.laps,
            "Cars": {
                d.model: {
                    "BestLap": best_lap, "LastLap": last_lap, "NumLaps": d.laps,
                    "TotalLapTime": d.total_ms * NS, "BestLapSplits": splits, "TopSpeedBestLap": round(d.top_kmh),
                    **({"ValidLaps": d.valid_laps, "BestValidLap": best_valid} if d.valid_laps is not None else {}),   # (ours, not ACSM's: the lobby page shows the valid laps and the quickest of them)
                }
            },
            "LastPos": {"X": d.pos[0], "Y": d.pos[1], "Z": d.pos[2]},
            "NormalisedSplinePos": d.spline,
            "SteerAngle": 0, "DRSActive": False, "BlueFlag": False,
        }

    def leaderboard(self, now: float | None = None) -> dict:
        """The subset of ACSM's `leaderboard.json` that the league site and telemetry backend read."""
        s = self.session
        race = s.get("session_type") == RACE
        order = sorted(self.drivers, key=lambda d: (-d.laps, d.total_ms)) if race else []
        pos = {id(d): i + 1 for i, d in enumerate(order)}

        def group(connected: bool) -> list[dict]:
            return [self._entry(d, pos.get(id(d), 0)) for d in self.drivers if d.connected == connected]

        return {
            "ServerName": s.get("server_name", ""), "Track": s.get("track", ""), "TrackConfig": s.get("track_config", ""),
            "Name": s.get("name", ""), "Type": s.get("session_type", 0), "Time": s.get("time_min", 0),
            "Laps": s.get("laps", 0), "ElapsedMilliseconds": self.elapsed_ms(now),
            "AmbientTemp": s.get("ambient_temp"), "RoadTemp": s.get("road_temp"),
            "ConnectedDrivers": group(True), "DisconnectedDrivers": group(False),
        }
