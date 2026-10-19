"""Incident watch (app/live/incidents.py): a pile-up or a stopped car on the track calls for a caution."""

from app.live.incidents import PILEUP_CARS, STOP_S, Watch


def moving(w, cars, t=0.0):
    w.step(t, {c: 200.0 for c in cars}, set(), True)


def test_three_cars_in_contacts_within_ten_seconds_are_a_pileup_and_two_are_not():
    w = Watch()
    w.on_contact(100.0, 1, 2, 80.0)
    assert w.step(101.0, {1: 50.0, 2: 50.0, 3: 50.0}, set(), True) is None, "two cars are a contact, not a pile-up"
    w.on_contact(104.0, 3, None, 60.0)   # car 3 hits the wall
    p = w.step(105.0, {1: 50.0, 2: 50.0, 3: 50.0}, set(), True)
    assert p and p["kind"] == "pileup" and p["cars"] == [1, 2, 3] and PILEUP_CARS == 3
    assert w.step(106.0, {}, set(), True) is None, "the same pile-up is not proposed twice"


def test_slow_touches_and_old_contacts_do_not_count():
    w = Watch()
    w.on_contact(100.0, 1, 2, 5.0)
    w.on_contact(100.0, 3, 4, 10.0)
    assert not w.involved and w.step(101.0, {}, set(), True) is None
    w.on_contact(100.0, 1, 2, 80.0)
    w.on_contact(105.0, 3, 4, 80.0)
    assert w.step(120.0, {}, set(), True) is None, "the first contact is older than the window by the time of the third car"
    assert w.recent_involved(120.0, 15.0) == set() and w.recent_involved(110.0, 15.0) == {1, 2, 3, 4}


def test_a_car_that_was_racing_and_stops_on_the_track_is_proposed_once_until_it_moves_again():
    w = Watch()
    moving(w, [7], 0.0)
    assert w.step(1.0, {7: 0.0}, set(), True) is None, "just stopped"
    assert w.step(1.0 + STOP_S - 0.5, {7: 0.0}, set(), True) is None
    p = w.step(1.0 + STOP_S + 0.1, {7: 0.0}, set(), True)
    assert p and p["kind"] == "stopped" and p["cars"] == [7]
    assert w.step(30.0, {7: 0.0}, set(), True) is None, "once"
    moving(w, [7], 40.0)
    assert w.step(41.0, {7: 0.0}, set(), True) is None and w.step(48.0, {7: 0.0}, set(), True)["kind"] == "stopped", "it moved and stopped again"


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
