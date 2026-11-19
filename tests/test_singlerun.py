"""app/singlerun.py: qualifying by single-car runs (let out one at a time, N timed laps, ranked by average or top speed)."""

import importlib.util
import random
from pathlib import Path

import pytest

from app import singlerun
from app.singlerun import Run


def test_the_cars_are_let_out_one_every_interval_and_the_rest_are_held():
    order = ["a", "b", "c", "d"]
    assert singlerun.release_times(order, 20) == {"a": 0, "b": 20, "c": 40, "d": 60}
    assert singlerun.on_track(order, 20, 0) == ["a"]
    assert singlerun.on_track(order, 20, 45) == ["a", "b", "c"]
    assert singlerun.held(order, 20, 45) == ["d"]
    assert singlerun.held(order, 20, 45, finished={"a", "c"}) == ["a", "c", "d"]        # who finished goes back to the pits and is held again
    assert singlerun.on_track(order, 20, 45, finished={"a", "c"}) == ["b"]
    assert singlerun.held(order, 20, 100, finished={"a", "b", "c", "d"}) == order       # everybody is done: everybody is held


def test_a_driver_who_came_in_late_is_held_until_he_has_a_place_and_goes_last():
    order = ["a", "b"]
    assert singlerun.held(order, 20, 100, connected=["a", "b", "z"]) == ["z"]            # connected, not in the order: held
    assert singlerun.with_late(order, ["b", "z", "y", "z"]) == ["a", "b", "z", "y"]      # in the order they came, once each
    assert singlerun.on_track(singlerun.with_late(order, ["z"]), 20, 45) == ["a", "b", "z"]


def test_the_timed_laps_are_the_ones_after_the_warm_up_and_a_cut_lap_voids_the_run():
    r = Run("a", [30_000, 25_000, 25_100, 26_000])
    assert r.timed(1, 2) == [25_000, 25_100]
    assert r.timed(0, 2) == [30_000, 25_000]
    assert Run("a", [30_000, 25_000]).timed(1, 2) is None            # not complete yet
    assert Run("a", [30_000, 25_000, 25_100], [0, 0, 1]).timed(1, 2) is None   # the second timed lap has cuts
    assert Run("a", [30_000, 25_000, 25_100], [1, 0, 0]).timed(1, 2) == [25_000, 25_100]   # a cut in the warm-up lap does not matter
    assert Run("a", [30_000, 25_000]).done(1, 2) is False and Run("a", [30_000, 25_000, 25_100]).done(1, 2) is True
    assert Run("a", [30_000, 25_000, 25_100], [0, 0, 1]).done(1, 2) is True       # void but over: the car goes back to the pits


def test_the_average_speed_is_the_same_ranking_as_the_total_time_and_the_table_shows_km_h():
    runs = {"slow": Run("slow", [30_000, 26_000, 26_000]), "fast": Run("fast", [30_000, 25_000, 25_100]), "mid": Run("mid", [30_000, 25_500, 25_500])}
    assert singlerun.classify(runs) == ["fast", "mid", "slow"]
    assert singlerun.average_kmh([25_000, 25_000], 1609) == pytest.approx(1609 / 25 * 3.6)   # 231.7 km/h


def test_a_void_or_incomplete_run_ranks_behind_every_completed_one_in_the_release_order():
    runs = {
        "void": Run("void", [30_000, 20_000, 20_000], [0, 1, 0]),           # the fastest laps, but a cut: void
        "short": Run("short", [30_000, 20_000]),                            # stopped before its second timed lap
        "ok": Run("ok", [30_000, 26_000, 26_000]),
        "ok2": Run("ok2", [30_000, 25_000, 25_000]),
    }
    assert singlerun.classify(runs) == ["ok2", "ok", "void", "short"]


def test_a_tie_keeps_whoever_went_out_first():
    runs = {"first": Run("first", [30_000, 25_000, 25_000]), "second": Run("second", [30_000, 25_000, 25_000])}
    assert singlerun.classify(runs) == ["first", "second"]


def test_ranking_by_top_speed_uses_the_fastest_and_leaves_out_runs_with_no_speed_data():
    runs = {"a": Run("a", [30_000, 25_000, 25_000], top_kmh=240.0), "b": Run("b", [30_000, 26_000, 26_000], top_kmh=251.5), "c": Run("c", [30_000, 24_000, 24_000])}
    assert singlerun.classify(runs, metric="top_speed") == ["b", "a", "c"]   # c has the best laps but no speed samples: behind
    assert singlerun.classify(runs, metric="average") == ["c", "a", "b"]
    with pytest.raises(ValueError):
        singlerun.classify(runs, metric="best_lap")


def test_every_car_has_exactly_one_place_whatever_happens():
    for seed in range(300):
        rng = random.Random(seed)
        n = rng.randint(1, 24)
        runs = {}
        for i in range(n):
            laps = [rng.randint(20_000, 35_000) for _ in range(rng.randint(0, 5))]
            runs[f"c{i}"] = Run(f"c{i}", laps, [int(rng.random() < 0.15) for _ in laps], rng.choice([None, round(rng.uniform(200, 260), 1)]))
        for metric in singlerun.METRICS:
            assert sorted(singlerun.classify(runs, rng.randint(0, 2), rng.randint(1, 4), metric)) == sorted(runs), (seed, metric)


def test_the_simulation_script_runs_and_prints_a_full_grid(capsys, monkeypatch):
    spec = importlib.util.spec_from_file_location("sim", Path(__file__).resolve().parent.parent / "ops" / "simulate_single_runs.py")
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    monkeypatch.setattr("sys.argv", ["simulate_single_runs.py", "--cars", "8", "--seed", "2", "--metric", "top_speed"])
    sim.main()
    out = capsys.readouterr().out
    assert "8 cars, 20 s apart" in out and "GRID" in out and "P8" in out
