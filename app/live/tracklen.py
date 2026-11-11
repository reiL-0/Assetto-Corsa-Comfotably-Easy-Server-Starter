"""The length of the track in metres, for the flags that must say HOW FAR (the blue flag: how many metres the car behind is; the danger ahead: how far away it is).

ACSP does not send it. Two sources, in this order:
  1. `ui_track.json` of the track/layout in the manager's content, when it is there (the server pack the manager keeps usually has no `ui/`, so this is a bonus, not the plan);
  2. an ESTIMATE from the cars: the distance a car drives between two crossings of the line, from its speed (the speed samples are ~5 Hz, the error well under 1 %), the median of several clean laps.
Nothing is guessed: until there are at least three agreeing laps (`LapLength.MIN_LAPS`) the length is None, and whoever needs it stays off and says so.

Plain state, no I/O apart from reading one small JSON file (`from_ui_json`); tested in tests/test_tracklen.py.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path

MIN_M, MAX_M = 300.0, 40000.0   # a lap shorter or longer than this is not a lap (a kart track to the Nordschleife with room to spare)


def parse_length(value: object) -> float | None:
    """'5793', 5793, '5.8 km', '3,6 km', '4309 m' -> metres, or None when it is not a plausible track length. A bare number under 100 is read as kilometres (the mods that write '4.3')."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n, unit = float(value), ""
    elif isinstance(value, str):
        m = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(km|m)?\s*", value.lower())
        if not m:
            return None
        n, unit = float(m.group(1).replace(",", ".")), m.group(2) or ""
    else:
        return None
    metres = n * 1000.0 if unit == "km" else n if unit == "m" else (n * 1000.0 if n < 100 else n)
    return metres if MIN_M <= metres <= MAX_M else None


