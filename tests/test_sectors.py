"""The sector clock, the reference laps and the delta judge of the F1-style VSC (app/live/sectors.py), on a synthetic 4000 m track driven at 5 Hz."""

import math

from app.live.sectors import N, DeltaJudge, RefLaps, SectorClock

L = 4000.0


def drive(clock, car, speed_at, t0=0.0, p0=0.0, seconds=200.0, hz=5.0, epoch=1, gen=0, skip=()):
    """Drives a car: speed_at(progress in laps) in m/s. -> all events, the last (t, progress). `skip`: packet indexes that are lost."""
    t, p, ev, i = t0, p0, [], 0
    while t - t0 < seconds:
        if i not in skip:
            ev += clock.observe(car, p, t, epoch, gen)
        p += speed_at(p) / hz / L
        t += 1.0 / hz
        i += 1
    return ev, (t, p)


def speed(p):   # 30 to 70 m/s around the lap
    return 50 + 20 * math.sin(p * 2 * math.pi)


def crossings(ev): return [(e[1], e[2]) for e in ev if e[0] == "cross"]


def test_the_clock_interpolates_the_crossings_and_says_when_it_loses_the_thread():
    c = SectorClock()
    ev, _ = drive(c, 1, lambda p: 40.0, seconds=60.0)   # constant 40 m/s: a boundary every 200 m = 5 s exactly
    xs = crossings(ev)
    assert [b for b, _ in xs] == list(range(1, 12)) and all(abs(t - 5.0 * b) < 1e-6 for b, t in xs), xs
    assert c.observe(1, 0.99, 100.0, 1, 0) == [("reset",)], "a hole in the packets (and a jump back)"
    c2 = SectorClock()
    c2.observe(1, 0.10, 0.0, 1, 0)
    assert c2.observe(1, 0.11, 0.2, 2, 0) == [("reset",)], "re-anchored progress"
    assert c2.observe(1, 0.12, 0.4, 2, 1) == [("reset",)], "a new driver in the slot"
    assert c2.observe(1, 0.10, 0.6, 2, 1) == [("reset",)], "backwards"
    assert c2.observe(1, float("nan"), 0.8, 2, 1) == [("reset",)]
    c3 = SectorClock()
    c3.observe(1, -0.012, 0.0, 1, 0)
    assert [e[1] for e in c3.observe(1, 0.011, 0.2, 1, 0)] == [0], "the grid is behind the line: boundary 0 is crossed going over it"


def test_the_best_clean_lap_per_model_becomes_the_profile_and_dirty_laps_do_not_count():
    clock, ref = SectorClock(), RefLaps()
    ev, _ = drive(clock, 1, speed, seconds=400.0)
    ref.feed(1, "gt3", ev, False)
    lap, secs = ref.best["gt3"]
    assert len(secs) == N and abs(sum(secs) - lap) < 1e-6 and 60 < lap < 100
    prof = ref.profiles(1.35)["gt3"]
    assert len(prof) == N and all(abs(p - s * 1.35 * 1000) < 1 for p, s in zip(prof, secs))
    faster, ref2 = SectorClock(), RefLaps()
    ev2, _ = drive(faster, 1, lambda p: speed(p) * 1.1, seconds=400.0)
    ref2.feed(1, "gt3", ev2, False)
    assert ref2.best["gt3"][0] < lap, "a faster driver, a faster reference"
    for dirty in ("pit", "hole", "unknown model"):
        c, r = SectorClock(), RefLaps()
        e, _ = drive(c, 1, speed, seconds=400.0, skip=(150, 151, 152, 153, 154) if dirty == "hole" else ())
        r.feed(1, None if dirty == "unknown model" else "gt3", e, dirty == "pit")
        if dirty == "hole":
            assert "gt3" in r.best, "the laps either side of the hole are still whole laps"   # (one lap is lost, the next one is clean)
        else:
            assert r.best == {}, dirty
    hole_c, hole_r = SectorClock(), RefLaps()
    e, _ = drive(hole_c, 1, speed, seconds=85.0, skip=tuple(range(60, 70)))   # one lap only and a hole in it
    hole_r.feed(1, "gt3", e, False)
    assert hole_r.best == {}, "a lap with a hole is not a reference"


