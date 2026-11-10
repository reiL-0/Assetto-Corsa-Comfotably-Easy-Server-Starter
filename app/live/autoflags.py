"""The flags that need no director: the BLUE flag (a car about to lap you is close behind) and the DANGER warning (a stopped car ahead of you, N metres). SHADOW only: it decides and writes
down what it WOULD show; nothing reaches a screen until the contract carries it.

Plain state, no I/O: the monitor feeds `step` the field samples (app/live/field.py), the speeds (km/h), the cars in the pit lane and the track length (app/live/tracklen.py). Distances are
metres along the track; with no length (None) it decides nothing and says so (`unknown_length`). Behaviour, as agreed with the owner:
  * BLUE for car A about car B: B is a lap or more ahead of A in progress (B is about to lap A or already has) and is BEHIND A on the track within BLUE_ON_M; it goes away when B is farther than
    BLUE_OFF_M (hysteresis: it does not flicker) or B passed A. No maximum time. Any class: whoever is about to lap you.
  * DANGER for car D about stopped car S: S is ahead of D on the track within DANGER_M, D is racing. The distance goes down as D approaches (rounded to ROUND_M); it ends when D passes S or S moves.
Cars whose progress is not trustworthy (just re-anchored, stale, in the pit lane, the pace car) take no part. Decisions are only the CHANGES (open / update / close), kept in a bounded log.
"""

from __future__ import annotations

import itertools
from collections import deque

from app.live.field import Sample, ring

BLUE_ON_M, BLUE_OFF_M = 150.0, 250.0
DANGER_M, ROUND_M = 400.0, 10.0
STOP_KMH, STOP_S = 5.0, 3.0           # a car under 5 km/h for 3 s that was racing is a stopped car
MOVING_KMH = 30.0
TRUST_S, FRESH_S = 2.0, 1.5           # progress continuous for 2 s and updated within 1.5 s
LOG_MAX = 200
_uid = itertools.count(1)


