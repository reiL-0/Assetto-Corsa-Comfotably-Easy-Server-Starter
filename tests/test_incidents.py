"""Incident watch (app/live/incidents.py): a stopped or limping car on the track calls for a caution; a contact never does."""

from app.live.incidents import SLOW_LIMP_S, STOP_S, Watch


def moving(w, cars, t=0.0):
    w.step(t, {c: 200.0 for c in cars}, set(), True)


def test_contacts_alone_never_call_for_a_caution_however_hard_or_many():
    w = Watch()
    for t, (a, b) in enumerate(((1, 2), (2, 3), (3, None), (1, 3))):
        w.on_contact(100.0 + t, a, b, 250.0)
    assert w.step(105.0, {1: 200.0, 2: 200.0, 3: 200.0}, set(), True) is None, "NASCAR: cars touch; only a car that stops or limps is an incident"
    assert w.recent_involved(105.0, 15.0) == {1, 2, 3}, "but they are still 'involved' (no free pass)"


def test_slow_touches_and_old_contacts_do_not_count():
    w = Watch()
    w.on_contact(100.0, 1, 2, 5.0)
    w.on_contact(100.0, 3, 4, 10.0)
    assert not w.involved and w.step(101.0, {}, set(), True) is None
    w.on_contact(100.0, 1, 2, 80.0)
    w.on_contact(105.0, 3, 4, 80.0)
    assert w.step(120.0, {}, set(), True) is None
    assert w.recent_involved(120.0, 15.0) == set() and w.recent_involved(110.0, 15.0) == {1, 2, 3, 4}


def test_a_car_that_was_racing_and_stops_on_the_track_is_proposed_once_until_it_moves_again():
    w = Watch()
    moving(w, [7], -20.0)   # (it slowed down long ago: a car that braked from speed just now is a spin, see the last test)
    assert w.step(1.0, {7: 0.0}, set(), True) is None, "just stopped"
    assert w.step(1.0 + STOP_S - 0.5, {7: 0.0}, set(), True) is None
    p = w.step(1.0 + STOP_S + 0.1, {7: 0.0}, set(), True)
    assert p and p["kind"] == "stopped" and p["cars"] == [7]
    assert w.step(30.0, {7: 0.0}, set(), True) is None, "once"
    moving(w, [7], 40.0)
    assert w.step(41.0, {7: 0.0}, set(), True) is None and w.step(48.0, {7: 0.0}, set(), True)["kind"] in ("stopped", "mass_stop"), "it moved and stopped again"


def test_a_car_in_the_pits_one_that_never_moved_and_a_caution_already_out_are_not_proposed():
    w = Watch()
    moving(w, [7], 0.0)
    w.step(1.0, {7: 0.0, 8: 0.0}, {7}, True)
    assert w.step(20.0, {7: 0.0, 8: 0.0}, {7}, True) is None, "7 is in the pit lane, 8 never moved (a spectator)"
    w2 = Watch()
    moving(w2, [7], 0.0)
    w2.step(1.0, {7: 0.0}, set(), False)
    assert w2.step(20.0, {7: 0.0}, set(), False) is None, "not racing under green: nothing is proposed"
    w3 = Watch()
    moving(w3, [7], 0.0)
    w3.step(1.0, {7: 0.0}, set(), True)
    p = w3.step(20.0, {7: 0.0}, set(), True)
    w3.clear()
    assert w3.proposal is None and w3.step(40.0, {7: 0.0}, set(), True) is None, "after the caution the stopped car is not proposed again until it moves"
    assert p


def test_a_proposal_goes_stale():
    w = Watch()
    moving(w, [7], 0.0)
    w.step(1.0, {7: 0.0}, set(), True)
    w.step(20.0, {7: 0.0}, set(), True)
    assert w.proposal
    w.step(200.0, {7: 100.0}, set(), True)
    assert w.proposal is None