def _vsc(judge, clock, car, prof, speed_at, flag=1, seconds=120.0, gen=0, active=True, in_pit=False, t0=0.0, p0=0.3, since=0.0, skip=()):
    out = []
    t, p, i = t0, p0, 0
    while t - t0 < seconds:
        if i not in skip:
            out += judge.feed(t, flag, active, since, car, gen, prof, clock.observe(car, p, t, 1, gen), in_pit)
        p += speed_at(p) / 5.0 / L
        t += 0.2
        i += 1
    return out


def test_the_delta_judge_punishes_two_negative_sectors_in_a_row_and_only_once():
    ref_clock, ref = SectorClock(), RefLaps()
    ev, _ = drive(ref_clock, 1, speed, seconds=400.0)
    ref.feed(1, "gt3", ev, False)
    prof = ref.profiles(1.35)["gt3"]
    slow = lambda p: speed(p) / 1.50   # 50 % slower than the best lap: slower than the 1.35 allowance, as asked
    assert _vsc(DeltaJudge(), SectorClock(), 3, prof, slow) == [], "slower than needed: delta positive, nothing"
    fast = lambda p: speed(p) / 1.10   # only 10 % slower than the lap: faster than the VSC allows
    out = _vsc(DeltaJudge(), SectorClock(), 3, prof, fast)
    assert len(out) == 1 and out[0]["car"] == 3 and out[0]["delta_s"] < -0.2 and out[0]["strikes"] == 2, out
    j = DeltaJudge()
    c = SectorClock()
    assert len(_vsc(j, c, 3, prof, fast)) == 1 and _vsc(j, c, 3, prof, fast, t0=120.0, p0=0.0) == [], "once per flag, car and slot generation"
    assert len(_vsc(j, SectorClock(), 3, prof, fast, gen=1, t0=300.0)) == 1, "the new driver of the slot is judged on his own"
    assert len(_vsc(j, SectorClock(), 3, prof, fast, flag=2, t0=500.0)) == 1, "a new flag, a new judgement"


def test_one_bad_sector_a_hole_the_pit_lane_the_grace_and_a_missing_profile_do_not_punish():
    ref_clock, ref = SectorClock(), RefLaps()
    ev, _ = drive(ref_clock, 1, speed, seconds=400.0)
    ref.feed(1, "gt3", ev, False)
    prof = ref.profiles(1.35)["gt3"]
    fast = lambda p: speed(p) / 1.10
    assert _vsc(DeltaJudge(), SectorClock(), 3, prof, fast, in_pit=True) == [], "in the pit lane"
    assert _vsc(DeltaJudge(), SectorClock(), 3, prof, fast, active=False) == [], "during the grace"
    assert _vsc(DeltaJudge(), SectorClock(), 3, None, fast) == [], "no profile for his model: the fixed-pace judge has him"
    assert _vsc(DeltaJudge(), SectorClock(), 3, prof, fast, since=1e9) == [], "no sector counts before the judging started"
    spike = lambda p: speed(p) / 1.10 if 0.30 <= p % 1 < 0.345 else speed(p) / 1.50   # one fast sector in a slow lap: one strike, then it clears
    assert _vsc(DeltaJudge(), SectorClock(), 3, prof, spike) == [], "a single negative sector is only a strike"
    bridged = _vsc(DeltaJudge(), SectorClock(), 3, prof, fast, skip=tuple(range(0, 600, 5)))
    assert len(bridged) == 1, "a packet lost every second (0.4 s hole, under the 0.8 s limit) is bridged by the interpolation"
    broken = _vsc(DeltaJudge(), SectorClock(), 3, prof, fast, skip=tuple(i for i in range(0, 600) if i % 5 != 0))   # one packet in five: holes of 1 s break the thread every time
    assert broken == [], "holes longer than 0.8 s break the continuity: no complete sector, no verdict"


