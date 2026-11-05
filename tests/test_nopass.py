"""No overtaking under a VSC / FCY (app/live/nopass.py): a pass that stays gives the passer 30 s to give the place back, or the penalty; obstacles and pit lane are no case."""

from app.live.field import Sample
from app.live.nopass import PassJudge

V = 100.0   # km/h of the cars unless said otherwise


def run(j, positions, seconds, t0, speeds=None, pit=(), flag=1, active=True, gens=None, dt=0.2):
    """positions: {car: fn(t) -> progress in laps} (t from t0). -> all events."""
    ev, t = [], t0
    while t - t0 < seconds:
        samples = {c: Sample(f(t - t0), 1, (gens or {}).get(c, 0), t, 0.0) for c, f in positions.items()}
        ev += j.step(t, flag, active, samples, speeds or {c: V for c in positions}, set(pit))
        t += dt
    return ev


def pass_at(t_pass, base=0.5, gap=0.01):   # car 2 starts `gap` behind car 1 and goes by it at t_pass, then stays ahead
    return lambda t: base - gap + (2 * gap) * min(1.0, max(0.0, (t - (t_pass - 1.0)) / 2.0)) if t > t_pass - 1.0 else base - gap


def test_a_pass_that_stays_opens_an_obligation_and_a_penalty_if_the_place_is_not_given_back():
    j = PassJudge()
    ev = run(j, {1: lambda t: 0.5, 2: pass_at(3.0)}, 10.0, 0.0)
    assert ev == [("open", 2, [1], 30)], ev
    ev = run(j, {1: lambda t: 0.5, 2: lambda t: 0.51}, 40.0, 10.0)
    assert [e for e in ev if e[0] == "due"] == [("due", 2, 0)] and j.log[-1]["car"] == 2, ev
    assert run(j, {1: lambda t: 0.5, 2: lambda t: 0.51}, 40.0, 50.0) == [], "once per car and flag"


def test_giving_the_place_back_clears_it_and_the_creditor_going_by_is_not_a_new_pass():
    j = PassJudge()
    run(j, {1: lambda t: 0.5, 2: pass_at(3.0)}, 6.0, 0.0)
    assert [o["car"] for o in j.open()] == [2]
    back = lambda t: 0.51 - 0.02 * min(1.0, t / 2.0)   # car 2 lifts and drops behind car 1 (0.49) over 2 s
    ev = run(j, {1: lambda t: 0.5, 2: back}, 12.0, 6.0)
    assert ("cleared", 2) in ev and not [e for e in ev if e[0] in ("open", "due")], ev
    assert run(j, {1: lambda t: 0.5, 2: lambda t: 0.49}, 60.0, 18.0) == []


def test_no_case_for_noise_slow_cars_the_pit_lane_the_grace_and_far_cars():
    assert run(PassJudge(), {1: lambda t: 0.5, 2: lambda t: 0.49 + (0.02 if (t % 2) > 1.6 else 0)}, 20.0, 0.0) == [], "ahead for 0.4 s then behind again: not a pass"
    assert run(PassJudge(), {1: lambda t: 0.5, 2: pass_at(3.0)}, 10.0, 0.0, speeds={1: 20.0, 2: V}) == [], "the passed car is slow: an obstacle"
    assert run(PassJudge(), {1: lambda t: 0.5, 2: pass_at(3.0)}, 10.0, 0.0, pit=[2]) == [], "in the pit lane"
    assert run(PassJudge(), {1: lambda t: 0.5, 2: pass_at(3.0)}, 10.0, 0.0, active=False) == [], "during the grace"
    assert run(PassJudge(), {1: lambda t: 0.5, 2: pass_at(3.0, gap=0.05)}, 10.0, 0.0) == [], "cars far apart are not compared"
    side = {1: lambda t: 0.5, 2: lambda t: 0.5 + 0.002 * (1 if (t % 1) < 0.5 else -1)}
    assert run(PassJudge(), side, 20.0, 0.0) == [], "side by side: neither ahead"


