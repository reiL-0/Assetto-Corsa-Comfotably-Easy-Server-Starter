"""app/knockout.py: Formula 1 style knockout qualifying (Q1, Q2, Q3): who is cut, who is held in the pits, the grid."""

import importlib.util
import random
from pathlib import Path

import pytest

from app import knockout


def test_the_number_of_cars_per_stage_follows_the_f1_cuts_and_scales_down():
    assert knockout.field_sizes(22) == [22, 16, 10]   # F1 2026: 6 out after Q1, 6 after Q2
    assert knockout.field_sizes(20) == [20, 15, 10]   # 5 and 5
    assert knockout.field_sizes(24) == [24, 17, 10]   # 7 and 7
    assert knockout.field_sizes(16) == [16, 12, 8] and knockout.field_sizes(8) == [8, 6, 4]
    assert knockout.field_sizes(3) == [3, 3, 3]       # too few: nobody is cut
    assert knockout.field_sizes(22, [14, 6]) == [22, 14, 6]
    for bad in ([30, 10], [10, 12], [0, 0], [10]):
        with pytest.raises(ValueError):
            knockout.field_sizes(22, bad)


def _times(order, base=100_000):
    """Times that put `order` first to last, 100 ms apart."""
    return {g: base + 100 * i for i, g in enumerate(order)}


def test_the_grid_is_q3_then_the_q2_casualties_then_the_q1_casualties():
    cars = [f"c{i}" for i in range(8)]                              # 8 -> 6 -> 4
    q1 = _times(["c0", "c1", "c2", "c3", "c4", "c5", "c6", "c7"])    # c6 and c7 are out
    q2 = _times(["c3", "c0", "c1", "c2", "c4", "c5"])                # c4 and c5 are out; c3 jumped to the top
    q3 = _times(["c2", "c3", "c0", "c1"])                            # c2 takes pole
    out = knockout.run(cars, [q1, q2, q3])
    assert out["grid"] == ["c2", "c3", "c0", "c1", "c4", "c5", "c6", "c7"]
    assert [st.out for st in out["stages"]] == [["c6", "c7"], ["c4", "c5"], []]
    assert [st.held for st in out["stages"]] == [[], ["c6", "c7"], ["c4", "c5", "c6", "c7"]]   # whoever was cut stays in the pits for every later stage


def test_after_q1_the_cars_to_hold_for_q2_are_known_and_the_grid_is_not_complete_yet():
    cars = [f"c{i}" for i in range(8)]
    out = knockout.run(cars, [_times(reversed(cars))])
    assert out["held"] == ["c0", "c1"]                  # the two slowest (c7 fastest here) must not run Q2
    assert out["grid"] == ["c1", "c0"]                  # only the casualties are placed so far, the later ones first
    out = knockout.run(cars, [_times(reversed(cars)), _times(["c7", "c6", "c5", "c4", "c3", "c2"])])
    assert out["held"] == ["c0", "c1", "c2", "c3"]      # after Q2 the Q1 and Q2 casualties are held for Q3


def test_a_driver_with_no_valid_lap_ranks_behind_everyone_and_is_cut_first():
    cars = [f"c{i}" for i in range(8)]
    q1 = _times(cars)
    q1["c1"] = None                                       # a spin and a deleted lap: no time
    out = knockout.run(cars, [q1])
    assert out["stages"][0].order[-1] == "c1" and "c1" in out["stages"][0].out
    assert set(out["held"]) == {"c1", "c7"}              # c1 (no time) and the slowest with a time, c7


def test_a_tie_goes_to_whoever_ranked_better_before():
    cars = ["a", "b", "c", "d"]                          # nobody is cut with 4 entrants? 4 -> 3 -> 2
    q1 = {"a": 100_000, "b": 100_500, "c": 100_500, "d": 101_000}
    out = knockout.run(cars, [q1])
    assert out["stages"][0].order[:3] == ["a", "b", "c"]  # b and c tied: entry order decides in Q1
    q2 = {"a": 99_000, "b": 99_500, "c": 99_500}
    out = knockout.run(cars, [q1, q2])
    assert out["stages"][1].order == ["a", "b", "c"]      # tied again: b ranked better in Q1


def test_late_joiners_go_last_and_never_ran():
    cars = [f"c{i}" for i in range(6)]
    out = knockout.run(cars, [_times(cars), _times(cars[:4]), _times(cars[:3])], late=["x", "y"])
    assert out["grid"][-2:] == ["x", "y"] and "x" not in {g for st in out["stages"] for g in st.ran}


def test_every_driver_has_exactly_one_place_and_the_cut_ones_never_run_again():
    for seed in range(300):
        rng = random.Random(seed)
        n = rng.randint(1, 26)
        cars = [f"c{i}" for i in range(n)]
        stages, ran = [], list(cars)
        sizes = knockout.field_sizes(n)
        for i in range(3):
            times = {g: (None if rng.random() < 0.1 else rng.randint(95_000, 105_000)) for g in ran}
            stages.append(times)
            order = knockout.classify(ran, times, {g: j for j, g in enumerate(ran)})
            ran = order[: sizes[i + 1]] if i < 2 else order
        out = knockout.run(cars, stages)
        assert sorted(out["grid"]) == sorted(cars), seed
        seen_out = set()
        for st in out["stages"]:
            assert not (set(st.ran) & seen_out), seed        # a driver cut earlier is not in a later stage
            assert len(st.ran) == out["sizes"][out["stages"].index(st)] or n < 4, seed
            seen_out |= set(st.out)


