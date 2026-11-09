"""Who has the pace (pure: no clock, no I/O, no locks: `RaceControl` calls it under its own lock). Feeds the blue flag: it is shown to a car only when the car BEHIND is quicker than the one it is about to lap, so a
lapped car that is faster (a comeback, a car that lost a lap to a penalty) is never flagged and the car in front of it is never asked to yield.

A car's pace is the median of its last `laps` CLEAN laps (no pit in or out, no server cut, not under a caution, not the first lap). Fewer than `laps` clean laps = unknown pace: no blue, nothing is judged (when in
doubt, nobody is flagged). B is quicker than A when its pace is at least `margin_pct` % better; once the flag is open it stays until the advantage falls under half the margin (no flicker).
"""
from __future__ import annotations

import statistics
from collections import deque


class Pace:
    def __init__(self, laps: int = 3, margin_pct: float = 0.7) -> None:
        self.laps, self.margin = laps, margin_pct / 100.0
        self._laps: dict[int, deque[int]] = {}

    def configure(self, laps: int, margin_pct: float) -> None:
        if laps != self.laps:
            self._laps = {c: deque(d, maxlen=laps) for c, d in self._laps.items()}
        self.laps, self.margin = laps, margin_pct / 100.0

    def lap(self, car: int, ms: int, clean: bool) -> None:
        """A lap was completed. A dirty lap is not kept (and does not break the clean ones before it)."""
        if clean and ms > 0:
            self._laps.setdefault(car, deque(maxlen=self.laps)).append(ms)

    def forget(self, car: int) -> None:
        """The slot changed hands: the next driver's pace starts from nothing."""
        self._laps.pop(car, None)

    def of(self, car: int) -> float | None:
        d = self._laps.get(car)
        return statistics.median(d) if d is not None and len(d) >= self.laps else None

    def quicker(self, behind: int, ahead: int, open_already: bool = False) -> bool:
        """Has `behind` the pace to catch `ahead`? Unknown pace is no."""
        pb, pa = self.of(behind), self.of(ahead)
        if pb is None or pa is None:
            return False
        return pb / pa <= 1.0 - (self.margin / 2 if open_already else self.margin)