def test_stopped_time_before_the_race_does_not_count_and_a_reused_slot_starts_from_zero():
    w = Watch()
    moving(w, [7], 0.0)
    for t in (1.0, 5.0, 9.0):
        assert w.step(t, {7: 0.0}, set(), False) is None   # formation lap / caution: the car is stopped
    assert w.step(10.0, {7: 0.0}, set(), True) is None, "the six seconds start when the race is on"
    assert w.step(16.5, {7: 0.0}, set(), True)["cars"] == [7]
    w2 = Watch()
    moving(w2, [7], 0.0)
    w2.step(1.0, {}, set(), True)   # car 7 left
    w2.step(2.0, {7: 0.0}, set(), True)   # somebody else sits in slot 7 and never moves
    assert w2.step(20.0, {7: 0.0}, set(), True) is None


def test_a_car_far_under_the_pace_of_the_others_for_a_while_is_a_limping_car_proposed_once():
    w = Watch()
    pack = {1: 80.0, 2: 200.0, 3: 200.0, 4: 200.0}   # car 1 moves at 80 km/h: 40 % of the others' 200 is the limit (80 is not under it)
    for t in (0.0, 1.0):
        w.step(t, {**pack, 1: 200.0}, set(), True)   # (it was racing)
    pack[1] = 79.0
    p = None
    for t in range(2, 2 + int(SLOW_LIMP_S) - 1):
        assert w.step(float(t), pack, set(), True) is None, "not long enough yet"
    p = w.step(2.0 + SLOW_LIMP_S, pack, set(), True)
    assert p and p["kind"] == "limping" and p["cars"] == [1]
    assert w.step(40.0, pack, set(), True) is None, "once, until it is up to pace again"
    w.step(41.0, {**pack, 1: 200.0}, set(), True)
    w.step(42.0, pack, set(), True)
    assert w.step(42.0 + SLOW_LIMP_S, pack, set(), True)["cars"] == [1], "proposed again after it recovered"


def test_a_slow_car_in_the_pit_lane_or_when_the_whole_field_is_slow_is_not_limping():
    w = Watch()
    for t in (0.0, 1.0):
        w.step(t, {1: 200.0, 2: 200.0, 3: 200.0, 4: 200.0}, set(), True)
    for t in range(2, 40):
        assert w.step(float(t), {1: 50.0, 2: 200.0, 3: 200.0, 4: 200.0}, {1}, True) is None, "in the pit lane"
    w = Watch()
    for t in (0.0, 1.0):
        w.step(t, {1: 200.0, 2: 200.0, 3: 200.0, 4: 200.0}, set(), True)
    for t in range(2, 40):
        assert w.step(float(t), {1: 50.0, 2: 60.0, 3: 55.0, 4: 58.0}, set(), True) is None, "everybody is slow (a hairpin, a start)"


def test_the_pace_of_the_others_is_the_lower_median_and_a_limping_car_that_recovers_under_a_caution_can_be_proposed_again():
    w = Watch()
    for t in (0.0, 1.0):
        w.step(t, {1: 200.0, 2: 30.0, 3: 31.0, 4: 150.0, 5: 150.0}, set(), True)
    for t in range(2, 30):   # for car 1 the others are 30, 31, 150, 150: the lower median is 31 -- nowhere near a limp at 45 km/h
        assert w.step(float(t), {1: 45.0, 2: 30.0, 3: 31.0, 4: 150.0, 5: 150.0}, set(), True) is None
    w = Watch()
    pack = {2: 200.0, 3: 200.0, 4: 200.0}
    for t in (0.0, 1.0):
        w.step(t, {1: 200.0, **pack}, set(), True)
    assert any(w.step(float(t), {1: 60.0, **pack}, set(), True) for t in range(2, 20)), "limping"
    w.step(30.0, {1: 200.0, **pack}, set(), False)   # (under a caution it is up to pace again)
    assert any(w.step(float(t), {1: 60.0, **pack}, set(), True) for t in range(40, 60)), "and limping again after the restart"


