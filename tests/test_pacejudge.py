"""The continuous judge of the VSC / FCY pace (app/live/pacejudge.py): sustained excess only, fresh samples only, one verdict per car and flag."""

from app.live.field import Sample
from app.live.pacejudge import PaceJudge

LIMIT = 85.0   # pace 80 + tolerance 5


def feed(j, car, speeds, t0=100.0, dt=0.2, epoch=1, gen=0, flag=1, pit=()):
    """The packets of ONE car at 5 Hz with the given speeds; -> the breaches returned."""
    out = []
    for i, v in enumerate(speeds):
        t = t0 + i * dt
        out += j.step(t, flag, True, LIMIT, {car: Sample(0.1 + i * 0.001, epoch, gen, t, 0.0)}, {car: v}, set(pit))
    return out


def test_a_sustained_excess_is_a_breach_once():
    j = PaceJudge()
    out = feed(j, 3, [100.0] * 20)   # 4 s over
    assert [(d["car"], d["speed_kmh"]) for d in out] == [(3, 100)] and out[0]["over_s"] >= 1.5, out
    assert feed(j, 3, [100.0] * 20, t0=110.0) == [], "one verdict per car and flag: taking it back by hand does not arm it again"
    assert len(feed(j, 3, [100.0] * 20, t0=120.0, flag=2)) == 1, "a new flag is a new judgement"


def test_noise_is_not_a_breach():
    j = PaceJudge()
    assert feed(j, 3, [100.0, 80.0] * 12) == [], "one packet over, one under: the count starts again each time"
    assert feed(j, 4, [100.0] * 5) == [], "a second over the limit is not enough (1.5 s are needed)"
    assert feed(j, 5, [60.0] * 30) == [], "slower than the pace is never a breach"
    assert feed(j, 6, [3.0] * 30) == [], "a stopped car is not over anything"


def test_holes_pits_reanchoring_and_stale_samples_restart_the_count():
    for kw in ({"pit": [3]},):
        assert feed(PaceJudge(), 3, [100.0] * 20, **kw) == [], "in the pit lane"
    j = PaceJudge()
    out = feed(j, 3, [100.0] * 6) + feed(j, 3, [100.0] * 6, t0=102.0) + feed(j, 3, [100.0] * 4, t0=104.0)   # holes of ~1 s every 6 packets: never 1.5 s in a row
    assert out == [], "packets lost restart the count"
    j = PaceJudge()
    out = feed(j, 3, [100.0] * 4) + feed(j, 3, [100.0] * 4, t0=100.8, epoch=2) + feed(j, 3, [100.0] * 4, t0=101.6, epoch=3)
    assert out == [], "a progress that was re-anchored (teleport, reconnection) restarts it"
    j = PaceJudge()
    assert j.step(120.0, 1, True, LIMIT, {3: Sample(0.1, 1, 0, 100.0, 0.0)}, {3: 100.0}, set()) == [], "a stale sample is not counted"


def test_a_slot_that_changes_hands_is_a_new_driver_and_nothing_runs_while_not_active():
    j = PaceJudge()
    assert len(feed(j, 3, [100.0] * 20)) == 1
    assert len(feed(j, 3, [100.0] * 20, t0=110.0, gen=1)) == 1, "the new driver of the slot is judged on his own"
    j = PaceJudge()
    assert j.step(100.0, 1, False, LIMIT, {3: Sample(0.1, 1, 0, 100.0, 0.0)}, {3: 100.0}, set()) == []
    assert all(j.step(100.0 + i * 0.2, 1, False, LIMIT, {3: Sample(0.1, 1, 0, 100.0 + i * 0.2, 0.0)}, {3: 120.0}, set()) == [] for i in range(30)), "during the grace (not active) nobody is judged"


def test_the_log_is_bounded_and_says_who_why():
    j = PaceJudge()
    for flag in range(150):
        feed(j, 3, [100.0] * 10, flag=flag, t0=100.0 + flag * 10)
    assert len(j.log) == 100 and j.log[-1]["car"] == 3 and j.log[-1]["limit"] == 85 and j.log[-1]["flag"] == 149


def test_the_registry_judges_a_vsc_after_its_grace_only_and_shows_the_log():
    import time
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    rc.set_flag("vsc", 80, grace_s=10, tolerance_kmh=5, now=1000.0)
    rc.auto_off.add("pace")   # (kill switch, in the middle of the flag: the judge only logs)
    out = []
    for i in range(30):   # 6 s at 100 km/h, 5 s after the grace ended at 1010
        t = 100.0 + i * 0.2
        out += rc.pace_tick({3: Sample(0.1 + i * 0.001, 1, 0, t, 0.0)}, {3: 100.0}, t, 1000.0 + i * 0.2)
    assert out == [], "inside the grace nobody is judged"
    for i in range(30):
        t = 110.0 + i * 0.2
        out += rc.pace_tick({3: Sample(0.2 + i * 0.001, 1, 0, t, 0.0)}, {3: 100.0}, t, 1010.5 + i * 0.2)
    assert [d["car"] for d in out] == [3] and rc.autoflags_view()["pace"][-1]["car"] == 3 and not rc.penalties, "logged, and no penalty listed: the kill switch"
    rc.set_flag("green")
    assert rc.pace_tick({3: Sample(0.3, 1, 0, 120.0, 0.0)}, {3: 100.0}, 120.0, 1030.0) == [], "under green nobody is judged"


