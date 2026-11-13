"""The VSC the way F1 runs it: a MINIMUM time per mini-sector (the lap cut in N = 20 equal parts of the spline) and a running DELTA the driver must keep from going negative.

Three small pieces of plain state, no I/O, fed by `RaceControl.sector_tick` from the field samples (app/live/field.py: progress that never wraps, `stamp` = when the packet arrived, `epoch` / `generation`):
  * `SectorClock`  turns the samples of a car into the instants (interpolated between two packets) at which it crossed each sector boundary, and says when its continuity was lost (a hole of packets, a
    re-anchored progress, a new driver in the slot, going backwards): whatever was being measured is then worthless.
  * `RefLaps`      watches the green racing and keeps, per car model, the sector times of the fastest CLEAN lap (every boundary seen, no pit lane, no hole). When a VSC starts, `profiles(factor)` freezes
    the minimum time per sector (factor x the best: 1.35 as in F1, a little over a third slower) for every model that has a lap; a model without one is judged by the fixed pace instead.
  * `DeltaJudge`   during a VSC, for a complete sector driven after the judging started: delta = min(credit, delta + time - minimum). Positive is slower than needed (right). A sector that ends with delta
    under -tol is a strike, one that ends above it clears them; `strikes` in a row are a breach, once per (flag, car, slot generation).
Resolution: packets arrive at ~5 Hz, so a crossing is known to a few hundredths of a second after interpolation; the delta is cumulative, so that error does not grow (what one sector over-reads the next
one under-reads), and `tol_s` absorbs the rest. The numbers (20 sectors, 1.35, 0.2 s, 2 strikes) are first guesses to calibrate with real races.
"""

from __future__ import annotations

import math
from collections import deque

N = 20                 # sectors per lap
GAP_S = 0.8            # a hole between two packets of a car breaks its continuity
MIN_LAP_S = 15.0       # a "lap" shorter than this is not a lap (a re-anchoring that looked clean)
LOG_MAX = 100


class SectorClock:
    def __init__(self) -> None:
        self._cars: dict[int, list] = {}   # car -> [epoch, generation, last t, last progress]

    def reset(self) -> None:
        self._cars.clear()

    def forget(self, car: int) -> None:
        self._cars.pop(car, None)

    def observe(self, car: int, progress: float, t: float, epoch: int, generation: int) -> list[tuple]:
        """-> events in order: ("reset",) when continuity was lost (this packet starts a new stretch), ("cross", b, t_b) for every boundary b (progress b / N) crossed since the last packet."""
        st = self._cars.get(car)
        if progress != progress or t != t:
            self._cars.pop(car, None)
            return [("reset",)]
        if st is None or st[0] != epoch or st[1] != generation or t - st[2] > GAP_S or t <= st[2] or progress < st[3]:
            first = st is None
            self._cars[car] = [epoch, generation, t, progress]
            return [] if first else [("reset",)]
        out: list[tuple] = [("cross", b, st[2] + (b / N - st[3]) / (progress - st[3]) * (t - st[2])) for b in range(math.floor(st[3] * N) + 1, math.floor(progress * N) + 1)] if progress > st[3] else []
        st[2], st[3] = t, progress
        return out


KEEP_LAPS = 10         # the clean laps kept per (track layout, car model): the window the reference is chosen from
MIN_WINDOW = 3         # with fewer laps than this there is no average to compare with: the plausible ones count
OUTLIER_RATIO = 0.85   # a lap this much quicker than the MEDIAN of the window (15 %) is not a reference: a teleport, a shortcut, a spline that jumped
MAX_AVG_MS = 100.0     # m/s: a lap whose average speed over the track's length is higher than this (360 km/h) cannot be real


def reference(laps: list[tuple[float, list[float]]]) -> tuple[float, list[float]] | None:
    """The reference of a window of laps [(lap s, the N sector times s)]: the quickest lap that is not an outlier (a lap under 85 % of the median of the window is left out; with fewer than 3 laps all count)."""
    if not laps:
        return None
    ok = laps
    if len(laps) >= MIN_WINDOW:
        ordered = sorted(lap for lap, _ in laps)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        ok = [x for x in laps if x[0] >= OUTLIER_RATIO * median] or laps
    return min(ok, key=lambda x: x[0])