def test_the_simulation_script_runs_and_prints_a_full_grid(capsys, monkeypatch):
    spec = importlib.util.spec_from_file_location("sim", Path(__file__).resolve().parent.parent / "ops" / "simulate_knockout.py")
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    monkeypatch.setattr("sys.argv", ["simulate_knockout.py", "--cars", "14", "--seed", "3", "--late", "2"])
    sim.main()
    out = capsys.readouterr().out
    assert "cars per stage [14, 10, 7]" in out and "P16" in out and "joined late" in out


def test_the_director_can_choose_how_many_cars_are_cut_after_each_stage():
    assert knockout.field_sizes(22, cuts=[None, None]) == [22, 16, 10]   # automatic = F1's
    assert knockout.field_sizes(30, cuts=[8, 8]) == [30, 22, 14]          # a bigger grid
    assert knockout.field_sizes(12, cuts=[2, 3]) == [12, 10, 7]           # a smaller one
    assert knockout.field_sizes(22, cuts=[3, None]) == [22, 19, 13]       # only the first one: the second stays automatic (6)
    assert knockout.field_sizes(8, cuts=[0, 0]) == [8, 8, 8]              # nobody cut
    assert knockout.field_sizes(5, cuts=[10, 10]) == [5, 1, 1]            # more than there are: one is always left in each stage
    with pytest.raises(ValueError):
        knockout.field_sizes(22, cuts=[1])
    with pytest.raises(ValueError):
        knockout.field_sizes(22, cuts=[-1, 2])


def test_a_knockout_with_the_directors_cuts_cuts_and_places_them():
    cars = [f"c{i}" for i in range(10)]
    q1 = _times(cars)
    out = knockout.run(cars, [q1], cuts=[3, 2])                           # 10 -> 7 -> 5
    assert out["held"] == ["c7", "c8", "c9"] and out["sizes"] == [10, 7, 5]
    q2 = _times(cars[:7])
    q3 = _times(cars[:5])
    full = knockout.run(cars, [q1, q2, q3], cuts=[3, 2])
    assert full["grid"] == cars and [st.out for st in full["stages"]] == [["c7", "c8", "c9"], ["c5", "c6"], []]


def test_a_stage_run_in_groups_lets_the_best_of_each_group_through_and_ranks_the_casualties_of_both_together():
    cars = [f"c{i:02d}" for i in range(14)]                 # c00 fastest ... c13 slowest, over both groups
    groups = {g: 1 if i % 2 == 0 else 2 for i, g in enumerate(cars)}   # group 1: the even ones, group 2: the odd ones
    q1 = _times(cars)
    out = knockout.run(cars, [q1], advance=[8, 4], groups=groups)       # 14 -> 8 (4 per group) -> 4
    st = out["stages"][0]
    assert [g for g in st.order if g not in st.out] == ["c00", "c01", "c02", "c03", "c04", "c05", "c06", "c07"]   # the four best of each group
    assert st.out == ["c08", "c09", "c10", "c11", "c12", "c13"]         # the casualties of both groups, by time
    assert out["held"] == st.out
    # it is not the same as the top eight overall when one group is faster
    q1b = _times(["c00", "c02", "c04", "c06", "c08", "c10", "c12", "c01", "c03", "c05", "c07", "c09", "c11", "c13"])   # group 1 is the faster one
    out = knockout.run(cars, [q1b], advance=[8, 4], groups=groups)
    assert set(g for g in out["stages"][0].order if g not in out["stages"][0].out) == {"c00", "c02", "c04", "c06", "c01", "c03", "c05", "c07"}   # still 4 + 4


def test_a_group_with_fewer_cars_than_its_places_leaves_them_to_the_best_of_the_others():
    cars = [f"c{i}" for i in range(8)]
    groups = {g: 1 if i < 7 else 2 for i, g in enumerate(cars)}         # 7 in group 1, 1 in group 2
    assert knockout.per_group(cars, groups, 6) == {"c0", "c1", "c2", "c7", "c3", "c4"}   # quota 3 + 3: group 2 has one car, the other two places go to the best of group 1


def test_the_full_indy_road_course_weekend_groups_then_the_twelve_then_the_fast_six():
    cars = [f"c{i:02d}" for i in range(20)]
    groups = {g: 1 if i % 2 == 0 else 2 for i, g in enumerate(cars)}
    q1 = _times(cars)
    q2 = _times(cars[:12])
    q3 = _times(["c03", "c00", "c01", "c02", "c04", "c05"])
    out = knockout.run(cars, [q1, q2, q3], advance=[12, 6], groups=groups, names=("Grupos", "Top 12", "Fast Six"))
    assert out["grid"][:6] == ["c03", "c00", "c01", "c02", "c04", "c05"]       # the Fast Six, by its own times
    assert out["grid"][6:12] == cars[6:12]                                       # the casualties of the round of twelve
    assert out["grid"][12:] == cars[12:]                                         # the casualties of the groups, together
    assert [len(st.ran) for st in out["stages"]] == [20, 12, 6]
