"""A server's weather over time, in the shape of AC Server Manager's weather editor, plus a live source (a real place's weather from an API).

Two modes (`Server.weather_plan["mode"]`):
- `entries`: a list of weathers. Each says its WeatherFX type, how many real minutes it lasts before the next one (0 = until the session ends), which
  sessions (practice / qualify / race) it belongs to, temperatures and wind. Within a session they play one after the other from the session's start,
  each change a smooth blend that ends when the next entry begins. A session with no entry keeps the server's own `[WEATHER_n]` weather.
- `live`: the weather of a latitude/longitude, read from Open-Meteo every few minutes (no key needed): WMO code and cloud cover -> WeatherFX type, plus temperature,
  wind, humidity and pressure; changes of type blend over `transition_s`.
`Weather.step` returns the conditions CSP is sent (`cspweather.command`): rain intensity per type, wetness and puddles that build while it rains and dry slowly
(they matter for grip; CSP also computes its own from the type, see app/live/README.md), grip, humidity.
"""

from __future__ import annotations

import json
import random
import urllib.request
from dataclasses import dataclass, field

SESSIONS = {"practice": 1, "qualify": 2, "race": 3}   # ACSP session types
RAIN = {0: 0.6, 1: 0.8, 2: 1.0, 3: 0.12, 4: 0.25, 5: 0.4, 6: 0.35, 7: 0.6, 8: 0.9}   # intensity per WeatherFX type id (0 = dry)
OPEN_METEO = "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&wind_speed_unit=kmh&current=temperature_2m,relative_humidity_2m,pressure_msl,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m"


@dataclass
class Entry:
    type: int = 15
    duration_min: float = 0
    sessions: frozenset = frozenset({1, 2, 3})
    ambient: float = 20
    road: float = 6          # added to the ambient temperature (as in AC Server Manager); may be negative
    ambient_var: float = 0
    road_var: float = 0
    wind_min: float = 0      # m/s
    wind_max: float = 0
    wind_dir: float = 0
    wind_dir_var: float = 0

    @classmethod
    def from_dict(cls, d: dict) -> Entry:
        sessions = frozenset(SESSIONS[s] for s in d.get("sessions", list(SESSIONS)))
        return cls(**{**{k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "sessions"}, "sessions": sessions})


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class Timeline:
    """The entries of one session, one after the other. Each is held for its duration and blended into the next during the last `transition_s`."""

    def __init__(self, entries: list[Entry], transition_s: float, rng: random.Random) -> None:
        self.entries, self.transition_s = entries, transition_s
        self.starts, t = [], 0.0
        for e in entries:
            self.starts.append(t)
            t += e.duration_min * 60 if e.duration_min > 0 else float("inf")
        self.offsets = [(rng.uniform(-e.ambient_var, e.ambient_var), rng.uniform(-e.road_var, e.road_var), rng.uniform(-e.wind_dir_var, e.wind_dir_var))
                        for e in entries]   # drawn once per entry: the «variation» settings

    def at(self, t: float) -> tuple[int, int, float]:
        """(index of the current entry, of the next one, blend 0..1) `t` seconds into the session."""
        i = max(k for k, s in enumerate(self.starts) if s <= t)
        if i + 1 < len(self.entries):
            begin = max(self.starts[i], self.starts[i + 1] - self.transition_s)
            if t >= begin:
                return i, i + 1, min(1.0, (t - begin) / max(1.0, self.transition_s))
        return i, i, 0.0


def live_type(code: int, cloud: float) -> int:
    """WMO weather code (+ cloud cover %) -> WeatherFX type id."""
    if code in (51, 53, 55, 56, 57):
        return {51: 3, 53: 4, 55: 5, 56: 3, 57: 4}[code]
    if code in (61, 63, 65, 80, 81, 82):
        return {61: 6, 63: 7, 65: 8, 80: 6, 81: 7, 82: 8}[code]
    if code in (66, 67):
        return 13
    if code in (71, 73, 75, 77, 85, 86):
        return {71: 9, 73: 10, 75: 11, 77: 9, 85: 9, 86: 11}[code]
    if code in (95,):
        return 1
    if code in (96, 99):
        return 2
    if code in (45, 48):
        return 20
    return 15 if cloud < 10 else 16 if cloud < 35 else 17 if cloud < 60 else 18 if cloud < 85 else 19


def fetch_live(lat: float, lon: float, timeout: float = 8) -> dict | None:
    """The current weather of a place from Open-Meteo, as the values the director uses; None if the service does not answer."""
    try:
        with urllib.request.urlopen(OPEN_METEO.format(lat=lat, lon=lon), timeout=timeout) as r:
            c = json.load(r)["current"]
    except (OSError, ValueError, KeyError):
        return None
    t = live_type(int(c["weather_code"]), float(c["cloud_cover"]))
    return {"type": t, "ambient": float(c["temperature_2m"]), "wind_kmh": float(c["wind_speed_10m"]), "wind_deg": float(c["wind_direction_10m"]),
            "humidity": float(c["relative_humidity_2m"]) / 100, "pressure": float(c["pressure_msl"])}


