"""Caution procedures (app/live/caution.py) and the progress they read (app/live/field.py): pure state fed with numbers."""

from app.live.caution import GIVE_BACK_HOLD, GIVE_BACK_MAX, GIVE_BACK_MIN, PASS_HOLD, Caution
from app.live.field import Field, ring


def test_progress_does_not_wrap_at_the_line_and_a_grid_car_starts_behind_it():
    f = Field()
    f.update(1, 0.995, laps=0)
    f.update(2, 0.30, laps=3)
    assert round(f.progress[1], 6) == -0.005 and f.progress[2] == 3.30
    f.update(1, 0.003)   # crosses the line: a few metres, not a lap
    f.update(2, 0.299)   # (a tiny step back is a step back, not a lap)
    assert round(f.progress[1], 6) == 0.003 and round(f.progress[2], 6) == 3.299
    f.update(1, 0.5); f.update(1, 0.99); f.update(1, 0.01)
    assert round(f.progress[1], 6) == 1.01 and f.order() == [2, 1]
    assert round(ring(0.9), 6) == -0.1 and ring(0.5) == -0.5


def field(*prog):
    return {i + 1: p for i, p in enumerate(prog)}


def test_the_order_is_frozen_by_progress_and_the_lapped_cars_are_a_lap_or_more_behind_the_leader():
    c = Caution(field(5.20, 5.15, 4.90, 3.95, 4.10), 0.0)
    assert c.frozen == [1, 2, 3, 5, 4] and c.leader == 1
    assert c.lapped() == [5, 4], "a car 0.30 of a lap behind is on the lead lap; one 1.0 or more behind is lapped"
    assert Caution(field(5.20, 4.21), 0.0).lapped() == [], "0.99 behind is still the lead lap"


def test_free_pass_uses_completed_laps_not_distance_from_leader_on_track():
    # A lapped car can be physically just ahead of the leader; a lead-lap car
    # can appear one lap behind in an uncorrected spline estimate.
    c = Caution({1: 5.20, 2: 4.22, 3: 4.10}, 0, completed_laps={1: 5, 2: 4, 3: 5})
    assert c.lapped() == [2] and c.free_pass == 2
    c = Caution({1: 5.20, 2: 4.10}, 0, completed_laps={1: 5, 2: 5})
    assert c.lapped() == [] and c.free_pass is None
    c = Caution({1: 5.20, 2: 5.15, 3: 4.22, 4: 4.10}, 0,
                completed_laps={1: 5, 2: 5, 3: 4, 4: 4}, involved=[3])
    assert c.free_pass is None, "an ineligible first lapped car does not hand the pass to the next"
    c = Caution({1: 5.20, 2: 4.10, 3: 3.90}, 0, completed_laps={1: 5, 3: 4})
    assert c.lapped() == [3] and c.free_pass == 3, "unknown counters are not inferred from spline progress"
    c = Caution({1: 5.20, 2: 4.10}, 0, completed_laps={})
    assert c.free_pass is None, "no official lap data means no automatic award"
    c = Caution({1: 5.20, 2: 4.10}, 0, completed_laps={1: 5, 2: 4})
    c.remove(1)
    assert c.lapped() == [] and c.free_pass == 2, "a removed pace car cannot remain the lap leader"


def test_the_free_pass_is_the_first_lapped_car_and_only_if_it_is_eligible_never_the_next_one():
    p = field(5.20, 5.15, 4.10, 3.95)
    assert Caution(p, 0.0).free_pass == 3
    assert Caution(p, 0.0, involved=[3]).free_pass is None, "the first lapped car is involved: no free pass"
    assert Caution(p, 0.0, pitted=[3]).free_pass is None
    assert Caution(p, 0.0, penalised=[3]).free_pass is None
    assert Caution(field(5.20, 5.15), 0.0).free_pass is None, "nobody is lapped"


def run(c, steps, speeds=None, exempt=()):
    """steps = [(t, progress)]; returns the cars that became violators."""
    out = []
    for t, p in steps:
        out += c.check(p, speeds if speeds is not None else {k: 60.0 for k in p}, t, exempt)
    return out