class AutoFlags:
    def __init__(self) -> None:
        self.blue: dict[tuple[int, int], float] = {}     # (slower car A, faster car B) -> metres B is behind A
        self.danger: dict[tuple[int, int], int] = {}     # (driver D, stopped car S) -> metres, rounded
        self._moved: set[int] = set()
        self._still: dict[int, float] = {}
        self._gen: dict[int, int] = {}                   # car -> the generation of its slot when last seen (a slot that changed hands starts from nothing)
        self.log: deque[dict] = deque(maxlen=LOG_MAX)    # {t, kind, op: open|update|close, car, ref, m}
        self.sequence = 0  # monotonic count, independent of bounded display history
        self.unknown_length = False
        self.uid = next(_uid)                            # which session's engine this is: a driver's screen drops what it holds when this changes
        self.modes = {"blue": "live", "danger": "live"}   # `off` types are not decided at all
        self.last: float | None = None                   # monotonic time of the last look (the age of what is served)
        self.gen: dict[int, int] = {}                    # slot -> generation of the slot at the last look
        self.pace_ok = None                              # callable(behind, ahead, open_already) -> bool: the blue only shows when the car behind has the pace to catch the other (app/live/flagpace.py); None = no gate

    def reset(self) -> None:
        self.blue.clear(); self.danger.clear(); self._moved.clear(); self._still.clear(); self._gen.clear(); self.unknown_length = False

    def _note(self, now: float, kind: str, op: str, car: int, ref: int, m: float | None) -> None:
        self.sequence += 1
        self.log.append({"t": now, "kind": kind, "op": op, "car": car, "ref": ref, "m": m})

    def step(self, now: float, samples: dict[int, Sample], speeds: dict[int, float], in_pit: set[int], length: float | None, racing: bool) -> None:
        """`now` and the samples' stamps are on the same clock (time.monotonic)."""
        if length is None or not racing:
            self.unknown_length = length is None and racing
            for (a, b) in list(self.blue):
                self._note(now, "blue", "close", a, b, None)
            for (d, s) in list(self.danger):
                self._note(now, "danger", "close", d, s, None)
            self.blue.clear(); self.danger.clear()
            self._still.clear()
            return
        self.unknown_length = False
        self.last = now
        self.gen = {c: s.generation for c, s in samples.items()}
        for c, s in samples.items():   # a slot that changed hands between two looks: whatever we knew of its last driver is not the new one's
            if self._gen.get(c, s.generation) != s.generation:
                self._moved.discard(c); self._still.pop(c, None)
                for k in [k for k in self.blue if c in k]:
                    self._note(now, "blue", "close", k[0], k[1], None); del self.blue[k]
                for k in [k for k in self.danger if c in k]:
                    self._note(now, "danger", "close", k[0], k[1], None); del self.danger[k]
            self._gen[c] = s.generation
        for c in [c for c in self._gen if c not in samples]:
            del self._gen[c]
        ok = {c: s for c, s in samples.items() if c not in in_pit and now - s.stamp <= FRESH_S and now - s.since >= TRUST_S}
        for c, v in speeds.items():   # who is stopped on the track
            if v >= MOVING_KMH:
                self._moved.add(c); self._still.pop(c, None)
            elif v <= STOP_KMH and c in self._moved and c in ok:
                self._still.setdefault(c, now)
            else:
                self._still.pop(c, None)
        for c in [c for c in self._moved if c not in samples]:
            self._moved.discard(c); self._still.pop(c, None)
        stopped = {c for c, t in self._still.items() if now - t >= STOP_S}

        blue: dict[tuple[int, int], float] = {}
        for a, sa in ok.items():
            for b, sb in ok.items():
                if a == b or b in stopped:
                    continue
                lead = sb.progress - sa.progress            # B ahead of A by this much, in laps
                behind_m = (1.0 - lead % 1.0) % 1.0 * length  # metres B is behind A on the track
                if lead < 1.0 - BLUE_OFF_M / length:
                    continue
                on = (a, b) in self.blue
                if behind_m <= (BLUE_OFF_M if on else BLUE_ON_M) and (self.pace_ok is None or self.pace_ok(b, a, on)):
                    blue[(a, b)] = behind_m
        danger: dict[tuple[int, int], int] = {}
        for s in stopped & ok.keys():
            for d, sd in ok.items():
                if d == s or d in stopped or speeds.get(d, 0.0) < MOVING_KMH:
                    continue
                m = ring(ok[s].progress - sd.progress) * length   # metres S is ahead of D (negative: behind)
                if 0.0 <= m <= DANGER_M:
                    danger[(d, s)] = int(round(m / ROUND_M) * ROUND_M)
        self._diff("blue", self.blue, blue if self.modes["blue"] != "off" else {}, now)
        self._diff("danger", self.danger, danger if self.modes["danger"] != "off" else {}, now)

    def _diff(self, kind: str, old: dict, new: dict, now: float) -> None:
        for k, m in new.items():
            if k not in old:
                self._note(now, kind, "open", k[0], k[1], m)
            elif kind == "danger" and old[k] != m:
                self._note(now, kind, "update", k[0], k[1], m)
        for k in old.keys() - new.keys():
            self._note(now, kind, "close", k[0], k[1], None)
        old.clear(); old.update(new)

    def for_slot(self, slot: int, generation: int, now: float, live: set[str]) -> dict | None:
        """What this driver's screen may show: the NEAREST car about to lap it (blue) and the NEAREST stopped car ahead (danger), only the types in `live`. None when there is nothing, when the engine has not
        looked yet or when the slot has changed hands since the last look."""
        if self.last is None or self.gen.get(slot) != generation:
            return None
        blue = min(((m, a) for (c, a), m in self.blue.items() if c == slot), default=None) if "blue" in live else None
        danger = min(((m, s) for (d, s), m in self.danger.items() if d == slot), default=None) if "danger" in live else None
        if blue is None and danger is None:
            return None
        return {"blue": {"by": blue[1], "m": round(blue[0])} if blue else None, "danger": {"stopped": danger[1], "m": danger[0]} if danger else None,
                "age_ms": int((now - self.last) * 1000), "session": self.uid}

    def view(self) -> dict:
        """What the director's panel may show of the shadow: who WOULD have a flag now, and the last decisions."""
        return {"blue": [{"car": a, "by": b, "m": round(m)} for (a, b), m in self.blue.items()],
                "danger": [{"car": d, "stopped": s, "m": m} for (d, s), m in self.danger.items()],
                "unknown_length": self.unknown_length, "log": list(self.log)[-30:]}
