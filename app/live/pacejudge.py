"""Who breaks the pace of a VSC / FCY, judged CONTINUOUSLY and not once at the end of the grace.

Why: with no throttle lock in the car (the hub's lock is being retired) the rule is only worth what its judge is worth. A driver is in breach when his speed stays above `limit` (pace + tolerance) for
`SUSTAINED_S` seconds of fresh samples; one sample over the limit (a lost packet, a bump) is nothing, and a sample under it starts the count again. Slower than the pace is never a breach.

Plain state, no I/O: `RaceControl.pace_tick` feeds it the field samples (app/live/field.py: their `stamp` is when the car's packet arrived, so the same packet is never counted twice), the speeds (km/h)
and the cars that said they are in the pit lane. A car's count restarts when its progress was re-anchored (epoch), when its slot changed hands (generation), when the samples have a hole (MAX_GAP_S)
or are stale, and in the pit lane. One verdict per (flag, car, slot generation): taking the penalty back by hand does not arm it again. Decisions are only returned (and logged, bounded): applying
a penalty is the caller's.
"""

from __future__ import annotations

from collections import deque

from app.live.field import Sample

SUSTAINED_S = 1.5     # s of fresh samples over the limit that make a breach (first guess: to calibrate with real races)
MAX_GAP_S = 0.8       # a hole between two samples of a car (packets lost, a lag spike) restarts its count
FRESH_S = 1.5         # a sample older than this does not count
STANDING_KMH = 5.0    # a car that does not move is not "over" anything
LOG_MAX = 100


class PaceJudge:
    def __init__(self) -> None:
        self._over: dict[int, list] = {}          # car -> [epoch, generation, since, last stamp]: the count of a car that is over the limit now
        self._judged: set[tuple[int, int, int]] = set()   # (flag id, car, generation) already judged: only those of the flag being judged are kept
        self._flag = 0
        self.log: deque[dict] = deque(maxlen=LOG_MAX)

    def reset(self) -> None:
        self._over.clear(); self._judged.clear(); self.log.clear(); self._flag = 0

    def step(self, now: float, flag_id: int, active: bool, limit: float, samples: dict[int, Sample], speeds: dict[int, float], in_pit: set[int]) -> list[dict]:
        """One look. `active`: a VSC/FCY whose grace is over. `now` and the samples' stamps are the same clock. Returns the NEW breaches [{car, generation, speed_kmh, over_s}]."""
        if not active:
            self._over.clear()
            return []
        if flag_id != self._flag:
            self._flag = flag_id
            self._judged.clear()   # (a new flag: the verdicts of the old one are not needed any more)
        out = []
        for car in [c for c in self._over if c not in samples]:   # it left
            del self._over[car]
        for car, s in samples.items():
            v = speeds.get(car)
            st = self._over.get(car)
            if v is None or v != v or car in in_pit or now - s.stamp > FRESH_S or v < STANDING_KMH or v <= limit:   # no news, garbage, the pit lane, stale, standing or within the limit: the count starts again
                self._over.pop(car, None)
                continue
            if st is None or st[0] != s.epoch or st[1] != s.generation or s.stamp - st[3] > MAX_GAP_S:
                self._over[car] = [s.epoch, s.generation, s.stamp, s.stamp]   # a first sample over the limit, or one after a hole / re-anchoring: counting from here
                continue
            if s.stamp <= st[3]:
                continue   # the same packet again
            st[3] = s.stamp
            key = (flag_id, car, s.generation)
            if s.stamp - st[2] >= SUSTAINED_S and key not in self._judged:
                self._judged.add(key)
                d = {"car": car, "generation": s.generation, "speed_kmh": round(v), "over_s": round(s.stamp - st[2], 1)}
                out.append(d)
                self.log.append({"t": now, "flag": flag_id, **d, "limit": round(limit)})
        return out


class PaceDeltaJudge:
    """The delta of a car with no reference lap of its own (an FCY always, a VSC until its model has a clean lap) against a VIRTUAL CAR that holds `limit` km/h from the moment the judging started:
    delta = the time this car has taken - the distance it has covered / limit. Positive = slower than the virtual car (right), negative = faster. It is worked out from the car's own speed, so it needs no
    sector, no spline and no length of the track. The credit that can be banked is limited (`credit_s`, the same rule as app/live/sectors.py `DeltaJudge`: min(credit, delta + dt * (1 - v / limit))); a car
    whose limited delta falls under -`tol_s` is in breach, once per (flag, car, slot generation). Same inputs and same holes as `PaceJudge`; plain state, no I/O."""

    def __init__(self, tol_s: float = 0.2, credit_s: float = 3.0) -> None:
        self.tol_s, self.credit_s = tol_s, credit_s
        self._car: dict[int, dict] = {}   # car -> {epoch, generation, t: stamp of the last sample, delta}
        self._judged: set[tuple[int, int, int]] = set()
        self._flag = 0
        self.log: deque[dict] = deque(maxlen=LOG_MAX)

    def reset(self) -> None:
        self._car.clear(); self._judged.clear(); self.log.clear(); self._flag = 0

    def state(self, car: int) -> dict | None:
        c = self._car.get(car)
        return None if c is None else {"delta": round(c["delta"], 2)}

    def step(self, now: float, flag_id: int, active: bool, limit: float, samples: dict[int, Sample], speeds: dict[int, float], in_pit: set[int]) -> list[dict]:
        """`active`: a VSC/FCY whose grace is over. -> the NEW breaches [{car, generation, speed_kmh, delta_s}]."""
        if not active or not limit or limit <= 0:
            self._car.clear()
            return []
        if flag_id != self._flag:
            self._flag = flag_id
            self._judged.clear(); self._car.clear()
        out = []
        for car in [c for c in self._car if c not in samples]:
            del self._car[car]
        for car, s in samples.items():
            v = speeds.get(car)
            if v is None or v != v or car in in_pit or now - s.stamp > FRESH_S:   # no news, garbage, the pit lane or stale: nothing of before carries over
                self._car.pop(car, None)
                continue
            st = self._car.get(car)
            if st is None or st["epoch"] != s.epoch or st["generation"] != s.generation or s.stamp - st["t"] > MAX_GAP_S:
                self._car[car] = {"epoch": s.epoch, "generation": s.generation, "t": s.stamp, "delta": 0.0}   # the virtual car starts level with this one
                continue
            if s.stamp <= st["t"]:
                continue   # the same packet again
            st["delta"] = min(self.credit_s, st["delta"] + (s.stamp - st["t"]) * (1.0 - v / limit))
            st["t"] = s.stamp
            key = (flag_id, car, s.generation)
            if st["delta"] < -self.tol_s and key not in self._judged:
                self._judged.add(key)
                d = {"car": car, "generation": s.generation, "speed_kmh": round(v), "delta_s": round(st["delta"], 2)}
                out.append(d)
                self.log.append({"t": now, "flag": flag_id, **d, "limit": round(limit)})
        return out