@dataclass
class Weather:
    plan: dict
    t: float = 0.0
    session: int | None = None
    timeline: Timeline | None = None
    wetness: float = 0.0
    water: float = 0.0
    live: dict | None = None            # latest data of the live source
    wet_rate: float = 1 / 60            # per second while raining
    dry_rate: float = 1 / 600
    water_rate: float = 1 / 180
    grip_loss: float = 0.2              # grip lost at full wetness
    rng: random.Random = field(default_factory=random.Random)
    _cur: int | None = None             # live mode: the type shown now, the one it blends into and how far
    _up: int | None = None
    _tr: float = 0.0

    def __post_init__(self) -> None:
        self.mode = self.plan.get("mode", "entries")
        self.transition_s = float(self.plan.get("transition_s", 60))
        self.entries = [Entry.from_dict(e) for e in self.plan.get("entries", [])]

    def start_session(self, session: int | None) -> bool:
        """The session now running; builds its timeline. False when it has no entry (the server's own weather applies)."""
        if self.mode == "live":
            self.session = session
            return True
        if session != self.session or self.timeline is None:
            self.session = session
            es = [e for e in self.entries if session in e.sessions]
            self.timeline = Timeline(es, self.transition_s, self.rng) if es else None
        return self.timeline is not None

    def step(self, dt: float, elapsed_s: float = 0.0) -> dict | None:
        """The conditions now: `elapsed_s` into the session (entries mode) or `dt` after the last step (live mode, needs `live` data first)."""
        if self.mode == "live":
            s = self._live_state(dt)
        elif self.timeline is not None:
            s = self._entry_state(elapsed_s)
        else:
            return None
        if s is None:
            return None
        rain = RAIN.get(s["current"], 0.0) * (1 - s["transition"]) + RAIN.get(s["upcoming"], 0.0) * s["transition"]
        if rain > 0.05:
            self.wetness = min(1.0, self.wetness + self.wet_rate * dt * rain)
            self.water = min(1.0, self.water + self.water_rate * rain * dt)
        else:
            self.wetness = max(0.0, self.wetness - self.dry_rate * dt)
            self.water = max(0.0, self.water - self.water_rate / 4 * dt)
        return {**s, "rain": rain, "wetness": self.wetness, "water": self.water, "grip": 1.0 - self.grip_loss * self.wetness,
                "humidity": min(1.0, max(s.get("humidity") or 0.0, 0.5 + 0.4 * rain)), "pressure": s.get("pressure") or 1013.0}

    def _entry_state(self, t: float) -> dict:
        tl = self.timeline
        i, j, tr = tl.at(t)
        a, b = tl.entries[i], tl.entries[j]
        oa, ob = tl.offsets[i], tl.offsets[j]
        amb_a, amb_b = a.ambient + oa[0], b.ambient + ob[0]
        road_a, road_b = amb_a + a.road + oa[1], amb_b + b.road + ob[1]
        wind_a, wind_b = (a.wind_min + a.wind_max) / 2 * 3.6, (b.wind_min + b.wind_max) / 2 * 3.6   # m/s -> km/h
        return {"current": a.type, "upcoming": b.type, "transition": tr, "ambient": _lerp(amb_a, amb_b, tr), "road": _lerp(road_a, road_b, tr),
                "wind_kmh": _lerp(wind_a, wind_b, tr), "wind_deg": _lerp(a.wind_dir + oa[2], b.wind_dir + ob[2], tr)}

    def _live_state(self, dt: float) -> dict | None:
        d = self.live
        if not d:
            return None
        if self._cur is None:
            self._cur = self._up = d["type"]
        if self._tr == 0 and d["type"] != self._cur:
            self._up = d["type"]
            self._tr = 1e-6
        if self._tr > 0:
            self._tr = min(1.0, self._tr + dt / max(1.0, self.transition_s))
            if self._tr >= 1.0:
                self._cur, self._tr = self._up, 0.0
        road = d["ambient"] + (3 if RAIN.get(self._cur, 0) == 0 and self._cur in (15, 16) else -2 if RAIN.get(self._cur, 0) else 1)
        return {"current": self._cur, "upcoming": self._up if self._tr else self._cur, "transition": self._tr, "ambient": d["ambient"], "road": road,
                "wind_kmh": d["wind_kmh"], "wind_deg": d["wind_deg"], "humidity": d["humidity"], "pressure": d["pressure"]}