def test_a_pass_that_lasts_is_a_violation_once_and_a_short_swap_is_not():
    start = field(5.20, 5.19, 5.18)   # cars 1, 2, 3 in a line, 0.01 apart
    c = Caution(start, 0.0)
    swapped = field(5.20, 5.215, 5.18)   # car 2 is now in front of car 1 by 0.015
    assert run(c, [(1.0, start), (1.5, swapped), (2.0, swapped)]) == [], "less than PASS_HOLD"
    assert run(c, [(2.2, start)]) == [] and run(c, [(3.0, swapped)]) == [], "it went back: the timer restarted"
    t = 3.0 + PASS_HOLD + 0.1
    assert run(c, [(3.5, swapped), (t, swapped)]) == [], "held, but it is judged at the passer's next crossing of the line"
    assert run(c, [(t + GIVE_BACK_MIN + 0.1, swapped)]) == [], "20 s later, but car 2 has not crossed the line yet"
    assert run(c, [(t + GIVE_BACK_MAX + 0.1, swapped)]) == [2], "a passer that never crosses is judged after GIVE_BACK_MAX"
    assert run(c, [(300.0, swapped), (310.0, swapped)]) == [], "once per caution"
    assert c.violators == {2}


def test_the_pass_is_judged_at_the_next_crossing_of_the_line_and_giving_the_place_back_before_it_clears_the_passer():
    start = field(5.985, 5.975)
    swapped = field(5.985, 5.995)   # car 2 passed car 1 just before the line
    c = Caution(start, 0.0)
    run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped)])
    t = 1.0 + PASS_HOLD + 0.1
    assert run(c, [(t + 5, field(6.01, 6.03))]) == [], "crossed the line, but less than GIVE_BACK_MIN after the pass"
    assert run(c, [(t + GIVE_BACK_MIN - 1, field(6.50, 6.52))]) == [], "it only crossed early: the end of its lap is the next crossing, at least GIVE_BACK_MIN after the pass"
    assert run(c, [(t + GIVE_BACK_MIN + 0.1, field(7.01, 7.03))]) == [2], "crossed the line again, still in front: the drive-through"
    c = Caution(start, 0.0)   # the same pass, but car 2 lets car 1 by before the line
    run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped)])
    behind = field(6.01, 6.0)
    assert run(c, [(t + 3, behind), (t + 3 + GIVE_BACK_HOLD + 0.1, behind), (t + GIVE_BACK_MIN + 1, field(7.20, 7.19))]) == [], "gave the place back"
    c = Caution(start, 0.0)   # behind for less than GIVE_BACK_HOLD does not count
    run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped)])
    assert run(c, [(t + 3, behind), (t + 3.5, field(6.0, 6.02)), (t + GIVE_BACK_MIN + 1, field(7.01, 7.03))]) == [2]


def test_giving_the_place_back_within_the_hold_is_no_violation_and_a_passed_car_that_reaches_the_pit_lane_clears_the_passer():
    start = field(5.20, 5.19)
    swapped = field(5.20, 5.215)
    c = Caution(start, 0.0)
    assert run(c, [(1.0, swapped), (1.0 + PASS_HOLD - 0.5, swapped), (1.0 + PASS_HOLD - 0.4, start), (20.0, start)]) == [], "passed and gave it back inside PASS_HOLD"
    c = Caution(start, 0.0)   # the pack goes by a car that is heading to the pits: it reports the pit lane while the pass is pending
    run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped)])
    assert run(c, [(1.0 + PASS_HOLD + 2.0, swapped)], exempt={1}) == [] and run(c, [(GIVE_BACK_MAX + 30.0, swapped)]) == [] and not c.violators


