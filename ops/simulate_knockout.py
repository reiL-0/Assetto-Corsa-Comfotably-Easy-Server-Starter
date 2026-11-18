#!/usr/bin/env python3
"""Simulate a Formula 1 style knockout qualifying (app/knockout.py) and print every stage.

    ops/simulate_knockout.py                        22 drivers, seed 1 (the F1 2026 numbers: 22 -> 16 -> 10)
    ops/simulate_knockout.py --cars 14 --seed 7     a small server
    ops/simulate_knockout.py --cars 22 --advance 16,10 --late 2
    ops/simulate_knockout.py --results q1.json q2.json q3.json    the same, from the result files of three real qualifying sessions

The simulated drivers have a pace and noise; the track gets faster stage by stage; some laps do not count (a spin, track limits). The output also says who the manager would hold in the pits at the start of each stage.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import knockout  # noqa: E402


def fmt(ms):
    return "  no time " if ms is None else f"{ms // 60000}:{ms % 60000 / 1000:06.3f}"


def simulate(n, seed, late):
    rng = random.Random(seed)
    names = [f"D{i + 1:02d}" for i in range(n)]
    pace = {g: rng.gauss(100_000, 700) for g in names}   # ms for a lap: ~1:40
    stages, ran = [], list(names)
    sizes = knockout.field_sizes(n)
    for i in range(3):
        times = {}
        for g in ran:
            laps = [pace[g] - 350 * i + rng.gauss(0, 220) for _ in range(3)]    # the track improves 0.35 s per stage; three tries
            ok = [t for t in laps if rng.random() > 0.07]                          # 7 % of laps are lost (spin, track limits)
            times[g] = round(min(ok)) if ok else None
        stages.append(times)
        if i < 2:
            order = knockout.classify(ran, times, {g: j for j, g in enumerate(ran)})
            ran = order[:sizes[i + 1]]
    return names, stages, [f"L{i + 1}" for i in range(late)]


def from_files(paths):
    from app.results import parse_result_file
    stages = []
    for p in paths:
        stages.append({e["driver_guid"]: e["best_lap_ms"] for e in parse_result_file(Path(p))["classification"] if e["driver_guid"]})
    return list(stages[0]), stages, []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cars", type=int, default=22)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--advance", help="cars that go through to Q2 and Q3, e.g. 16,10")
    ap.add_argument("--late", type=int, default=0, help="drivers who join after Q1")
    ap.add_argument("--results", nargs="+", help="result files of the stages (Q1 Q2 Q3) instead of simulating")
    a = ap.parse_args()
    advance = [int(x) for x in a.advance.split(",")] if a.advance else None
    names, stages, late = from_files(a.results) if a.results else simulate(a.cars, a.seed, a.late)
    out = knockout.run(names, stages, advance, late)
    print(f"entrants {len(names)}  ->  cars per stage {out['sizes']}\n")
    for st in out["stages"]:
        print(f"{st.name}: {len(st.ran)} cars" + (f"   held in the pits: {', '.join(st.held)}" if st.held else ""))
        lead = st.times[st.order[0]]
        for pos, g in enumerate(st.order, 1):
            t = st.times[g]
            gap = "" if t is None or lead is None or pos == 1 else f"+{(t - lead) / 1000:.3f}"
            mark = "OUT" if g in st.out else ""
            print(f"  {pos:>2}  {g:<6} {fmt(t)}  {gap:>8}  {mark}")
        print()
    print("GRID  (Q3 order, then Q2 casualties, then Q1 casualties" + (", then late joiners)" if late else ")"))
    src = {g: st.name for st in out["stages"] for g in (st.order if st is out["stages"][-1] else st.out)}
    for pos, g in enumerate(out["grid"], 1):
        print(f"  P{pos:<2} {g:<6} from {src.get(g, 'joined late')}")
    assert sorted(out["grid"]) == sorted(list(names) + list(late)), "every driver has exactly one place"


if __name__ == "__main__":
    main()