def from_ui_json(track_dir: Path, config: str | None) -> float | None:
    """The `length` in `ui/<config>/ui_track.json` (the layout) or `ui/ui_track.json` (the track), whichever is there. Never raises: a missing or broken file is None."""
    names = ([track_dir / "ui" / config / "ui_track.json"] if config else []) + [track_dir / "ui" / "ui_track.json"]
    for p in names:
        try:
            data = json.loads(p.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            n = parse_length(data.get("length"))
            if n is not None:
                return n
    return None


OVALS_KEY = "oval_tracks"       # Setting row: {"<track>/<layout>": true | false}: what the admin said about a layout, over everything else
DETECTED_KEY = "oval_detected"  # Setting row, same shape: what the cars' own laps showed (OvalShape), the last resort for a layout with no tags (a server pack has no ui/)


def oval_key(track: str | None, config: str | None) -> str:
    return f"{track or ''}/{config or ''}"


def oval_auto(track_dir: Path, config: str | None) -> bool | None:
    """What the `tags` of the layout's `ui_track.json` (or the track's) say: True with `oval`, False with other tags, None when there are no tags to read. Never raises."""
    for p in ([track_dir / "ui" / config / "ui_track.json"] if config else []) + [track_dir / "ui" / "ui_track.json"]:
        try:
            data = json.loads(p.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("tags"), list) and data["tags"]:
            return any(isinstance(x, str) and x.strip().lower() == "oval" for x in data["tags"])
    return None


def _setting(key: str) -> dict:
    from sqlmodel import Session
    from app.db import engine
    from app.models import Setting
    try:
        with Session(engine) as s:
            row = s.get(Setting, key)
        return dict(row.value) if row else {}
    except Exception:   # (no database: nothing is known)
        return {}


def oval_known(track: str | None, config: str | None) -> bool | None:
    """Is this layout an oval? The admin's word, else its tags, else what the cars' laps showed on an earlier session; None = nobody knows yet (the shape detector will say). Touches the disk and the
    database: OFF the event loop."""
    key = oval_key(track, config)
    for said in (_setting(OVALS_KEY).get(key),):
        if isinstance(said, bool):
            return said
    d = track_dir(track)
    tags = oval_auto(d, config) if d else None
    if tags is not None:
        return tags
    seen = _setting(DETECTED_KEY).get(key)
    return seen if isinstance(seen, bool) else None


def store_detected(track: str | None, config: str | None, oval: bool) -> None:
    """Remember what the shape detector found, for the next session on this layout. OFF the event loop."""
    from sqlmodel import Session
    from app.db import engine
    from app.models import Setting
    with Session(engine) as s:
        row = s.get(Setting, DETECTED_KEY) or Setting(key=DETECTED_KEY, value={})
        row.value = {**row.value, oval_key(track, config): oval}
        s.add(row); s.commit()


class LapLength:
    """Estimates the length from the cars. `observe` is called for every car_update with the car's unwrapped progress (app/live/field.py), its speed and the epoch/generation of that progress: a lap
    counts only if the car drove it WITHOUT the progress being re-anchored (a teleport, a lap-counter correction, a reconnection) and without a gap in the samples."""

    STOP_MS, STOP_S = 2.0, 3.0            # a car under 2 m/s for more than 3 s in a lap (a pit stop, a spin, the garage) did not drive the track: that lap does not count
    MIN_LAPS, SOLO_LAPS, KEEP, AGREE = 3, 5, 40, 0.04   # laps needed (and when only ONE car has driven them: more, so that a car lapping through the pits every time cannot set the length), laps remembered, how much the laps may differ from the median (4 %: the racing line is not the centre line)
    BACK = 0.003                          # a car whose progress goes BACK by more than this (of a lap, ~15 m on a 5 km track) reversed or spun round: its lap is not a lap (the speed is never negative, so the integral cannot tell)
    GAP_S = 1.0                           # a bigger hole between two samples of a car spoils the lap it is in
    TURN_MS = 8.0                         # the direction of travel is only read above 8 m/s (slower, the velocity vector is noise)
    OVAL_NET, OVAL_BACK = math.radians(300), math.radians(35)   # an oval lap turns ~360 degrees ONE way and never turns the other way by more than 35 (a tri-oval's dog-leg a few): any road layout has a chicane or a hairpin the other way

    def __init__(self) -> None:
        self._cars: dict[int, list] = {}   # car -> [epoch, generation, last_t, last_speed, lap_index, distance, clean, seconds stopped, last progress]
        self._laps: list[tuple[float, int]] = []   # (distance, car)
        self._shapes: list[bool] = []              # per whole lap: did it turn like an oval (see OVAL_NET / OVAL_BACK)

    def reset(self) -> None:
        self._cars.clear()
        self._laps.clear()
        self._shapes.clear()

    def forget(self, car: int) -> None:
        """The car left (its slot may be somebody else's): whatever lap it was in is not a lap."""
        self._cars.pop(car, None)

    def observe(self, car: int, progress: float, speed_ms: float, now: float, epoch: int, generation: int, heading: float | None = None) -> None:
        if not (speed_ms >= 0.0) or progress != progress:   # (a NaN or a negative speed is garbage)
            return
        lap = int(progress // 1)
        st = self._cars.get(car)
        if st is None or st[0] != epoch or st[1] != generation or now - st[2] > self.GAP_S or now < st[2]:
            self._cars[car] = [epoch, generation, now, speed_ms, lap, 0.0, False, 0.0, progress, []]   # (a lap that starts here is not whole: it counts from the next crossing)
            return
        dt = now - st[2]
        st[5] += 0.5 * (st[3] + speed_ms) * dt   # trapezoid
        if speed_ms < self.STOP_MS:
            st[7] += dt
        if heading is not None and speed_ms >= self.TURN_MS:   # (the direction of travel, unwrapped, for the shape of the lap)
            h = st[9]
            h.append(heading if not h else h[-1] + (heading - h[-1] + math.pi) % math.tau - math.pi)
        if progress < st[8] - self.BACK:
            st[6] = False   # it went backwards: this lap is not whole
        st[2], st[3], st[8] = now, speed_ms, max(progress, st[8]) if progress >= st[8] - self.BACK else st[8]
        if lap != st[4]:
            if lap == st[4] + 1 and st[6] and st[7] <= self.STOP_S and MIN_M <= st[5] <= MAX_M:
                self._laps.append((st[5], car))
                del self._laps[:-self.KEEP]
                if len(st[9]) > 20:
                    self._shapes.append(self._oval_lap(st[9]))
                    del self._shapes[:-self.KEEP]
            st[4], st[5], st[6], st[7], st[9] = lap, 0.0, lap == st[4] + 1, 0.0, []   # a new lap starts at the crossing: clean from here on, but only when the car went FORWARD over the line (backwards, or a jump of laps, is not a lap)

    def _oval_lap(self, h: list[float]) -> bool:
        s = 1.0 if h[-1] >= h[0] else -1.0
        peak, back = h[0] * s, 0.0
        for x in h:
            peak = max(peak, x * s)
            back = max(back, peak - x * s)   # how far it turned back from its furthest point
        return abs(h[-1] - h[0]) >= self.OVAL_NET and back <= self.OVAL_BACK

    @property
    def oval(self) -> bool | None:
        """True when at least `MIN_LAPS` whole laps turned like an oval and they are 3 of every 4 laps seen (a spin spoils one lap, not the verdict); False when at least `MIN_LAPS` laps were seen and
        3 of every 4 did not; None until then (the layout stays a road one: the danger warning works)."""
        n = len(self._shapes)
        if n < self.MIN_LAPS:
            return None
        yes = sum(self._shapes)
        return True if yes >= 0.75 * n else False if n - yes >= 0.75 * n else None

    @property
    def length(self) -> float | None:
        """The median of the laps that agree with it, or None until `MIN_LAPS` of them agree (`SOLO_LAPS` when they are all one car's)."""
        if len(self._laps) < self.MIN_LAPS:
            return None
        med = statistics.median(d for d, _ in self._laps)
        good = [(d, c) for d, c in self._laps if abs(d - med) <= self.AGREE * med]
        if len(good) < self.MIN_LAPS or (len({c for _, c in good}) < 2 and len(good) < self.SOLO_LAPS):
            return None
        return statistics.median(d for d, _ in good)


def track_dir(track: str | None) -> Path | None:
    """The folder of a track in the manager's content, or None. The name comes from the game server: only a plain folder name is looked up (never a path)."""
    if not track or not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,80}", track) or track in (".", ".."):
        return None
    from app import content   # (late: the content indexer reads settings at import)

    try:
        d = content._tracks_dir() / track
        return d if d.is_dir() else None
    except OSError:
        return None


def lookup(track: str | None, config: str | None) -> float | None:
    """What `new_session` needs from the disk, in one call (the folder of the track, then its ui_track.json): to be run OFF the event loop, it touches the disk."""
    d = track_dir(track)
    return from_ui_json(d, config) if d else None


class TrackLength:
    """What the rest of the manager asks: `metres()`. A configured length (the content's ui_track.json) wins over the estimate."""

    def __init__(self) -> None:
        self.configured: float | None = None
        self.estimator = LapLength()

    def reset(self) -> None:
        """A new session: it may be another track. The estimate starts again and the length that was configured is forgotten (the file of the new track is looked up by `ACSPClient`, off the event loop)."""
        self.estimator.reset()
        self.configured = None

    def metres(self) -> float | None:
        return self.configured if self.configured is not None else self.estimator.length

    def source(self) -> str:
        return "ui_track.json" if self.configured is not None else "estimated" if self.estimator.length is not None else "unknown"