# ---- the registry: reference laps in green, a delta VSC, the fixed judge for a model with no lap, and the block a screen reads -----------------------------------------------------------------------
def _field(rc, cars, t0, seconds, models, in_green=True, wall0=1000.0, p0=None, gens=None):
    """Drives `cars` {car: speed function} for `seconds` through rc.pace_tick; -> the breaches."""
    from app.live.field import Sample
    out, p, t = [], dict(p0 or {c: 0.001 for c in cars}), t0
    while t - t0 < seconds:
        samples = {c: Sample(p[c], 1, (gens or {}).get(c, 0), t, 0.0) for c in cars}
        speeds = {c: cars[c](p[c]) * 3.6 for c in cars}
        out += rc.pace_tick(samples, speeds, t, wall0 + (t - t0), models)
        for c in cars:
            p[c] += cars[c](p[c]) * 0.2 / L
        t += 0.2
    return out, p


def test_a_delta_vsc_freezes_the_reference_judges_by_delta_and_falls_back_to_the_fixed_pace():
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    models = {1: "gt3", 2: "gt3", 3: "gt3", 4: "lmp"}
    rc.set_flag("vsc", 80, grace_s=5, mode="delta", now=1000.0)
    assert rc.vsc_mode == "delta" and rc.vsc_fallback, "no clean lap yet: delta mode, the cars are judged by the delta of their speed"
    blk0 = rc.delta_block("gt3")
    assert blk0 is not None and "min_ms" not in blk0 and blk0["limit_kmh"] == 80 + rc.tolerance_kmh and blk0["credit"] == 3.0 and rc.auto_live() == ["vsc"], "no reference lap: a virtual car at the limit speed, no track length needed"
    rc.set_flag("green")
    _field(rc, {1: speed, 2: lambda p: speed(p) * 0.9}, 0.0, 400.0, models)   # two gt3, in green racing: laps get timed
    assert "gt3" in rc.reflaps.best and "lmp" not in rc.reflaps.best
    rc.set_flag("vsc", 80, grace_s=5, mode="delta", now=1000.0)
    assert rc.vsc_mode == "delta" and not rc.vsc_fallback and set(rc.vsc_profiles) == {"gt3"} and len(rc.vsc_profiles["gt3"]) == 20
    frozen = {k: list(v) for k, v in rc.vsc_profiles.items()}
    blk = rc.delta_block("gt3")
    assert blk["mode"] == "delta" and blk["min_ms"] == frozen["gt3"] and blk["tol"] == 0.2
    assert "min_ms" not in rc.delta_block("lmp") and rc.delta_block("lmp")["limit_kmh"] == 80 + rc.tolerance_kmh   # lmp has no reference lap: the delta of its speed
    assert rc.delta_block(None)["limit_kmh"] == 85
    assert rc.auto_live() == ["vsc"] and rc.auto_for(1, 0, 500.0, None, "gt3")["vsc"]["min_ms"] == frozen["gt3"]
    # car 1 and 2 (gt3) drive 10 % slower than their lap: faster than a VSC allows; car 3 keeps well below; car 4 (lmp, no reference lap: the delta of its speed) drives at 120 km/h > 85 km/h
    bad = {1: lambda p: speed(p) / 1.10, 3: lambda p: speed(p) / 1.60, 4: lambda p: 120 / 3.6}
    out, p = _field(rc, bad, 500.0, 5.0, models, wall0=1000.0, p0={1: 0.30, 3: 0.30, 4: 0.30})   # the grace (5 s)
    assert out == [], "nobody is judged during the grace"
    out, _ = _field(rc, bad, 505.0, 80.0, models, wall0=1006.0, p0=p)
    kinds = {(d["car"], d["kind"]) for d in out}
    assert kinds == {(1, "delta"), (4, "delta")}, kinds
    assert sorted(x["car_id"] for x in rc.violations) == [1, 4] and sorted((x["car_id"], x["laps"]) for x in rc.penalties) == [(1, 3), (4, 3)]
    assert rc.vsc_profiles["gt3"] == frozen["gt3"], "the reference did not move while the VSC ran"
    assert rc.autoflags_view()["vsc_mode"] == "delta" and {x["car"] for x in rc.autoflags_view()["delta"]} == {1, 4}
    rc.set_flag("fcy", 80, grace_s=5, mode="delta", now=2000.0)
    assert rc.vsc_mode == "delta" and rc.delta_block("gt3") is not None, "an FCY supports delta mode too"
    assert rc.vsc_profiles == {} and not rc.vsc_fallback, "an FCY has no reference lap: its reference is the speed, even with reference laps known"
    fb1, fb2 = rc.delta_block("gt3"), rc.delta_block("lmp")
    assert fb1 == fb2 and "min_ms" not in fb1 and fb1["limit_kmh"] == 80 + rc.tolerance_kmh and fb1["credit"] == 3.0, "the same for every model: a virtual car at the limit speed"
    rc.set_flag("fcy", 80, grace_s=5, mode="fixed", now=2100.0)
    assert rc.vsc_mode == "fixed" and rc.delta_block("gt3") is None, "an FCY with mode=fixed is fixed pace"
    import pytest
    with pytest.raises(ValueError):
        rc.set_flag("vsc", 80, mode="f1")


