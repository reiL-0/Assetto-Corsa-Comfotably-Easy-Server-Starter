"""Where every car is along the track, as one number that never wraps: laps plus the spline position (0..1), unwrapped.

Plain state, no I/O: `ACSPClient` feeds it from every car_update (about 5 Hz). The caution procedures (app/live/caution.py) read it to freeze the order, to see who passes whom and to tell who is
a lap down. `progress` of the car in front is the larger one; a lap down is a difference of 1.0 or more. A car is followed by the SMALLEST step of its spline between two updates, so the line
being crossed (the spline jumps from ~1 to ~0) adds a few metres instead of a whole lap, and does not depend on when the lap_completed packet arrives.
"""

from __future__ import annotations

import time
from typing import NamedTuple

JUMP = 0.15   # of a lap: a car does not move this far between two updates (~5 Hz); a bigger step is a teleport


def ring(d: float) -> float:
    """A progress difference folded to [-0.5, 0.5): how far ahead (positive) or behind the other car is ON THE TRACK, whatever the laps between them."""
    return (d + 0.5) % 1.0 - 0.5


class Sample(NamedTuple):
    """One car as the automatic flags need it: where it is (laps + spline, unwrapped), and how far that number can be trusted."""

    progress: float
    epoch: int         # bumps whenever the progress was RE-ANCHORED (first sample, a teleport, a correction by the lap counter): the progress is not continuous across epochs, so a car that seems to have
                       # passed another between two epochs has not necessarily done so
    generation: int    # bumps when the slot was dropped (the driver left): the same slot can be another driver or another connection, even with the same GUID
    stamp: float       # time.monotonic() of its last update
    since: float       # time.monotonic() since which its progress has been continuous (the last re-anchoring)


class Field:
    def __init__(self) -> None:
        self.progress: dict[int, float] = {}
        self._spline: dict[int, float] = {}
        self.epoch: dict[int, int] = {}
        self.generation: dict[int, int] = {}   # survives drop(): that is what it counts
        self.stamp: dict[int, float] = {}
        self.since: dict[int, float] = {}
        self.session = 0                       # bumps on clear(): a new session, every car starts again

    def update(self, car_id: int, spline: float, laps: int | None = None, now: float | None = None) -> None:
        """A car_update. `laps` = what the board knows (None = not known yet). The steps of the spline decide, and the laps only put the car right when it has no progress yet, when it
        jumped (a teleport, a reset to the pits: no car moves 0.15 of a lap between two updates) or when the two disagree by more than a lap and a half."""
        now = time.monotonic() if now is None else now
        last = self._spline.get(car_id)
        reanchored = False
        hint = None if laps is None else laps + (spline - 1.0 if laps == 0 and spline > 0.5 else spline)   # (on the grid, behind the line: -0.01, not 0.99)
        step = ring(spline - last) if last is not None else 0.0
        if last is None or abs(step) > JUMP:
            self.progress[car_id] = hint if hint is not None else self.progress.get(car_id, spline) + (step if last is not None else 0.0)
            reanchored = True
        else:
            self.progress[car_id] += step
            if hint is not None and abs(self.progress[car_id] - hint) > 1.5:
                self.progress[car_id] = hint
                reanchored = True
        self._spline[car_id] = spline
        self.stamp[car_id] = now
        if reanchored:
            self.epoch[car_id] = self.epoch.get(car_id, 0) + 1
            self.since[car_id] = now

    def snapshot(self) -> dict[int, float]:
        """A copy that is safe to take from another thread (one C-level copy under the GIL: the event loop keeps updating)."""
        return dict(self.progress)

    def samples(self) -> dict[int, Sample]:
        """Every car with its progress and how far it can be trusted. A copy; read it on the event loop that feeds the field (the monitor does), so that the cars are one coherent picture."""
        return {c: Sample(p, self.epoch.get(c, 0), self.generation.get(c, 0), self.stamp.get(c, 0.0), self.since.get(c, 0.0)) for c, p in self.progress.items()}

    def drop(self, car_id: int) -> None:
        self.progress.pop(car_id, None)
        self._spline.pop(car_id, None)
        self.epoch.pop(car_id, None)
        self.stamp.pop(car_id, None)
        self.since.pop(car_id, None)
        self.generation[car_id] = self.generation.get(car_id, 0) + 1   # (the slot may be a new driver)

    def clear(self) -> None:
        self.progress.clear()
        self._spline.clear()
        self.epoch.clear()
        self.stamp.clear()
        self.since.clear()
        self.session += 1

    def order(self) -> list[int]:
        """Car ids from the front."""
        return sorted(self.progress, key=lambda c: -self.progress[c])