def test_passing_a_stopped_car_a_car_in_the_pits_or_one_directed_to_pass_is_not_a_violation():
    start = field(5.20, 5.19)
    swapped = field(5.20, 5.215)
    c = Caution(start, 0.0)
    assert run(c, [(1.0, swapped), (4.0, swapped)], speeds={1: 5.0, 2: 60.0}) == [], "car 1 is (nearly) stopped"
    c = Caution(start, 0.0)
    assert run(c, [(1.0, swapped), (4.0, swapped)], speeds={1: 60.0, 2: 60.0}, exempt={2}) == [], "the passer is in the pit lane"
    c = Caution(start, 0.0)
    c.allow(2)
    assert run(c, [(1.0, swapped), (4.0, swapped)], speeds={1: 60.0, 2: 60.0}) == []
    c = Caution(start, 0.0)
    far = field(5.20, 5.40)   # not close: not compared, whatever the order
    assert run(c, [(1.0, far), (9.0, far)], speeds={1: 60.0, 2: 60.0}) == []


def test_a_pass_of_a_lapped_car_by_the_lead_lap_is_not_a_swap_of_the_frozen_order():
    # car 3 is a lap down and 0.02 ahead of the leader ON THE TRACK: it stays there; the leader behind it is not passing it
    p = field(5.20, 5.10, 4.22)
    c = Caution(p, 0.0)
    assert run(c, [(1.0, p), (9.0, field(5.21, 5.11, 4.23))], speeds={1: 60.0, 2: 60.0, 3: 60.0}) == []


def test_the_wave_around_is_the_lapped_cars_between_the_leader_and_the_pace_car_but_the_free_pass_one():
    # leader 1 at 5.20; lapped: 3, 4, 6 are two laps down and 0.02, 0.03 and 0.005 AHEAD of the leader on the track; 5 is a lap down and behind it
    p = field(5.20, 5.15, 3.22, 3.23, 4.10, 3.205)
    c = Caution(p, 0.0)
    assert c.free_pass == 5, "the first lapped car of the frozen order"
    assert c.wave_around(p, pace_progress=5.24) == [4, 3, 6], "nearest the pace car first; car 5 is the free pass one, car 2 is on the lead lap"
    assert Caution(p, 0.0, involved=[3, 4, 6]).wave_around(p, 5.24) == [], "involved cars are not waved"
    assert Caution(p, 0.0, pitted=[4]).wave_around(p, 5.24) == [3, 6]


def test_the_restart_order_follows_the_rulebook_groups():
    # 1, 2, 3 lead lap (3 pitted); 4 lapped; 5 lapped (free pass is 4); 6 involved on the lead lap; 7 lapped and waved; 8 with a penalty
    p = field(9.9, 9.8, 9.7, 8.5, 8.4, 9.6, 8.3, 9.5)
    c = Caution(p, 0.0, involved=[6], pitted=[3], penalised=[8])
    assert c.free_pass == 4
    assert c.restart_order(waved=[7]) == [1, 2, 3, 5, 4, 6, 7, 8]
    assert c.restart_order(waved=[7], pitted_exit=[3]) == [1, 2, 3, 5, 4, 6, 7, 8]
    assert Caution(field(9.9, 9.8, 9.7), 0.0, pitted=[2, 3]).restart_order(pitted_exit=[3, 2]) == [1, 3, 2], "lead lap cars that pitted go in the order they left the pits"


def test_a_teleport_or_a_wrong_first_lap_count_is_put_right_by_the_laps_the_board_knows():
    f = Field()
    f.update(1, 0.80, laps=5)
    f.update(1, 0.10, laps=5)   # reset to the pits: not a lap forward
    assert round(f.progress[1], 6) == 5.10
    g = Field()
    g.update(2, 0.80)   # laps not known yet
    g.update(2, 0.90, laps=4)
    assert g.progress[2] == 4.90, "the board knew better a moment later"
    g.update(2, 0.95, laps=4)
    assert round(g.progress[2], 6) == 4.95 and g.snapshot() == g.progress and g.snapshot() is not g.progress


def test_a_car_that_leaves_during_the_caution_is_never_judged_again_even_if_the_slot_is_reused():
    start = field(5.20, 5.19)
    c = Caution(start, 0.0)
    swapped = field(5.20, 5.215)
    run(c, [(1.0, swapped)])                           # the swap starts
    run(c, [(2.0, {1: 5.20})])                          # car 2 leaves
    assert 2 in c.gone and not c._since and not c._signs
    assert run(c, [(5.0, swapped), (9.0, swapped)]) == [], "somebody else took slot 2: not judged for what the old driver did"