def test_the_reference_does_not_learn_from_a_vsc_and_the_kill_switch_only_logs():
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    models = {1: "gt3"}
    rc.set_flag("vsc", 80, grace_s=5, now=1000.0)
    _field(rc, {1: speed}, 0.0, 300.0, models, wall0=1006.0)
    assert rc.reflaps.best == {}, "laps driven under a VSC are no reference"
    rc.set_flag("green")
    _field(rc, {1: speed}, 400.0, 300.0, models)
    rc.auto_off.add("pace")
    rc.set_flag("vsc", 80, grace_s=5, mode="delta", now=1000.0)
    out, _ = _field(rc, {1: lambda p: speed(p) / 1.10}, 800.0, 80.0, models, wall0=1006.0, p0={1: 0.3})
    assert rc.legacy and rc.vsc_mode == "fixed" and all(d["applied"] is False for d in out), "a flag that started with the switch on is judged by the old judgement only"


def test_the_pit_lane_without_a_crossing_and_a_neutralisation_spoil_a_lap_and_the_delta():
    c2, r2 = SectorClock(), RefLaps()
    e1, (t, p) = drive(c2, 1, speed, seconds=40.0)
    r2.feed(1, "gt3", e1, False)
    r2.feed(1, "gt3", [], True)
    e2, _ = drive(c2, 1, speed, t0=t, p0=p, seconds=100.0)
    r2.feed(1, "gt3", e2, False)
    assert r2.best == {}, "the lap in which the car went through the pits is not a reference"
    # the delta judge: a pit lane without a crossing clears delta and strikes
    ref_clock, rr = SectorClock(), RefLaps()
    evr, _ = drive(ref_clock, 1, speed, seconds=400.0)
    rr.feed(1, "gt3", evr, False)
    prof = rr.profiles(1.35)["gt3"]
    j, c = DeltaJudge(), SectorClock()
    fast = lambda p_: speed(p_) / 1.10
    out, (t, p) = [], (0.0, 0.3)
    # three seconds of fast sectors (a strike or two), a pit report with no crossing, then fast again: it needs two NEW sectors in a row after the pit lane
    evs = []
    for i in range(0, 40):
        evs = c.observe(3, p, t, 1, 0)
        out += j.feed(t, 1, True, 0.0, 3, 0, prof, evs, False)
        p += fast(p) * 0.2 / L; t += 0.2
        if j.state(3) and j.state(3)["strikes"] == 1:
            break
    assert j.state(3)["strikes"] == 1 and not out
    assert j.feed(t, 1, True, 0.0, 3, 0, prof, [], True) == [] and j.state(3) == {"delta": 0.0, "strikes": 0}, "the pit lane clears what was counted, with no crossing"


