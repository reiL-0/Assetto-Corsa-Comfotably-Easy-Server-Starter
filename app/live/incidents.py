"""Does the race need a caution? Looks at the contacts and the stopped cars and makes a PROPOSAL for the director (or, when the director asked for it, the yellow itself).

Plain state, no I/O (`ACSPClient` feeds the contacts, the monitor calls `step`). Two things call for a caution:
  * a PILE-UP: PILEUP_CARS different cars in a contact within PILEUP_S seconds of each other;
  * a car STOPPED on the track: it was moving (so it is racing, not a spectator or a car in the garage), it has been under STOP_KMH for STOP_S seconds and it is not in the pit lane.
A single hard contact is not enough by itself: the car either keeps going (nothing to wait for) or stops (it becomes a stopped car). The cars in a contact are `involved`: they are the ones the
free pass skips and that restart at the tail (app/live/caution.py). The speed unit of the contact events is not verified against a real server (km/h assumed, like app/stewards/detectors.py),
and a contact is reported 1.5-2.5 s late: these numbers are first guesses to tune with real races.
"""

from __future__ import annotations

from collections import deque

CONTACT_KMH = 20.0   # a contact slower than this does not make a car 'involved'
PILEUP_CARS, PILEUP_S = 3, 10.0
STOP_KMH, STOP_S = 5.0, 6.0
MOVING_KMH = 30.0    # a car that has been this fast is racing: the one that never was (a spectator, a car in the garage) is not a stopped car
PROPOSAL_FOR = 60.0  # s a proposal stays up if nobody acts on it


class Watch:
    def __init__(self) -> None:
        self.contacts: deque[tuple[float, int, int | None]] = deque(maxlen=64)   # (time, car, the other car or None for a wall)
        self.involved: dict[int, float] = {}    # car id -> when it was last in a contact
        self._moved: set[int] = set()
        self._still: dict[int, float] = {}       # car id -> since when it has been (nearly) stopped
        self._flagged: set[int] = set()          # stopped cars already proposed (until they move again)
        self.proposal: dict | None = None
        self._next_id = 1

    def on_contact(self, now: float, car: int, other: int | None, speed: float) -> None:
        if speed < CONTACT_KMH:
            return
        self.contacts.append((now, car, other))
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
        if not racing:
            self._still.clear()   # (the formation lap and the cautions do not count: the six seconds start when the race is on)
            return None
        recent = {x for t, a, b in self.contacts if now - t <= PILEUP_S for x in (a, b) if x is not None}
        if len(recent) >= PILEUP_CARS and not (self.proposal and self.proposal["kind"] == "pileup"):
            self.contacts.clear()
            return self._propose(now, "pileup", sorted(recent), f"Choque múltiple: {len(recent)} coches")
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
        self.contacts.clear()
        self._flagged.update(self._still)

    def recent_involved(self, now: float, within: float) -> set[int]:
        return {c for c, t in self.involved.items() if now - t < within}
