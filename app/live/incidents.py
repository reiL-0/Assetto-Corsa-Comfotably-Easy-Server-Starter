"""Does the race need a caution? Looks at the stopped cars and makes a PROPOSAL for the director (or, when the director asked for it, the yellow itself).

Plain state, no I/O (`ACSPClient` feeds the contacts, the monitor calls `step`). Three things call for a caution:
  * a car STOPPED on the track: it was moving (so it is racing, not a spectator or a car in the garage), it has been under STOP_KMH for STOP_S seconds and it is not in the pit lane.
  * several cars BRAKED to a near stop together (a wreck that stops cars): DROP_CARS cars, each fast (FAST_KMH) in the last DROP_WITHIN s and now under DROP_KMH for DROP_S s, outside the pit lane. ONE car is enough if it stays that way SPIN_S s (it spun out, nobody else needed) or braked like that within SPIN_WITHIN s of a contact.
  * a LIMPING car: it keeps moving but under SLOW_FRAC of the pace of the others (their median, at least SLOW_REF_KMH) for SLOW_LIMP_S seconds, outside the pit lane (a damaged car).
A contact is NEVER a reason for a caution by itself (in NASCAR cars touch all the time: a pile-up is cars that stop or limp). The cars in a contact are `involved`: they are the ones the
free pass skips and that restart at the tail (app/live/caution.py). The speed unit of the contact events is not verified against a real server (km/h assumed, like app/stewards/detectors.py),
and a contact is reported 1.5-2.5 s late: these numbers are first guesses to tune with real races.
"""

from __future__ import annotations

import math
import statistics

CONTACT_KMH = 20.0   # a contact slower than this does not make a car 'involved'
STOP_KMH, STOP_S = 5.0, 6.0
MOVING_KMH = 30.0    # a car that has been this fast is racing: the one that never was (a spectator, a car in the garage) is not a stopped car
SLOW_FRAC, SLOW_REF_KMH, SLOW_LIMP_S, SLOW_MIN_CARS = 0.4, 100.0, 12.0, 3   # a limping car: under 40 % of the median of the others (which must be doing 100+, at least 3 of them) for 12 s
FAST_KMH, DROP_KMH, DROP_S, DROP_WITHIN, DROP_CARS = 80.0, 20.0, 2.5, 8.0, 2   # several cars braked to a near stop together
SPIN_S, SPIN_WITHIN = 4.0, 10.0   # one car that braked to a near stop: it spun out after SPIN_S s, or after DROP_S s if it was in a contact in the last SPIN_WITHIN s (a contact is reported 1.5-2.5 s late)
PROPOSAL_FOR = 60.0  # s a proposal stays up if nobody acts on it


