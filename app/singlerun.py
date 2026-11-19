"""Qualifying by single-car runs, like IndyCar's ovals: pure logic, no I/O.

How it goes: the cars are in the pits in a fixed order (by car number, or the order the director gives); the first one is let out when the session starts, the next one `interval_s` seconds later (20 s, say), and so on.
Each car has ONE run: some warm-up laps that do not count (`warmup` laps, 1 by default: the lap that leaves the pits) and then `laps` timed laps (2 on the short ovals; 4 at Indianapolis). It is ranked by the AVERAGE SPEED
of those laps (the same thing as their total time on one track, so the ranking needs no track length; the length only turns it into km/h for the table) or by its TOP SPEED. After the run the car goes back to the
pits and stays there. A lap with track cuts voids the run. A run that has not completed its laps ranks behind every completed one.

What the manager does with it (app/singlerunlive.py, later): the cars that are not released yet and the ones that finished are held in the pits by the same `pit_hold` the knockout qualifying uses; the laps of a run
come from the lap events of the server (app/models.py Activity) after the car's release; the grid goes into the times of the practice before the race (app/gridtimes.py).

ponytail: a driver who joins late is put at the end of the order (released after the last one that was scheduled, if the session still has time); a driver who leaves and comes back keeps his place (everything is by Steam ID).
The metric `top_speed` needs the speed samples of the run (ACSP car updates): this module only ranks what it is given.
"""

from __future__ import annotations

from dataclasses import dataclass, field

METRICS = ("average", "top_speed")


def release_times(order: list[str], interval_s: float) -> dict[str, float]:
    """Seconds after the start of the session at which each car is let out: the first at 0, the next `interval_s` later..."""
    return {g: i * interval_s for i, g in enumerate(dict.fromkeys(order))}


def on_track(order: list[str], interval_s: float, elapsed_s: float, finished: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Who is allowed on the track `elapsed_s` seconds into the session: released and not finished."""
    return [g for g, t in release_times(order, interval_s).items() if t <= elapsed_s and g not in finished]


def held(order: list[str], interval_s: float, elapsed_s: float, finished: set[str] | frozenset[str] = frozenset(), connected: list[str] | None = None) -> list[str]:
    """Who must stay in the pits: those not released yet and those that finished. `connected`: when given, a driver who is connected but is not in the order (he came in late) is held as well until he has a place."""
    free = set(on_track(order, interval_s, elapsed_s, finished))
    everyone = list(dict.fromkeys([*order, *(connected or [])]))
    return [g for g in everyone if g not in free]


def with_late(order: list[str], connected: list[str]) -> list[str]:
    """The order with whoever connected and is not in it put at the end, in the order they came."""
    return list(dict.fromkeys([*order, *connected]))


@dataclass
class Run:
    guid: str
    laps_ms: list[int] = field(default_factory=list)   # EVERY lap the car completed since it was released, in order, warm-up ones included
    cuts: list[int] = field(default_factory=list)      # track cuts of each of those laps (same length; missing = 0)
    top_kmh: float | None = None                       # its top speed in the run

    def timed(self, warmup: int, laps: int) -> list[int] | None:
        """The `laps` timed laps (after the `warmup` ones), or None when the run is not complete or a timed lap has cuts."""
        win = self.laps_ms[warmup: warmup + laps]
        cut = (self.cuts + [0] * len(self.laps_ms))[warmup: warmup + laps]
        if len(win) < laps or any(cut):
            return None
        return win

    def done(self, warmup: int, laps: int) -> bool:
        """The run is over: it has completed its timed laps (a cut one too: the run is void but it is over)."""
        return len(self.laps_ms) >= warmup + laps


def average_kmh(laps_ms: list[int], length_m: float) -> float:
    return length_m * len(laps_ms) / (sum(laps_ms) / 1000) * 3.6


def classify(runs: dict[str, Run], warmup: int = 1, laps: int = 2, metric: str = "average") -> list[str]:
    """Best first. Completed valid runs by the metric (average speed: the smaller total time first; top speed: the faster first); then the voided ones and the incomplete ones, in the order of the runs given
    (the release order). A tie keeps the order of the runs given (whoever went out first)."""
    if metric not in METRICS:
        raise ValueError(f"metric {metric!r}: one of {METRICS}")
    order = list(runs)
    ok = {g: r.timed(warmup, laps) for g, r in runs.items()}
    good = [g for g in order if ok[g] is not None and (metric == "average" or runs[g].top_kmh is not None)]
    key = (lambda g: sum(ok[g])) if metric == "average" else (lambda g: -runs[g].top_kmh)
    return sorted(good, key=key) + [g for g in order if g not in good]