def test_the_pass_of_a_car_whose_speed_is_unknown_is_not_judged_and_a_voided_free_pass_may_not_pass():
    start, swapped = field(5.20, 5.19), field(5.20, 5.215)
    c = Caution(start, 0.0)
    assert run(c, [(1.0, swapped), (4.0, swapped)], speeds={2: 60.0}) == [], "the passed car's speed is not known"
    p = field(5.20, 4.10)
    c = Caution(p, 0.0)
    assert c.free_pass == 2 and 2 in c.allowed
    assert c.void_free_pass() == 2 and c.free_pass is None and 2 not in c.allowed


def test_closed_pits_cost_a_drive_through_unless_there_before_involved_serving_or_let_in_and_only_once():
    p = field(5.20, 5.19, 5.18, 5.17, 5.16)
    c = Caution(p, 0.0, involved=[2], pitted=[3])
    for car in (1, 2, 3, 4, 5):
        c.pit_enter(car)
    c.pit_allow(5, True)
    c.pit_enter(5)
    assert c.pit_breakers(serving={4}) == [1], "2 is involved, 3 was in the pits, 4 serves a drive-through, 5 was let in"
    c.pit_enter(1)
    assert c.pit_breakers() == [] and 1 in c.violators, "once per caution; it restarts at the tail"
    c.pit_allow(4, False); c.pit_enter(4)
    assert c.pit_breakers() == [4], "no permission, nothing to serve: judged"


def test_a_car_that_joined_after_the_yellow_and_anybody_in_an_open_pit_lane_are_not_judged_for_the_pits():
    c = Caution(field(5.20, 5.10), 0.0)
    c.pit_enter(9)   # joined the server under the yellow: it appears in its box
    c.pit_enter(1)
    assert c.pit_breakers() == [1], "a car of the field that enters the closed pit lane is still a violator"
    c = Caution(field(5.20, 5.10), 0.0)
    c.pit_enter(1)
    c.open_pits()
    c.pit_enter(2)
    assert c.pit_breakers() == [1], "the entry before the pits opened is still judged, the one after is not"


def test_the_leader_going_by_a_lapped_car_that_is_physically_ahead_of_it_is_not_a_pass():
    # car 3 is a lap down and 0.02 ahead of the leader ON THE TRACK; the leader catches it and goes by: the frozen order puts the leader in front, so it is getting its place, not passing
    c = Caution(field(5.20, 5.10, 4.22), 0.0)
    swapped = field(5.25, 5.15, 4.23)
    assert run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped), (GIVE_BACK_MAX + 30.0, swapped)], speeds={1: 60.0, 2: 60.0, 3: 60.0}) == [] and not c.violators and not c._pending


def test_a_car_that_went_wide_and_crawls_back_is_not_passed_illegally_and_may_take_its_place_again():
    start = field(5.20, 5.19)
    swapped = field(5.20, 5.215)   # car 2 (behind in the frozen order) goes by car 1...
    c = Caution(start, 0.0)
    slow = {1: 30.0, 2: 80.0}      # ...which is at 30 km/h against 80: it went wide, it is crawling
    assert run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped), (GIVE_BACK_MAX + 30.0, swapped)], speeds=slow) == [] and not c._pending
    again = field(5.25, 5.22)   # car 1 recovers and goes by car 2: it was ahead in the frozen order, so it is its own place
    assert run(c, [(200.0, again), (200.0 + PASS_HOLD + 0.1, again), (400.0, again)], speeds={1: 80.0, 2: 80.0}) == [] and not c.violators


