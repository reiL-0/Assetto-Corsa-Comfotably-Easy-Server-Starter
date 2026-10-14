"""NASCAR rules engine (app/live/nascar.py): stages by percentage, the green finish, the caution window of a stage, overtime and the distance changed live. Pure state, fed with events."""

import pytest

from app.live.nascar import BREAK, DONE, DRAINING, OT_RUN, OT_WAIT, RACING, Nascar, stage_ends


def run_to(n: Nascar, lap: int, flag: str = "green") -> list[tuple]:
    """The leader completes every lap up to `lap` under `flag`; returns what the engine asked for along the way."""
    out = n.on_flag(flag) if flag != n.flag else []
    for i in range(n.laps + 1, lap + 1):
        out += n.on_leader_lap(i)
    return out


def kinds(actions: list[tuple]) -> list[tuple]:
    return [a for a in actions if a[0] != "say"]


def test_stages_split_by_percentage_and_the_last_one_ends_at_the_total():
    assert stage_ends(70) == [21, 53, 70]   # 30 % / 45 % / 25 %
    assert stage_ends(100, (50, 50)) == [50, 100]
    assert stage_ends(3) == [1, 2, 3]
    with pytest.raises(ValueError):
        Nascar(1)
    with pytest.raises(ValueError):
        Nascar(70, ends=[40, 30, 70])


def test_a_stage_ends_under_green_with_a_break_caution_and_the_director_restarts_it():
    n = Nascar(70)
    out = run_to(n, 21)
    assert n.phase == BREAK and ("flag", "yellow") in out, "the stage-break caution is asked for"
    assert n.on_flag("yellow") == [] and n.phase == BREAK, "the yellow the engine asked for comes back and changes nothing"
    n.on_flag("green")
    assert n.phase == RACING and n.stage == 1 and n.cautions_in_stage == 0
    assert n.snapshot()["stage_end"] == 53


def test_a_caution_in_the_last_laps_of_the_stage_ends_it_right_there_a_caution_earlier_does_not():
    n = Nascar(70)   # stage 1 = 21 laps: the window is 1 lap
    run_to(n, 18)
    n.on_flag("yellow")
    assert n.phase == RACING, "3 laps from the end of the stage: still racing, under caution"
    out = run_to(n, 21, "yellow")
    assert n.phase == BREAK and ("flag", "yellow") not in out, "ended at its lap under the caution already out: no second yellow"
    m = Nascar(70)
    run_to(m, 20)
    out = m.on_flag("fcy")
    assert m.phase == BREAK and any("termina aquí" in a[1] for a in out if a[0] == "say"), "inside the window: the stage ends there"


def test_the_window_shrinks_with_the_cautions_already_deployed_in_the_stage():
    n = Nascar(200)   # stages end at 60, 150, 200: a 60-lap stage has a window of 2 laps (2 %, no caution yet) and of 1 lap (1 %, one already)
    assert n.ends == [60, 150, 200]
    run_to(n, 57)
    n.on_flag("yellow")
    assert n.phase == RACING and n.cautions_in_stage == 1, "first caution: 2 %, 2 laps: 3 laps from the end is outside"
    n.on_flag("green")
    run_to(n, 58)
    n.on_flag("yellow")
    assert n.phase == RACING and n.cautions_in_stage == 2, "second caution: 1 %, 1 lap: 2 laps from the end is now outside"
    m = Nascar(200)
    run_to(m, 58)
    m.on_flag("yellow")
    assert m.phase == BREAK, "a first caution 2 laps from the end: inside the window of 2 %"
    k = Nascar(200)
    run_to(k, 57); k.on_flag("yellow"); k.on_flag("green"); run_to(k, 59)
    k.on_flag("yellow")
    assert k.phase == BREAK, "a second caution 1 lap from the end: inside the window of 1 %"


def test_yellow_to_fcy_is_one_caution_not_two():
    n = Nascar(200)
    n.on_flag("yellow")
    n.on_flag("fcy")
    n.on_flag("vsc")
    assert n.cautions_in_stage == 1
    n.on_flag("green")
    n.on_flag("vsc")
    assert n.cautions_in_stage == 2


def test_the_race_ends_at_the_total_under_green_and_drains_before_it_is_done():
    n = Nascar(70)
    run_to(n, 21); n.on_flag("green"); run_to(n, 53); n.on_flag("green")
    out = run_to(n, 70)
    assert n.phase == DRAINING and ("flag", "checkered") in out and ("finish",) in out
    assert n.on_leader_lap(71) == [] and n.phase == DRAINING, "nothing else happens while the field finishes"
    n.done()
    assert n.phase == DONE


