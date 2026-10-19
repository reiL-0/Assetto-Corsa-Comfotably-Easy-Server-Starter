"""Where every car is along the track, as one number that never wraps: laps plus the spline position (0..1), unwrapped.

Plain state, no I/O: `ACSPClient` feeds it from every car_update (about 5 Hz). The caution procedures (app/live/caution.py) read it to freeze the order, to see who passes whom and to tell who is
a lap down. `progress` of the car in front is the larger one; a lap down is a difference of 1.0 or more. A car is followed by the SMALLEST step of its spline between two updates, so the line
being crossed (the spline jumps from ~1 to ~0) adds a few metres instead of a whole lap, and does not depend on when the lap_completed packet arrives.
"""

from __future__ import annotations


JUMP = 0.15   # of a lap: a car does not move this far between two updates (~5 Hz); a bigger step is a teleport


def ring(d: float) -> float:
    """A progress difference folded to [-0.5, 0.5): how far ahead (positive) or behind the other car is ON THE TRACK, whatever the laps between them."""
    return (d + 0.5) % 1.0 - 0.5


class Field:
    def __init__(self) -> None:
        self.progress: dict[int, float] = {}
        self._spline: dict[int, float] = {}

    def update(self, car_id: int, spline: float, laps: int | None = None) -> None:
        """A car_update. `laps` = what the board knows (None = not known yet). The steps of the spline decide, and the laps only put the car right when it has no progress yet, when it
        jumped (a teleport, a reset to the pits: no car moves 0.15 of a lap between two updates) or when the two disagree by more than a lap and a half."""
        last = self._spline.get(car_id)
        hint = None if laps is None else laps + (spline - 1.0 if laps == 0 and spline > 0.5 else spline)   # (on the grid, behind the line: -0.01, not 0.99)
        step = ring(spline - last) if last is not None else 0.0
        if last is None or abs(step) > JUMP:
            self.progress[car_id] = hint if hint is not None else self.progress.get(car_id, spline) + (step if last is not None else 0.0)
        else:
            self.progress[car_id] += step
            if hint is not None and abs(self.progress[car_id] - hint) > 1.5:
                self.progress[car_id] = hint
        self._spline[car_id] = spline

    def snapshot(self) -> dict[int, float]:
        """A copy that is safe to take from another thread (one C-level copy under the GIL: the event loop keeps updating)."""
        return dict(self.progress)

    def drop(self, car_id: int) -> None:
        self.progress.pop(car_id, None)
        self._spline.pop(car_id, None)

    def clear(self) -> None:
        self.progress.clear()
        self._spline.clear()

    def order(self) -> list[int]:
        """Car ids from the front."""
        return sorted(self.progress, key=lambda c: -self.progress[c])
