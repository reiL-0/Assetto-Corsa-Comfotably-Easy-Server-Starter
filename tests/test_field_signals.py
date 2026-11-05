"""What Field says about how far a car's progress can be trusted (epoch, generation, stamp, since): the automatic flags decide from pairs of cars and must not read a reset as an overtake."""

from app.live.field import JUMP, Field


def test_the_line_is_crossed_without_a_new_epoch_and_the_progress_is_continuous():
    f = Field()
    f.update(1, 0.95, 3, now=1.0)
    f.update(1, 0.99, 3, now=1.2)
    f.update(1, 0.02, 4, now=1.4)   # the spline wraps and the lap counter moves: the same continuous motion
    s = f.samples()[1]
    assert s.epoch == 1 and s.since == 1.0 and s.stamp == 1.4, s
    assert abs(s.progress - 4.02) < 1e-9 and f.snapshot() == {1: s.progress}, "the old reading is unchanged"


def test_a_teleport_and_a_lap_counter_correction_start_a_new_epoch():
    f = Field()
    f.update(1, 0.10, 0, now=1.0)
    f.update(1, 0.12, 0, now=1.2)
    assert f.samples()[1].epoch == 1
    f.update(1, 0.50, 0, now=1.4)   # no car moves 0.38 of a lap in 0.2 s: it was put there
    s = f.samples()[1]
    assert s.epoch == 2 and s.since == 1.4, s
    f.update(1, 0.52, 7, now=1.6)   # the board says lap 7 while we had been counting laps 0: the counter wins, and the progress jumped by 7 laps
    assert f.samples()[1].epoch == 3 and f.samples()[1].since == 1.6


def test_a_dropped_slot_is_another_generation_even_when_the_same_driver_comes_back():
    f = Field()
    f.update(5, 0.30, 1, now=1.0)
    g0 = f.samples()[5].generation
    f.drop(5)
    assert 5 not in f.samples() and f.snapshot() == {}
    f.update(5, 0.30, 1, now=9.0)   # same slot, same place, same driver, maybe: a new connection all the same
    s = f.samples()[5]
    assert s.generation == g0 + 1 and s.epoch == 1 and s.since == 9.0, s
    f.drop(5); f.update(5, 0.3, 1, now=10.0)
    assert f.samples()[5].generation == g0 + 2


def test_a_new_session_clears_the_cars_and_says_so():
    f = Field()
    f.update(1, 0.5, 2, now=1.0)
    gen = f.samples()[1].generation
    f.clear()
    assert f.samples() == {} and f.session == 1
    f.update(1, 0.5, 2, now=2.0)
    assert f.samples()[1].generation == gen, "(the generation counts connections; the session counter counts sessions)"
    assert f.samples()[1].epoch == 1


def test_the_stamp_is_the_time_of_the_last_update_and_defaults_to_the_clock():
    f = Field()
    f.update(1, 0.1, 0)
    a = f.samples()[1].stamp
    f.update(1, 0.11, 0)
    assert f.samples()[1].stamp >= a > 0
    assert JUMP == 0.15
