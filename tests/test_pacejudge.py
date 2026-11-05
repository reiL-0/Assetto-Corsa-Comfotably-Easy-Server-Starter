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
    out = []
    for i in range(30):   # 6 s at 100 km/h, 5 s after the grace ended at 1010
        t = 100.0 + i * 0.2
        out += rc.pace_tick({3: Sample(0.1 + i * 0.001, 1, 0, t, 0.0)}, {3: 100.0}, t, 1000.0 + i * 0.2)
    assert out == [], "inside the grace nobody is judged"
    for i in range(30):
        t = 110.0 + i * 0.2
        out += rc.pace_tick({3: Sample(0.2 + i * 0.001, 1, 0, t, 0.0)}, {3: 100.0}, t, 1010.5 + i * 0.2)
    assert [d["car"] for d in out] == [3] and rc.autoflags_view()["pace"][-1]["car"] == 3 and not rc.penalties, "logged, and no penalty listed: shadow"
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