def test_several_cars_going_by_the_same_one_are_for_the_director_and_nobody_is_punished_by_the_automatic_judge():
    start = field(5.30, 5.29, 5.28, 5.27)   # car 1 in front, then 2, 3, 4
    c = Caution(start, 0.0)
    swapped = field(5.30, 5.315, 5.31, 5.27)   # 2 and 3 go by car 1 (which is slow), 4 stays behind it
    run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped)], speeds={1: 70.0, 2: 80.0, 3: 80.0, 4: 80.0})
    assert run(c, [(GIVE_BACK_MAX + 30.0, swapped)], speeds={1: 70.0, 2: 80.0, 3: 80.0, 4: 80.0}) == [] and not c.violators
    assert c.reviews == [{"passed": 1, "passers": [2, 3]}] and c.snapshot()["reviews"] == c.reviews


def test_a_car_leaving_the_pits_is_not_judged_even_if_its_report_lapses_before_the_move_is_confirmed():
    start = field(5.20, 5.19)
    swapped = field(5.20, 5.215)
    c = Caution(start, 0.0)
    run(c, [(1.0, swapped)], exempt={2})   # the move begins while car 2 is reported in the pit lane (just leaving it)
    assert run(c, [(1.0 + PASS_HOLD + 0.1, swapped), (GIVE_BACK_MAX + 30.0, swapped)]) == [] and not c._pending


def test_a_car_that_leaves_or_becomes_the_pace_car_leaves_no_trace_in_the_passes_and_the_reviews():
    start = field(5.30, 5.29, 5.28)
    swapped = field(5.30, 5.315, 5.31)
    c = Caution(start, 0.0)
    sp = {1: 70.0, 2: 80.0, 3: 80.0}
    run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped)], speeds=sp)
    run(c, [(GIVE_BACK_MAX + 30.0, swapped)], speeds=sp)
    assert c.reviews == [{"passed": 1, "passers": [2, 3]}]
    c.remove(3)
    assert c.reviews == [] and all(3 != x for seen in c._passed_by.values() for _, x in seen), "one passer is no cascade any more"
    c = Caution(start, 0.0)
    run(c, [(1.0, swapped), (1.0 + PASS_HOLD + 0.1, swapped)], speeds=sp)
    run(c, [(30.0, {1: 5.30, 2: 5.315})], speeds=sp)   # car 3 disappeared from the track list
    assert 3 in c.gone and all(3 not in k for k in c._pending) and all(x != 3 for seen in c._passed_by.values() for _, x in seen)


def test_the_cascade_window_is_shorter_than_the_wait_before_a_verdict_and_the_history_does_not_grow_for_ever():
    from app.live.caution import CASCADE_S
    assert CASCADE_S < GIVE_BACK_MIN
    c = Caution(field(5.30, 5.29), 0.0)
    a, b = field(5.30, 5.315), field(5.30, 5.29)
    t = 1.0
    for _ in range(40):   # car 2 passes car 1 and gives the place back, again and again
        run(c, [(t, a), (t + PASS_HOLD + 0.1, a), (t + PASS_HOLD + 0.2, b), (t + PASS_HOLD + 0.2 + GIVE_BACK_HOLD + 0.1, b)])
        t += 30.0
    assert len(c._passed_by.get(1, [])) <= 6, "only the passes that can still be near a waiting one are kept"


def test_a_second_cascade_on_the_same_car_is_another_review_and_does_not_erase_the_first():
    c = Caution(field(5.30, 5.29, 5.28, 5.27, 5.26), 0.0)
    sp = {i: 80.0 for i in range(1, 6)}; sp[1] = 70.0
    first = field(5.30, 5.315, 5.31, 5.27, 5.26)          # 2 and 3 go by car 1
    run(c, [(1.0, first), (1.0 + PASS_HOLD + 0.1, first), (GIVE_BACK_MAX + 20.0, first)], speeds=sp)
    second = field(5.30, 5.61, 5.60, 5.32, 5.325)         # much later 2 and 3 are far away and 4 and 5 go by car 1 too
    t = GIVE_BACK_MAX + 100.0
    run(c, [(t, second), (t + PASS_HOLD + 0.1, second), (t + GIVE_BACK_MAX + 20.0, second)], speeds=sp)
    assert c.reviews == [{"passed": 1, "passers": [2, 3]}, {"passed": 1, "passers": [4, 5]}] and not c.violators