def test_garbage_speeds_restart_the_count_and_the_verdicts_do_not_pile_up():
    j = PaceJudge()
    nan = float("nan")
    assert feed(j, 3, [100.0] * 5 + [nan] + [100.0] * 5) == [], "a NaN between them: the count starts again, it cannot be walked through"
    j = PaceJudge()
    for flag in range(1, 40):
        feed(j, 3, [100.0] * 10, flag=flag, t0=100.0 + flag * 10)
    assert len(j._judged) == 1, "only the verdicts of the flag being judged are kept"


def _run_vsc(rc, t_start=110.0, wall_start=1010.5, speed=100.0, n=30, car=3):
    out = []
    for i in range(n):
        t = t_start + i * 0.2
        out += rc.pace_tick({car: Sample(0.2 + i * 0.001, 1, 0, t, 0.0)}, {car: speed}, t, wall_start + i * 0.2)
    return out


def test_the_judge_decides_by_default_the_drive_through_is_three_laps_and_the_director_can_change_it():
    import pytest
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    rc.set_flag("fcy", 80, grace_s=10, now=1000.0)   # no penalty given: the default
    assert rc.vsc_penalty == {"kind": "pits", "laps": 3, "warnings": 2, "severe_s": 10}, "the default: drive-through of 3 laps, 2 warnings, 10 s for the severe level"
    out = _run_vsc(rc)
    assert [d["car"] for d in out] == [3] and [(p["car_id"], p["laps"]) for p in rc.penalties] == [(3, 3)], "a breach gets the drive-through, 3 laps"
    assert rc.snapshot()["violations"] == [{"car_id": 3, "speed_kmh": 100}]
    assert _run_vsc(rc) == [] and len(rc.penalties) == 1, "once per car and flag"
    rc2 = RaceControl({"session_type": RACE_TYPE})
    rc2.set_flag("vsc", 80, grace_s=10, now=1000.0)
    rc2.set_pace_config(tolerance_kmh=30, penalty={"kind": "pits", "laps": 5})
    assert _run_vsc(rc2) == [] and rc2.vsc_penalty["laps"] == 5, "100 km/h is within 80 + 30"
    rc2.set_pace_config(tolerance_kmh=5)
    assert len(_run_vsc(rc2, t_start=130.0, wall_start=1030.5)) == 1 and rc2.penalties[0]["laps"] == 5
    for bad in ({"tolerance_kmh": 99}, {"penalty": {"kind": "pits", "laps": 9}}, {"penalty": {"kind": "dsq"}}):
        with pytest.raises(ValueError):
            rc2.set_pace_config(**bad)
    rc2.set_flag("green")
    with pytest.raises(ValueError):
        rc2.set_pace_config(tolerance_kmh=5)


def test_with_no_penalty_chosen_the_breach_is_only_recorded():
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    rc.set_flag("vsc", 80, grace_s=10, penalty={"kind": "none"}, now=1000.0)
    assert len(_run_vsc(rc)) == 1 and rc.penalties == [] and len(rc.violations) == 1


def test_switching_the_judge_in_the_middle_of_a_flag_never_makes_two_judges_punish_one_car():
    from app.live.racecontrol import RACE_TYPE, RaceControl, tick, _states

    rc = RaceControl({"session_type": RACE_TYPE})
    rc.set_flag("vsc", 80, grace_s=10, now=1000.0)
    assert len(_run_vsc(rc)) == 1 and len(rc.penalties) == 1
    rc.auto_off.add("pace")
    assert not rc.legacy and _run_vsc(rc, t_start=120.0, wall_start=1020.5, car=4)[0]["applied"] is False and len(rc.penalties) == 1, "switched off mid-flag: it only logs"
    rc.auto_off.clear()
    _states[907] = rc
    try:
        assert tick(1100.0, [(907, {3: {"velocity": [0, 0, 28.0]}})]) == [] and rc.evaluated is False and len(rc.penalties) == 1, "the old single judgement does not wake up in this flag"
    finally:
        _states.pop(907, None)
    leg = RaceControl({"session_type": RACE_TYPE})
    leg.auto_off.add("pace")
    leg.set_flag("vsc", 80, grace_s=10, now=1000.0)
    assert leg.legacy and leg.judging_continuously() is False
    leg.auto_off.clear()
    assert _run_vsc(leg)[0]["applied"] is False and leg.penalties == [], "a flag that started with the switch on is judged by the old judgement only"