class RefLaps:
    def __init__(self) -> None:
        self._cross: dict[int, dict[int, float]] = {}   # car -> boundary -> instant, since the last reset / pit lane
        self.recent: dict[str, list[tuple[float, list[float]]]] = {}   # car model -> its last KEEP_LAPS clean laps (lap s, the N sector times s): what the reference is chosen from
        self.fresh: dict[str, list[tuple[float, list[float]]]] = {}    # laps learned here and not in the database yet (app/vscrefs.py)
        self.best: dict[str, tuple[float, list[float]]] = {}           # car model -> (lap time s, the N sector times s) of its reference lap: the quickest that is no outlier of `recent`
        self.track_m: float | None = None                              # the track's length in metres when known: a lap too quick for it is not a lap

    def reset(self) -> None:
        self._cross.clear(); self.recent.clear(); self.fresh.clear(); self.best.clear()

    def add_lap(self, model: str, lap: float, secs: list[float], fresh: bool = True) -> None:
        if self.track_m and self.track_m / lap > MAX_AVG_MS:   # (physically impossible: 20 s on a 1:20 track)
            return
        w = self.recent.setdefault(model, [])
        w.append((lap, secs)); del w[:-KEEP_LAPS]
        if fresh:
            self.fresh.setdefault(model, []).append((lap, secs))
        ref = reference(w)
        if ref:
            self.best[model] = ref

    def feed(self, car: int, model: str | None, events: list[tuple], in_pit: bool) -> None:
        if in_pit:   # (also with no crossing in this packet: a lap through the pit lane is never a reference)
            self._cross.pop(car, None)
            return
        for ev in events:
            if ev[0] == "reset":
                self._cross.pop(car, None)
                continue
            _, b, t = ev
            c = self._cross.setdefault(car, {})
            c[b] = t
            if b % N == 0 and model and all(k in c for k in range(b - N, b + 1)):
                lap = c[b] - c[b - N]
                if lap >= MIN_LAP_S:
                    self.add_lap(model, lap, [c[k + 1] - c[k] for k in range(b - N, b)])
            for k in [k for k in c if k < b - N]:
                del c[k]

    def merge(self, known: dict[str, list[tuple[float, list[float]]]]) -> None:
        """The windows kept in the database: the laps learned here since (not saved yet) go after them, and the reference is chosen again."""
        for model, laps in known.items():
            w = (list(laps) + self.fresh.get(model, []))[-KEEP_LAPS:]
            self.recent[model] = w
            ref = reference(w)
            if ref:
                self.best[model] = ref

    def take_fresh(self) -> dict[str, list[tuple[float, list[float]]]]:
        out, self.fresh = self.fresh, {}
        return out

    def carry_from(self, old: "RefLaps") -> None:
        """A new session on the same track: the windows the old one knew. Its laps not saved yet stay with it (the monitor's worker saves them from the old state or, at most a second of laps, they are lost: carrying
        them too would save them twice, once from each state)."""
        self.recent = {m: list(w) for m, w in old.recent.items()}
        self.best = dict(old.best)

    def forget(self, car: int) -> None:
        self._cross.pop(car, None)

    def profiles(self, factor: float) -> dict[str, list[int]]:
        """The minimum time in ms of each sector, per car model that has a clean lap: factor x its best sector times."""
        return {m: [round(s * factor * 1000) for s in secs] for m, (_, secs) in self.best.items()}


class DeltaJudge:
    def __init__(self, tol_s: float = 0.2, credit_s: float = 3.0, strikes: int = 2) -> None:
        self.tol_s, self.credit_s, self.need = tol_s, credit_s, strikes
        self._car: dict[int, dict] = {}   # car -> {delta, strikes, prev: (boundary, t) | None, since}
        self._judged: set[tuple[int, int, int]] = set()
        self._flag = 0
        self.log: deque[dict] = deque(maxlen=LOG_MAX)

    def reset(self) -> None:
        self._car.clear(); self._judged.clear(); self.log.clear(); self._flag = 0

    def state(self, car: int) -> dict | None:
        """{delta, strikes, sector} of a car being judged (what a screen would show); None if it is not."""
        c = self._car.get(car)
        return None if c is None else {"delta": round(c["delta"], 2), "strikes": c["strikes"]}

    def feed(self, now: float, flag_id: int, active: bool, since: float, car: int, generation: int, profile: list[int] | None, events: list[tuple], in_pit: bool, need: int | None = None) -> list[dict]:
        """`active`: a VSC whose grace is over. `since`: the instant (same clock as the crossings) from which sectors count. `profile`: the minimum ms per sector of this car's model, None = not judged here.
        `need`: the warnings (sectors under the limit in a row) that make a breach (the director sets it; default 2). A breach has two levels: the first one at `need` warnings; the warnings are then spent
        (the count starts again) and `need` more in a row make the second, severe one (`level` 2). Each level once per (flag, car, slot generation).
        -> the NEW breaches [{car, generation, delta_s, strikes, level}]."""
        need = need or self.need
        if flag_id != self._flag:
            self._flag = flag_id
            self._judged.clear(); self._car.clear()
        if not active or profile is None or len(profile) != N:
            self._car.pop(car, None)
            return []
        c = self._car.get(car)
        if c is None or c["generation"] != generation:   # (a new driver in the slot starts with no warnings spent)
            c = self._car[car] = {"delta": 0.0, "strikes": 0, "prev": None, "level": 0, "generation": generation}
        if in_pit:   # (also with no crossing in this packet: nothing of before the pit lane carries over)
            c.update(prev=None, delta=0.0, strikes=0)
            return []
        out = []
        for ev in events:
            if ev[0] == "reset":
                c["prev"] = None; c["delta"] = 0.0; c["strikes"] = 0   # the stretch is broken: nothing carries over
                continue
            _, b, t = ev
            prev, c["prev"] = c["prev"], (b, t)
            if prev is None or prev[0] != b - 1 or prev[1] < since:
                continue   # not a complete sector driven since the judging started
            dt = t - prev[1]
            c["delta"] = min(self.credit_s, c["delta"] + dt - profile[(b - 1) % N] / 1000.0)
            c["strikes"] = c["strikes"] + 1 if c["delta"] < -self.tol_s else 0
            key = (flag_id, car, generation)
            if c["strikes"] >= need and c["level"] < 2 and (*key, c["level"] + 1) not in self._judged:
                level = c["level"] + 1
                self._judged.add((*key, level))
                d = {"car": car, "generation": generation, "delta_s": round(c["delta"], 2), "strikes": c["strikes"], "level": level}
                c["level"], c["strikes"] = level, 0   # the warnings are spent: the next breach needs `need` more
                out.append(d)
                self.log.append({"t": now, "flag": flag_id, **d})
        return out
