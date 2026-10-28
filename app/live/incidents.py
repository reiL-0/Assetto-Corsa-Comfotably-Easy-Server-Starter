"""Does the race need a caution? Looks at the contacts and the stopped cars and makes a PROPOSAL for the director (or, when the director asked for it, the yellow itself).

: it keeps moving but under SLOW_FRAC of the pace of the others (their median, at least SLOW_REF_KMH) for SLOW_LIMP_S seconds, outside the pit lane (a damaged car).
 are `involved`: they are the ones the
free pass skips and that restart at the tail (app/live/caution.py). The speed unit of the contact events is not verified against a real server (km/h assumed, like app/stewards/detectors.py),
and a contact is reported 1.5-2.5 s late: these numbers are first guesses to tune with real races.
"""

from __future__ import annotations

CONTACT_KMH = 20.0   # a contact slower than this does not make a car 'involved'
STOP_KMH, STOP_S = 5.0, 6.0
MOVING_KMH = 30.0    # a car that has been this fast is racing: the one that never was (a spectator, a car in the garage) is not a stopped car
SLOW_FRAC, SLOW_REF_KMH, SLOW_LIMP_S, SLOW_MIN_CARS = 0.4, 100.0, 12.0, 3   # a limping car: under 40 % of the median of the others (which must be doing 100+, at least 3 of them) for 12 s
PROPOSAL_FOR = 60.0  # s a proposal stays up if nobody acts on it


class Watch:
    def __init__(self) -> None:
        self.involved: dict[int, float] = {}    # car id -> when it was last in a contact
        self._moved: set[int] = set()
        self._still: dict[int, float] = {}       # car id -> since when it has been (nearly) stopped
        self._flagged: set[int] = set()          # stopped cars already proposed (until they move again)
        self._limp: dict[int, float] = {}        # car id -> since when it has been far under the pace of the others
        self._limp_flagged: set[int] = set()     # limping cars already proposed (until they are up to pace again)
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
            self._still.clear()   # (the formation lap and the cautions do not count: the six seconds start when the race is on)
        for c, since in self._limp.items():
            if now - since >= SLOW_LIMP_S and c not in self._limp_flagged:
                self._limp_flagged.add(c)
                return self._propose(now, "limping", [c], "Coche lento en pista")
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
        self._limp_flagged.update(self._limp)

    def recent_involved(self, now: float, within: float) -> set[int]:
        return {c for c, t in self.involved.items() if now - t < within}