def test_a_caution_when_the_leader_completes_the_distance_starts_an_overtime_that_ends_under_green():
    n = Nascar(70)
    n.stage = 2   # the final stage
    run_to(n, 69)
    n.on_flag("yellow")
    out = run_to(n, 70, "yellow")
    assert n.phase == OT_WAIT and kinds(out) == [], "extended, nothing finished"
    run_to(n, 72, "yellow")
    assert n.phase == OT_WAIT, "laps under caution while waiting do not end it"
    n.on_flag("green")
    assert n.phase == OT_RUN and n.attempts == 1 and n.snapshot()["ot_end"] == 74
    assert run_to(n, 73) == [] and n.phase == OT_RUN
    out = run_to(n, 74)
    assert n.phase == DRAINING and ("flag", "checkered") in out


def test_a_caution_in_the_overtime_or_at_its_end_is_another_attempt_until_they_are_spent():
    n = Nascar(70)
    n.stage = 2
    run_to(n, 70, "yellow")
    assert n.phase == OT_WAIT
    n.on_flag("green")
    run_to(n, 71)
    n.on_flag("yellow")
    assert n.phase == OT_WAIT and n.attempts == 1, "a caution before the end of the overtime laps: next attempt"
    n.on_flag("green")
    assert n.phase == OT_RUN and n.attempts == 2
    out = run_to(n, 73, "yellow")
    assert n.phase == OT_WAIT, "the overtime laps end under caution: next attempt"
    n.on_flag("green")
    assert n.phase == OT_RUN and n.attempts == 3
    out = n.on_flag("yellow")
    assert n.phase == DRAINING and ("flag", "checkered") in out and any("agotados" in a[1] for a in out if a[0] == "say"), "the third attempt failed: the race ends"


def test_the_distance_can_be_changed_while_racing_and_pending_stages_are_recomputed():
    n = Nascar(70)
    run_to(n, 21); n.on_flag("green")
    n.set_total(100)
    assert n.ends[0] == 21 and n.ends[-1] == 100 and n.ends[1] == 75, "the stage that is over keeps its end, the others follow the percentages"
    with pytest.raises(ValueError):
        n.set_total(21)   # not over the laps already run
    fixed = Nascar(70, ends=[20, 45, 70])
    with pytest.raises(ValueError):
        fixed.set_total(80)
    n.stage = 2
    run_to(n, 99)
    n.set_total(120)
    assert n.ends[-1] == 120 and n.phase == RACING


def test_repeated_or_late_lap_events_do_nothing():
    n = Nascar(70)
    run_to(n, 10)
    assert n.on_leader_lap(10) == [] and n.on_leader_lap(7) == [] and n.laps == 10
    run_to(n, 21)
    assert n.phase == BREAK
    assert n.on_leader_lap(21) == [], "the stage end is not applied twice"


def test_the_order_in_which_the_flag_and_the_lap_arrive_does_not_change_the_result():
    a, b = Nascar(70), Nascar(70)
    a.stage = b.stage = 2
    run_to(a, 69); run_to(b, 69)
    a.on_flag("yellow"); a.on_leader_lap(70)   # the flag first
    b.on_leader_lap(70); b.on_flag("yellow")   # the lap first (it was still green when the leader crossed)
    assert a.phase == OT_WAIT, "yellow before the lap: overtime"
    assert b.phase == DRAINING, "the leader crossed under green: the race was over when the yellow came"


def test_a_caution_on_the_first_lap_of_an_overtime_attempt_cancels_it_whatever_the_order():
    n = Nascar(70)
    n.stage = 2
    run_to(n, 70, "yellow")
    n.on_flag("green")
    assert n.phase == OT_RUN and n.attempts == 1
    n.on_flag("yellow"); n.on_leader_lap(71)   # the caution, then the lap that is run under it
    assert n.phase == OT_WAIT and n.attempts == 1, "the attempt failed once, not forgotten because a lap came in under the caution"


def test_changing_the_distance_during_a_stage_break_leaves_the_stage_that_just_ended_alone():
    n = Nascar(70)
    run_to(n, 21)
    assert n.phase == BREAK and n.ends[0] == 21
    n.set_total(100)
    assert n.ends[0] == 21 and n.ends[-1] == 100, "the stage that just ended keeps its end"


def test_the_distance_cannot_go_under_the_laps_the_engine_has_counted():
    n = Nascar(70)
    run_to(n, 30)
    with pytest.raises(ValueError):
        n.set_total(25)