def test_the_start_of_the_judging_is_latched_once_and_a_green_drops_partial_laps():
    from app.live.field import Sample
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    models = {1: "gt3"}
    _field(rc, {1: speed}, 0.0, 300.0, models)
    rc.set_flag("vsc", 80, grace_s=5, mode="delta", now=1000.0)
    rc.pace_tick({1: Sample(0.5, 1, 0, 500.0, 0.0)}, {1: 100.0}, 500.0, 1006.0, models)   # the grace ended 1 s ago
    first = rc._since
    assert first is not None and abs(first - 499.0) < 1e-6
    rc.pace_tick({1: Sample(0.51, 1, 0, 500.2, 0.0)}, {1: 100.0}, 500.2, 5000.0, models)   # the wall clock jumps forward
    assert rc._since == first, "latched: a jump of the wall clock does not move it"
    rc.set_flag("green")
    assert rc._since is None
    rc.pace_tick({1: Sample(0.2, 1, 0, 600.0, 0.0)}, {1: 100.0}, 600.0, 6000.0, models)
    assert 1 not in rc.reflaps._cross, "no partial lap is kept while the flag is not green"


def test_an_fcy_is_judged_by_the_delta_of_its_speed_without_a_reference_lap_or_the_length_of_the_track():
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    models = {1: "gt3", 2: "gt3", 3: "gt3"}
    rc.set_flag("fcy", 60, grace_s=5, mode="delta", now=1000.0)   # limit = 60 + the tolerance
    limit = 60 + rc.tolerance_kmh
    assert rc.delta_block("gt3")["limit_kmh"] == limit and rc.delta_block("gt3").get("min_ms") is None
    cars = {1: lambda p: (limit + 55) / 3.6, 2: lambda p: (limit - 1) / 3.6, 3: lambda p: 30 / 3.6}
    _, p = _field(rc, cars, 0.0, 5.0, models, wall0=1000.0, p0={1: 0.3, 2: 0.3, 3: 0.3})   # the grace
    out, _ = _field(rc, cars, 5.0, 40.0, models, wall0=1006.0, p0=p)
    assert {(d["car"], d["kind"]) for d in out} == {(1, "delta")}, "55 km/h over the limit is a breach; just under it, or crawling, is nothing: " + str(out)
    assert sorted(x["car_id"] for x in rc.violations) == [1] and [x["car"] for x in rc.autoflags_view()["delta"]] == [1]


def test_the_delta_of_a_speed_banks_at_most_the_credit():
    from app.live.field import Sample
    from app.live.pacejudge import PaceDeltaJudge

    def drive(j, v, t0, secs):
        out, t = [], t0
        for _ in range(round(secs * 5)):
            t += 0.2
            out += j.step(t, 1, True, 65.0, {1: Sample(0.5, 1, 0, t, 0.0)}, {1: v}, set())
        return t, out

    j = PaceDeltaJudge()
    t, out = drive(j, 20.0, 0.0, 30.0)   # crawling for 30 s would bank 20 s if it could
    assert out == [] and j.state(1)["delta"] == 3.0, "the credit that can be banked is 3 s"
    t, out = drive(j, 130.0, t, 2.0)   # twice the limit: a second of delta lost for every second
    assert out == [] and abs(j.state(1)["delta"] - 1.0) < 0.1
    t, out = drive(j, 130.0, t, 2.0)
    assert len(out) == 1 and out[0]["car"] == 1 and out[0]["delta_s"] <= -0.2, "after 3.2 s over, the credit is spent and he is in breach, once"
    assert drive(j, 130.0, t, 5.0)[1] == [], "once per flag, car and slot generation"
    unlimited = PaceDeltaJudge(credit_s=1e9)
    t2, _ = drive(unlimited, 20.0, 0.0, 30.0)
    assert drive(unlimited, 130.0, t2, 4.0)[1] == [], "(without the limit the same car would not have been judged: the cap is what is tested)"