def test_two_cars_braking_from_speed_to_a_near_stop_together_are_a_mass_stop_but_one_the_pits_or_a_slow_field_are_not():
    from app.live.incidents import DROP_S
    w = Watch()
    w.step(0.0, {1: 200.0, 2: 200.0, 3: 200.0}, set(), True)
    w.step(1.0, {1: 10.0, 2: 200.0, 3: 200.0}, set(), True)
    assert w.step(1.0 + DROP_S + 0.1, {1: 10.0, 2: 200.0, 3: 200.0}, set(), True) is None, "one car slowing is not a mass stop (alone it is a spin only after SPIN_S)"
    w.step(2.0, {1: 10.0, 2: 15.0, 3: 200.0}, set(), True)   # (car 2 was fast 1 s ago: it is within the window)
    p = w.step(2.0 + DROP_S + 0.1, {1: 10.0, 2: 15.0, 3: 200.0}, set(), True)
    assert p and p["kind"] == "mass_stop" and p["cars"] == [1, 2]
    assert w.step(20.0, {1: 10.0, 2: 15.0, 3: 200.0}, set(), True) is None, "once"
    w = Watch()
    w.step(0.0, {1: 200.0, 2: 200.0}, set(), True)
    w.step(1.0, {1: 10.0, 2: 10.0}, {1, 2}, True)
    assert w.step(10.0, {1: 10.0, 2: 10.0}, {1, 2}, True) is None, "in the pit lane"
    w = Watch()
    for t in (0.0, 1.0):
        w.step(t, {1: 50.0, 2: 60.0}, set(), True)
    w.step(2.0, {1: 10.0, 2: 10.0}, set(), True)
    assert w.step(10.0, {1: 10.0, 2: 10.0}, set(), True) is None, "never fast: a slow field (a hairpin) is not a wreck"
    w = Watch()
    w.step(0.0, {1: 200.0, 2: 200.0}, set(), True)
    w.step(1.0, {1: 10.0, 2: 10.0}, set(), False)
    assert w.step(10.0, {1: 10.0, 2: 10.0}, set(), False) is None, "under a caution nothing is proposed"



def test_cars_that_braked_before_the_race_went_green_are_not_a_mass_stop():
    w = Watch()
    w.step(0.0, {1: 200.0, 2: 200.0}, set(), False)   # (fast, but the race is not on)
    w.step(1.0, {1: 10.0, 2: 10.0}, set(), True)
    assert w.step(10.0, {1: 10.0, 2: 10.0}, set(), True) is None


def test_one_car_that_braked_to_a_near_stop_is_a_spin_after_a_few_seconds_or_sooner_after_a_contact():
    from app.live.incidents import DROP_S, SPIN_S
    pack = {2: 200.0, 3: 200.0}
    w = Watch()   # alone, no contact: a spin only when it lasts
    w.step(0.0, {1: 200.0, **pack}, set(), True)
    w.step(1.0, {1: 10.0, **pack}, set(), True)
    assert w.step(1.0 + DROP_S + 0.1, {1: 10.0, **pack}, set(), True) is None
    p = w.step(1.0 + SPIN_S + 0.1, {1: 10.0, **pack}, set(), True)
    assert p and p["cars"] == [1] and "trompeado" in p["text"]
    w = Watch()   # after a contact the short wait is enough
    w.step(0.0, {1: 200.0, **pack}, set(), True)
    w.on_contact(1.0, 1, 2, 250.0)
    w.step(3.0, {1: 10.0, **pack}, set(), True)
    p = w.step(3.0 + DROP_S + 0.1, {1: 10.0, **pack}, set(), True)
    assert p and p["cars"] == [1]
    w = Watch()   # in the pit lane it is a pit stop, and a car that never was fast is not a spin
    w.step(0.0, {1: 200.0, **pack}, set(), True)
    w.step(1.0, {1: 10.0, **pack}, {1}, True)
    assert w.step(20.0, {1: 10.0, **pack}, {1}, True) is None
