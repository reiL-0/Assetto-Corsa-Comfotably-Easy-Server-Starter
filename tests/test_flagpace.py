"""Pace for the blue flag (app/live/flagpace.py)."""
from app.live.flagpace import Pace
from app.rulebook import BlueRule, Detectors


def feed(p, car, ms, n=3, clean=True):
    for _ in range(n):
        p.lap(car, ms, clean)


def test_unknown_pace_is_never_quicker():
    p = Pace()
    feed(p, 1, 90000, 2)
    feed(p, 2, 95000, 3)
    assert p.of(1) is None and not p.quicker(1, 2) and not p.quicker(2, 1), "two laps are not a pace"


def test_a_lapped_car_with_more_pace_is_not_flagged():
    p = Pace()
    feed(p, 10, 91000)   # the lap leader behind
    feed(p, 20, 90000)   # the lapped car in front, quicker (a comeback)
    assert not p.quicker(10, 20), "the car behind is slower: no blue"
    assert p.quicker(20, 10), "and the other way round it would be"


def test_margin_and_hysteresis():
    p = Pace(3, 0.7)
    feed(p, 1, 90000)
    feed(p, 2, 90500)   # 0.55 % slower: inside the margin
    assert not p.quicker(1, 2)
    feed(p, 3, 91000)   # 1.1 % slower
    assert p.quicker(1, 3)
    feed(p, 4, 90500)
    assert p.quicker(1, 4, open_already=True) is True and not p.quicker(1, 4), "an open flag stays above half the margin"


def test_dirty_laps_do_not_count_and_a_slot_that_changes_hands_forgets():
    p = Pace()
    feed(p, 1, 90000, 3)
    p.lap(1, 150000, False)   # an in-lap, a cut, a caution lap
    assert p.of(1) == 90000
    p.forget(1)
    assert p.of(1) is None


def test_the_blue_rule_defaults_and_limits():
    r = Detectors().blue
    assert (r.yield_s, r.pace_laps, r.ladder, r.unserved_s) == (15, 3, [0, 5], 10)
    import pytest
    for bad in ({"yield_s": 1}, {"ladder": [0, 601]}, {"pace_margin_pct": -1}, {"x": 1}):
        with pytest.raises(ValueError):
            BlueRule(**bad)