class Watch:
    def __init__(self) -> None:
        self.involved: dict[int, float] = {}    # car id -> when it was last in a contact
        self._moved: set[int] = set()
        self._still: dict[int, float] = {}       # car id -> since when it has been (nearly) stopped
        self._flagged: set[int] = set()          # stopped cars already proposed (until they move again)
        self._limp: dict[int, float] = {}        # car id -> since when it has been far under the pace of the others
        self._limp_flagged: set[int] = set()     # limping cars already proposed (until they are up to pace again)
        self._fast_at: dict[int, float] = {}     # car id -> when it was last fast (FAST_KMH)
        self._slow_since: dict[int, float] = {}  # car id -> since when it has been under DROP_KMH after having been fast
        self._drop_flagged: set[int] = set()     # cars already proposed in a mass stop (until they are up to speed again)
        self.proposal: dict | None = None
        self._next_id = 1

    def on_contact(self, now: float, car: int, other: int | None, speed: float) -> None:
        if speed < CONTACT_KMH:
            return
        for c in (car, other):
            if c is not None:
                self.involved[c] = now

    def step(self, now: float, speeds: dict[int, float], in_pit: set[int], racing: bool) -> dict | None:
        """One look at the track. Returns a NEW proposal (also kept in `self.proposal`) or None. `racing`: the race is on and under green."""
        if self.proposal and now - self.proposal["t"] > PROPOSAL_FOR:
            self.proposal = None
        for c, v in speeds.items():
            if v >= MOVING_KMH:
                self._moved.add(c)
                self._still.pop(c, None)
                self._flagged.discard(c)
            elif v <= STOP_KMH and c in self._moved and c not in in_pit:
                self._still.setdefault(c, now)
            else:
                self._still.pop(c, None)
        for c, v in speeds.items():
            if v >= FAST_KMH:
                self._fast_at[c] = now
            if v >= DROP_KMH or c in in_pit:
                self._slow_since.pop(c, None)
                self._drop_flagged.discard(c)
            elif c not in self._slow_since and now - self._fast_at.get(c, -1e9) <= DROP_WITHIN:
                self._slow_since[c] = now
        for c in [c for c in self._fast_at if c not in speeds]:   # it left: the slot may be somebody else's
            self._fast_at.pop(c)
            self._slow_since.pop(c, None)
            self._drop_flagged.discard(c)
        for c in [c for c in self._moved if c not in speeds]:   # it left the server: the slot may be somebody else's now
            self._moved.discard(c)
            self._flagged.discard(c)
            self._still.pop(c, None)
        for c in [c for c in self._limp if c not in speeds]:
            self._limp.pop(c)
        self._limp_flagged &= speeds.keys()
        for c, v in speeds.items():   # (the pace of the others: the median -- the lower one -- of the cars racing, not in the pit lane, not stopped; kept up to date under a caution too, so a recovered car can be proposed again)
            others = sorted(x for k, x in speeds.items() if k != c and k not in in_pit and x >= MOVING_KMH)
            pace = others[(len(others) - 1) // 2] if len(others) >= SLOW_MIN_CARS else 0.0
            if racing and c in self._moved and c not in in_pit and v > STOP_KMH and pace >= SLOW_REF_KMH and v < SLOW_FRAC * pace:
                self._limp.setdefault(c, now)
            else:
                self._limp.pop(c, None)
                if pace == 0.0 or v >= SLOW_FRAC * pace:
                    self._limp_flagged.discard(c)
        if not racing:
            self._slow_since.clear()
            self._fast_at.clear()   # (a car that braked under the caution or the formation did not brake in the race)
            self._still.clear()   # (the formation lap and the cautions do not count: the six seconds start when the race is on)
            return None
        for c, since in self._limp.items():
            if now - since >= SLOW_LIMP_S and c not in self._limp_flagged:
                self._limp_flagged.add(c)
                return self._propose(now, "limping", [c], "Coche lento en pista")
        down = [c for c, t in self._slow_since.items() if now - t >= DROP_S and c not in self._drop_flagged]
        alone = [c for c in down if now - self._slow_since[c] >= SPIN_S or now - self.involved.get(c, -1e9) <= SPIN_WITHIN]
        if len(down) >= DROP_CARS or alone:
            self._drop_flagged.update(down)
            return self._propose(now, "mass_stop", sorted(down), f"Varios coches frenados en pista: {len(down)}" if len(down) > 1 else "Coche trompeado en pista")
        for c, since in self._still.items():
            if now - since >= STOP_S and c not in self._flagged:
                self._flagged.add(c)
                return self._propose(now, "stopped", [c], "Coche parado en pista")
        return None

    def _propose(self, now: float, kind: str, cars: list[int], text: str) -> dict:
        self.proposal = {"id": self._next_id, "kind": kind, "cars": cars, "text": text, "t": now}
        self._next_id += 1
        return self.proposal

    def clear(self) -> None:
        """A caution is out (whoever put it): the proposal is answered and the stopped cars are not proposed again until they move."""
        self.proposal = None
        self._flagged.update(self._still)
        self._drop_flagged.update(self._slow_since)
        self._limp_flagged.update(self._limp)

    def recent_involved(self, now: float, within: float) -> set[int]:
        return {c for c, t in self.involved.items() if now - t < within}


RECOVERY_TICKS = 2  # consecutive fresh recovered samples before a neutralization car rearms


class NeutralizationWatch:
    """Session-owned threshold state; only fresh samples advance sustained timers."""

    def __init__(self):
        self.state = {}  # car -> [identity, last stamp, was moving, stopped since, slow since]
        self.events = {}  # car -> (confirmed at, kind), bounded by current slots and window
        self.flagged = set()
        self.recovered = {}  # car -> consecutive fresh recovered samples
        self.last_pileup = None  # (time, involved cars), suppress overlapping incidents within the window

    def step(self, now, samples, speeds, in_pit, racing, rule):
        if not racing or rule.get("mode", "off") == "off":
            self.state.clear(); self.events.clear(); self.flagged.clear()
            self.recovered.clear(); self.last_pileup = None
            return None
        fresh = {c: s for c, s in samples.items() if c not in in_pit
                 and 0 <= now - s.stamp <= rule["stale_telemetry_s"]
                 and c in speeds and math.isfinite(speeds[c])}
        for c in list(self.state):
            if c not in fresh:
                self.state.pop(c); self.events.pop(c, None); self.flagged.discard(c)
                self.recovered.pop(c, None)
        new = []
        for c, sample in fresh.items():
            v = speeds[c]
            identity = (sample.generation, sample.epoch)
            st = self.state.get(c)
            if st is None or st[0] != identity or sample.stamp - st[1] > rule["stale_telemetry_s"]:
                st = [identity, sample.stamp, False, None, None]
                self.events.pop(c, None); self.flagged.discard(c)
                self.recovered.pop(c, None)
                self.state[c] = st
            elif sample.stamp <= st[1]:
                continue
            others = [speeds[k] for k in fresh if k != c and speeds[k] >= MOVING_KMH]
            pace = statistics.median(others) if others else 0
            if v >= MOVING_KMH:
                st[2] = True
            stopped = st[2] and v <= STOP_KMH
            slow = st[2] and v > STOP_KMH and pace > 0 and v < pace * rule["slow_car"]["pace_ratio"]
            for idx, active in ((3, stopped), (4, slow)):
                st[idx] = (sample.stamp if st[idx] is None else st[idx]) if active else None
            st[1] = sample.stamp
            kind = "spin" if st[3] is not None and sample.stamp - st[3] >= rule["spin"]["stopped_s"] else (
                "slow_car" if st[4] is not None and sample.stamp - st[4] >= rule["slow_car"]["sustained_s"] else None)
            if not stopped and not slow:
                self.recovered[c] = min(RECOVERY_TICKS, self.recovered.get(c, 0) + 1)
                if self.recovered[c] >= RECOVERY_TICKS:
                    self.flagged.discard(c)
            else:
                self.recovered.pop(c, None)
            if kind and c not in self.flagged:
                self.flagged.add(c)
                # Retain the original evidence: recovery cannot restart an incident's window.
                if c not in self.events or now - self.events[c][0] > rule["pileup"]["window_s"]:
                    self.events[c] = (now, kind)
                    new.append(c)
        self.events = {c: e for c, e in self.events.items() if c in fresh and now - e[0] <= rule["pileup"]["window_s"]}
        if not new:
            return None
        cars = sorted(self.events)
        pileup = len(cars) >= rule["pileup"]["cars"]
        if pileup:
            if (self.last_pileup is not None
                    and now - self.last_pileup[0] <= rule["pileup"]["window_s"]
                    and self.last_pileup[1].intersection(cars)):
                return None
            self.last_pileup = (now, set(cars))
        return {"kind": "pileup" if pileup else self.events[new[0]][1], "cars": cars if pileup else new,
                "speeds": {c: speeds[c] for c in cars}, "window_s": rule["pileup"]["window_s"], "t": now,
                "events": {c: {"t": self.events[c][0], "kind": self.events[c][1]} for c in cars}}
