"""Live race control of one server: the flag the director shows, the pace for a full-course yellow and the drive-throughs still to be served.

Plain state, no I/O: `RaceControl` is what the director (a steward in Control AC) sets and what the CSP hub script on every client reads (GET /servers/{id}/race-state, through the
website's /api/csp/state). The manager is the only authority: the client shows the flag, limits its pace and sets the game's own penalty; a modified client can ignore it, so nothing
here is a sanction by itself (the penalties of the result stay in app/penalties.py).

`rev` rises on every change (the hub applies each penalty `id` once and drops the flag if the answers stop). Lives in memory: it is gone when the server stops, and `snapshot`
resets it when the session changes (a yellow must not survive into the next session).
"""

from __future__ import annotations

import itertools
import time

# Penalty ids only ever rise, across sessions and manager restarts (they start at the clock): the hub applies each id once, and a client that stays connected through a session change
# must never see an id it has already used come back for a different penalty.
_penalty_ids = itertools.count(int(time.time()))

FLAGS = ("green", "yellow", "white", "checkered")
PACE_MIN, PACE_MAX = 30, 250   # km/h a full-course yellow may ask for
LAPS_MAX = 5                   # laps a car gets to serve a drive-through (the game enforces it: pit-lane pass clears it, running out sends the car to the pits)


class RaceControl:
    def __init__(self, session_ref: object = None) -> None:
        self.flag, self.pace_kmh = "green", None
        self.penalties: list[dict] = []
        self.rev, self.ts = 0, time.time()
        self.session_ref = session_ref   # the ACSP session object this state belongs to (a new session replaces it)

    def _bump(self) -> None:
        self.rev += 1
        self.ts = time.time()

    def set_flag(self, flag: str, pace_kmh: int | None = None) -> None:
        if flag not in FLAGS:
            raise ValueError(f"flag must be one of {', '.join(FLAGS)}")
        if flag == "yellow" and pace_kmh is not None and not PACE_MIN <= pace_kmh <= PACE_MAX:
            raise ValueError(f"pace is between {PACE_MIN} and {PACE_MAX} km/h")
        self.flag, self.pace_kmh = flag, (pace_kmh if flag == "yellow" else None)   # only a yellow carries a pace
        self._bump()

    def add_penalty(self, car_id: int, laps: int) -> dict:
        if not 1 <= laps <= LAPS_MAX:
            raise ValueError(f"laps is between 1 and {LAPS_MAX}")
        p = {"id": next(_penalty_ids), "car_id": car_id, "type": "pits", "laps": laps}
        self.penalties.append(p)
        self._bump()
        return p

    def clear_penalty(self, penalty_id: int) -> bool:
        n = len(self.penalties)
        self.penalties = [p for p in self.penalties if p["id"] != penalty_id]
        if len(self.penalties) != n:
            self._bump()
        return len(self.penalties) != n

    def snapshot(self, running: bool = True) -> dict:
        return {"rev": self.rev, "ts": int(self.ts), "running": running, "flag": self.flag, "pace_kmh": self.pace_kmh, "penalties": list(self.penalties)}


_states: dict[int, RaceControl] = {}


def state_of(server_id: int, running: bool, session_ref: object = None, create: bool = False) -> RaceControl | None:
    """The state of a server: forgotten when it is not running or the session changed (a new RaceControl starts green, with `rev` one ahead so clients notice the reset)."""
    rc = _states.get(server_id)
    if rc and (not running or rc.session_ref is not session_ref):
        _states.pop(server_id, None)
        rc = None
        if running and session_ref is not None:   # a session change: tell the hub clients (rev moves on) that the old flag is gone
            fresh = RaceControl(session_ref)
            fresh.rev = 1
            _states[server_id] = fresh
            return fresh
    if rc is None and create and running:
        rc = _states[server_id] = RaceControl(session_ref)
    return rc
