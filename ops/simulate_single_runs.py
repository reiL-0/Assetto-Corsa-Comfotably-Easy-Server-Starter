#!/usr/bin/env python3
"""Simulate a qualifying by single-car runs (app/singlerun.py): cars let out one at a time, N timed laps each, ranked by average or top speed.

    ops/simulate_single_runs.py                                  20 cars, 20 s apart, 1 warm-up lap and 2 timed laps, average speed, a one-mile oval
    ops/simulate_single_runs.py --cars 12 --interval 30 --laps 4 --metric top_speed --seed 5
    ops/simulate_single_runs.py --length 4023 --laps 4           (Indianapolis: 2.5 miles, 4 laps)

The simulated cars have a speed and some noise; the first lap (the one that leaves the pits) is slower and does not count; a few runs are voided (track limits) or end early (a crash). The output
shows the timeline (who is on the track at several moments, who is held in the pits), every run and the grid.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import singlerun  # noqa: E402


def simulate(n, interval, warmup, laps, length_m, seed):
    rng = random.Random(seed)
    order = [f"#{i + 1:02d}" for i in range(n)]
    runs, spans = {}, {}
    for g in order:
        speed = rng.gauss(230, 6)   # km/h
        lap_ms = [round(length_m / (speed / 3.6) * 1000 * (1.08 if i < warmup else rng.gauss(1, 0.002))) for i in range(warmup + laps)]
        cuts = [0] * len(lap_ms)
        if rng.random() < 0.04:
            lap_ms = lap_ms[: rng.randint(1, len(lap_ms) - 1)]   # a crash: the run stops
            cuts = cuts[: len(lap_ms)]
        elif rng.random() < 0.05:
            cuts[rng.randrange(warmup, len(cuts))] = 1            # track limits: the run is void
        runs[g] = singlerun.Run(g, lap_ms, cuts, round(speed * 1.035 + rng.gauss(0, 1.5), 1))
        spans[g] = sum(lap_ms) / 1000
    return order, runs, spans


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cars", type=int, default=20)
    ap.add_argument("--interval", type=float, default=20)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--laps", type=int, default=2)
    ap.add_argument("--length", type=float, default=1609, help="track length in metres")
    ap.add_argument("--metric", choices=singlerun.METRICS, default="average")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    order, runs, spans = simulate(a.cars, a.interval, a.warmup, a.laps, a.length, a.seed)
    rel = singlerun.release_times(order, a.interval)
    end = {g: rel[g] + spans[g] for g in order}
    print(f"{a.cars} cars, {a.interval:g} s apart, {a.warmup} warm-up lap(s) + {a.laps} timed, ranked by {a.metric}; track {a.length:g} m\n")
    print("who is on the track (the rest are held in the pits):")
    for t in range(0, int(max(end.values())) + 1, max(1, int(a.interval * 3))):
        finished = {g for g in order if end[g] <= t}
        print(f"  t={t:>4} s   on track: {', '.join(singlerun.on_track(order, a.interval, t, finished)) or '-':<30} held: {len(singlerun.held(order, a.interval, t, finished))}")
    ranking = singlerun.classify(runs, a.warmup, a.laps, a.metric)
    print(f"\n{'car':<5}{'out at':>8}{'back at':>9}  {'timed laps (s)':<22}{'avg km/h':>9}{'top km/h':>9}  status")
    for g in order:
        r = runs[g]
        t = r.timed(a.warmup, a.laps)
        status = "VOID (track limits)" if r.done(a.warmup, a.laps) and t is None else "" if t else "INCOMPLETE (stopped)"
        timed = " ".join(f"{x / 1000:.3f}" for x in r.laps_ms[a.warmup:a.warmup + a.laps])
        print(f"{g:<5}{rel[g]:>7.0f}s{end[g]:>8.0f}s  {timed:<22}{(singlerun.average_kmh(t, a.length) if t else float('nan')):>9.2f}{r.top_kmh:>9.1f}  {status}")
    print("\nGRID")
    for pos, g in enumerate(ranking, 1):
        t = runs[g].timed(a.warmup, a.laps)
        print(f"  P{pos:<2} {g:<5}" + (f" {singlerun.average_kmh(t, a.length):7.2f} km/h avg" if t else "   no valid run"))
    assert sorted(ranking) == sorted(order), "every car has exactly one place"


if __name__ == "__main__":
    main()
