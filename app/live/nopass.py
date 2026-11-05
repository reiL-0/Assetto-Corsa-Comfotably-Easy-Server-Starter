"""No overtaking under a VSC or an FCY: a car that passes another one while the neutralisation runs must give the place back, in time, or it gets the flag's penalty (the drive-through).

Plain state, no I/O: `RaceControl.pace_tick` feeds it the field samples (app/live/field.py: progress that never wraps, `stamp`, `epoch`, `generation`), the speeds and the cars in the pit lane, only while a
VSC / FCY is running and its grace is over. It DECIDES (ids, never names) and returns events; the adapter turns them into banners and the penalty.

How a pass is read:
  * two cars within CLOSE of a lap are compared; one is AHEAD of the other when it is more than MARGIN of a lap in front (side by side is neither). A pass is the order turning round and STAYING turned
    for HOLD_S (a car that goes by for a moment, a lag spike, two cars side by side that swap in the packets: no case).
  * the order the pair had is only trusted while both cars keep their continuity (same `epoch` and `generation`: a teleport, a reconnection or a new driver in the slot forgets it).
  * not a case: the passed car is slow or stopped (under SLOW_KMH: it is an obstacle, passing it is right), either car is in the pit lane, or a car that left.
  * the passer then owes the place for `window_s` seconds. It gave it back when it is behind the car it passed by more than MARGIN for BACK_HOLD s; a passed car that stops, pits or leaves releases it. Once the
    time is out with creditors left, `due`: the penalty, once per car and flag.
The numbers are first guesses to calibrate with real races (the progress is a spline, a few metres wide: MARGIN is about 16 m on a 4 km track).
"""

from __future__ import annotations

import itertools
from collections import deque

from app.live.field import Sample, ring

CLOSE = 0.03          # of a lap: cars further apart than this are not compared
MARGIN = 0.004        # of a lap: in front or behind by more than this, not side by side
HOLD_S = 1.0          # s the new order has to last to be a pass
BACK_HOLD = 2.0       # s behind the passed car that count as having given the place back
SLOW_KMH = 40.0       # the passed car this slow is an obstacle: no case
FRESH_S = 1.5
WINDOW_S = 30.0       # s to give the place back
LOG_MAX = 100
_IDS = itertools.count(1)


class PassJudge:
    def __init__(self, window_s: float = WINDOW_S) -> None:
        self.window_s = window_s
        self._rel: dict[tuple[int, int], list] = {}      # (a, b) a < b -> [sign of (a - b), (epoch a, gen a, epoch b, gen b), pending sign, pending since]
        self._owe: dict[int, dict] = {}                  # passer -> {id, creditors: {car: back_since | None}, due}
        self._punished: set[tuple[int, int, int]] = set()   # (flag id, car, generation)
        self._flag = 0
        self.log: deque[dict] = deque(maxlen=LOG_MAX)

    def reset(self) -> None:
        self._rel.clear(); self._owe.clear(); self._punished.clear(); self.log.clear(); self._flag = 0

    def open(self) -> list[dict]:
        """The places owed now: [{id, car, creditors, left_s?}] for the director's view."""
        return [{"id": o["id"], "car": c, "creditors": sorted(o["creditors"])} for c, o in self._owe.items()]

    def step(self, now: float, flag_id: int, active: bool, samples: dict[int, Sample], speeds: dict[int, float], in_pit: set[int]) -> list[tuple]:
        """-> events: ("open", passer, [passed...], seconds), ("cleared", passer), ("due", passer, generation)."""
        if flag_id != self._flag:
            self._flag = flag_id
            self._rel.clear(); self._owe.clear(); self._punished.clear()
        if not active:
            self._rel.clear(); self._owe.clear()
            return []
        out: list[tuple] = []
        ok = {c: s for c, s in samples.items() if c not in in_pit and now - s.stamp <= FRESH_S}
        for key in [k for k in self._rel if k[0] not in ok or k[1] not in ok]:
            del self._rel[key]   # a car left, pitted or went quiet: what the pair had is forgotten
        ids = sorted(ok)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                d = ring(ok[a].progress - ok[b].progress)   # (on the track, whatever the laps between them: two cars a lap apart that run side by side ARE side by side)
                if abs(d) > CLOSE:
                    self._rel.pop((a, b), None)
                    continue
                sign = 1 if d > MARGIN else -1 if d < -MARGIN else 0
                ident = (ok[a].epoch, ok[a].generation, ok[b].epoch, ok[b].generation)
                r = self._rel.get((a, b))
                if r is None or r[1] != ident:
                    self._rel[(a, b)] = [sign, ident, 0, 0.0] if sign else [0, ident, 0, 0.0]   # (a pair seen for the first time or after a break: the order starts here)
                    continue
                if sign == 0 or sign == r[0] or r[0] == 0:
                    if sign and r[0] == 0:
                        r[0] = sign
                    r[2], r[3] = 0, 0.0
                    continue
                if r[2] != sign:
                    r[2], r[3] = sign, now   # the order turned round: it has to stay so for HOLD_S
                    continue
                if now - r[3] < HOLD_S:
                    continue
                r[0], r[2], r[3] = sign, 0, 0.0
                passer, passed = (a, b) if sign > 0 else (b, a)
                if speeds.get(passed, 99.0) < SLOW_KMH:
                    continue   # (an obstacle: it was right to pass it)
                if passer in self._owe.get(passed, {}).get("creditors", {}):
                    continue   # (the place is being given back: the creditor going by again is no new pass)
                o = self._owe.get(passer)
                if o is None:
                    o = self._owe[passer] = {"id": next(_IDS), "creditors": {}, "due": now + self.window_s, "gen": ok[passer].generation, "gens": {}}
                    new = True
                else:
                    new = passed not in o["creditors"]
                o["creditors"][passed] = None
                o["gens"][passed] = ok[passed].generation
                if new:
                    out.append(("open", passer, sorted(o["creditors"]), int(self.window_s)))
        for passer in list(self._owe):   # the obligations
            o = self._owe[passer]
            if passer not in ok or ok[passer].generation != o["gen"]:
                del self._owe[passer]   # the passer left or pitted: nothing to give back (a pit lane is its own rule)
                continue
            for cred in list(o["creditors"]):
                if cred not in ok or speeds.get(cred, 99.0) < SLOW_KMH or ok[cred].generation != o["gens"].get(cred):
                    del o["creditors"][cred]   # the creditor left, pitted or stopped
                    continue
                behind = ring(ok[cred].progress - ok[passer].progress) > MARGIN   # the creditor is ahead of the passer again (however far: dropping well back is giving it back too)
                since = o["creditors"][cred]
                if behind:
                    o["creditors"][cred] = since if since is not None else now
                    if now - o["creditors"][cred] >= BACK_HOLD:
                        del o["creditors"][cred]
                else:
                    o["creditors"][cred] = None
            if not o["creditors"]:
                del self._owe[passer]
                out.append(("cleared", passer))
            elif now >= o["due"]:
                key = (flag_id, passer, ok[passer].generation)
                del self._owe[passer]
                if key not in self._punished:
                    self._punished.add(key)
                    self.log.append({"t": now, "flag": flag_id, "car": passer, "creditors": sorted(o["creditors"])})
                    out.append(("due", passer, ok[passer].generation))
        return out
