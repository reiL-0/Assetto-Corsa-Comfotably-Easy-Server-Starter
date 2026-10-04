"""Live timing state built from ACSP events: who is on track, their laps, and where they are."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

NS = 1_000_000  # ACSM reports lap times in nanoseconds; ACSP gives milliseconds
NO_TIME = 2**31  # acServer's "no lap yet" values are huge sentinels (0xFFFFFFFF, 999999999...)
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
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    spline: float = 0.0


class LiveBoard:
    """Fed by `ACSPClient._apply`; read by `app.live.acsm` (ACSM-compatible JSON)."""

    def __init__(self) -> None:
        self.session: dict = {}  # last new_session / session_info event
        self.session_at = 0.0  # wall clock when it arrived: elapsed time keeps running from there
        self.drivers: list[Driver] = []

    def _by_car(self, car_id: int) -> Driver | None:
        """Newest driver that used this car slot (slots are reused by whoever joins next)."""
        return next((d for d in reversed(self.drivers) if d.car_id == car_id and d.connected), None)

    def apply(self, e: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        t = e["type"]
        if t in ("new_session", "session_info"):
            self.session, self.session_at = e, now
            if t == "new_session":  # each session starts its own table; whoever is still connected stays
                self.drivers = [d for d in self.drivers if d.connected]
                for d in self.drivers:
                    d.best_ms = d.last_ms = d.laps = d.total_ms = 0
                    d.top_kmh = 0.0
        elif t == "new_connection":
            old = next((d for d in self.drivers if e["driver_guid"] and d.guid == e["driver_guid"]), None)
            d = old or Driver(car_id=e["car_id"])
            d.car_id, d.connected = e["car_id"], True
            d.name, d.guid = e["driver_name"], e["driver_guid"]
            d.model, d.skin = e["car_model"], e["car_skin"]
            if not old:
                self.drivers.append(d)
        elif t == "connection_closed":
            d = self._by_car(e["car_id"])
            if d:
                d.connected = False
        elif t == "car_update":
            d = self._by_car(e["car_id"])
            if d:
                d.pos, d.spline = tuple(e["pos"]), e["spline_pos"]
                d.top_kmh = max(d.top_kmh, math.hypot(*e["velocity"]) * 3.6)  # ponytail: top speed of the session, ACSM gives it for the best lap
        elif t == "lap_completed":
            d = self._by_car(e["car_id"])
            if d:
                d.last_ms, d.laps = e["laptime_ms"], d.laps + 1
                d.total_ms += e["laptime_ms"]
            for row in e["leaderboard"]:  # the server's own table: best lap and lap count of every car
                r = self._by_car(row["car_id"])
                if r:
                    r.laps = max(r.laps, row["laps"])
                    if 0 < row["laptime_ms"] < NO_TIME:
                        r.best_ms = row["laptime_ms"]

    def elapsed_ms(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return int(self.session.get("elapsed_ms", 0) + max(0.0, now - self.session_at) * 1000)

    def _entry(self, d: Driver, position: int) -> dict:
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
                    "BestLap": d.best_ms * NS, "LastLap": d.last_ms * NS, "NumLaps": d.laps,
                    "TotalLapTime": d.total_ms * NS, "BestLapSplits": {}, "TopSpeedBestLap": round(d.top_kmh),
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