def test_a_new_driver_in_the_slot_a_teleport_and_a_new_flag_forget_the_order():
    j = PassJudge()
    run(j, {1: lambda t: 0.5, 2: lambda t: 0.49}, 3.0, 0.0)
    assert run(j, {1: lambda t: 0.5, 2: lambda t: 0.51}, 6.0, 3.0, gens={2: 1}) == [], "another driver: the order before is not his"
    j2 = PassJudge()
    run(j2, {1: lambda t: 0.5, 2: pass_at(3.0)}, 6.0, 0.0)
    assert j2.open()
    run(j2, {1: lambda t: 0.5, 2: lambda t: 0.51}, 1.0, 6.0, flag=2)
    assert j2.open() == [], "a new flag starts clean"


def test_the_registry_judges_passes_applies_the_flags_penalty_and_the_kill_switch_only_logs():
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    rc.set_flag("vsc", 80, grace_s=5, now=1000.0)
    ev = _drive_rc(rc, 10.0, 45.0)
    kinds = [e["kind"] for e in ev]
    assert "pass_open" in kinds and "pass_due" in kinds, kinds
    assert [(p["car_id"], p["laps"]) for p in rc.penalties] == [(2, 3)] and rc.violations[-1]["car_id"] == 2
    rc2 = RaceControl({"session_type": RACE_TYPE})
    rc2.set_flag("fcy", 80, grace_s=5, now=1000.0)
    rc2.auto_off.add("pass")
    ev = _drive_rc(rc2, 10.0, 45.0)
    assert not rc2.penalties and all(e["applied"] is False for e in ev if e["kind"] == "pass_due"), "the kill switch: logged, nothing applied"


def _drive_rc(rc, t_open, seconds):
    out, t = [], 0.0
    while t < seconds:
        samples = {1: Sample(0.5, 1, 0, t, 0.0), 2: Sample(pass_at(8.0)(t), 1, 0, t, 0.0)}
        out += rc.pass_tick(samples, {1: V, 2: V}, t, 1000.0 + t)
        t += 0.2
    return out


def test_the_obligation_is_the_drivers_not_the_slots_and_a_dropped_back_passer_has_given_it_back():
    j = PassJudge()
    run(j, {1: lambda t: 0.5, 2: pass_at(3.0)}, 6.0, 0.0)
    assert [o["car"] for o in j.open()] == [2]
    ev = run(j, {1: lambda t: 0.5, 2: lambda t: 0.51}, 40.0, 6.0, gens={2: 1})
    assert [o for o in ev if o[0] == "due"] == [] and j.open() == [], "a new driver in the slot owes nothing of the old one's"
    far = PassJudge()
    run(far, {1: lambda t: 0.5, 2: pass_at(3.0)}, 6.0, 0.0)
    ev = run(far, {1: lambda t: 0.5, 2: lambda t: max(0.30, 0.51 - 0.1 * t)}, 20.0, 6.0)   # drops well back, 0.2 of a lap
    assert ("cleared", 2) in ev and not [e for e in ev if e[0] == "due"], "falling far behind is giving the place back too"


def test_cars_a_lap_apart_that_run_together_are_compared_on_the_track():
    j = PassJudge()
    ev = run(j, {1: lambda t: 2.5, 2: lambda t: pass_at(3.0)(t) - 1.0 + 0.0}, 10.0, 0.0)   # car 2 is a lap behind in the count and goes by car 1 on the track
    assert [e for e in ev if e[0] == "open"] == [("open", 2, [1], 30)], ev


def test_one_penalty_per_car_and_flag_whichever_judge_catches_him():
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    rc.set_flag("vsc", 80, grace_s=5, now=1000.0)
    # the pace judge penalises car 2 first (100 km/h over a long time), then the pass judge finds the same car
    t = 0.0
    while t < 60.0:
        samples = {1: Sample(0.5, 1, 0, t, 0.0), 2: Sample(pass_at(8.0)(t), 1, 0, t, 0.0)}
        rc.pace_tick(samples, {1: 100.0, 2: 120.0}, t, 1000.0 + t)
        rc.pass_tick(samples, {1: 100.0, 2: 120.0}, t, 1000.0 + t)
        t += 0.2
    assert [(p["car_id"], p["laps"]) for p in rc.penalties].count((2, 3)) == 1, rc.penalties
