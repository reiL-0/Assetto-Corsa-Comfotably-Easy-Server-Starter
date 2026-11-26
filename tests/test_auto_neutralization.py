"""Automatic neutralization of a race that is not NASCAR: a car stopped on the track brings the VSC / FCY the server selected; a wreck (stopped + a lot of damage) or a car that never moves brings the safety car;
a slow car (a slow driver) never brings anything. Numbers calibrated on the production audit of 2026-11-07..14 (see ops/calibrate_neutralization.py)."""
import pytest

from app.live.field import Field
from app.live.racecontrol import RaceControl
from app.rulebook import Neutralization, SevereRule, SpinRule

CARS = 6   # cars 0..4 race, car 5 is the pace car


def race(monkeypatch, mode="live", category="", pace=True, **over):
    rc = RaceControl({"session_type": 3})
    rc.neutralization_rule = {**Neutralization(mode=mode, mode_by_category={"GT3": "FCY"}, **over).model_dump(), "category": category}
    if pace:
        rc.set_pace_car(CARS - 1)
    field, clock = Field(), [0.0]
    monkeypatch.setattr("app.live.racecontrol.time.monotonic", lambda: clock[0])

    def feed(t, speeds=None, damage=None):
        clock[0] = t
        for c in range(CARS):
            field.update(c, .1, now=t)
        for c, level in (damage or {}).items():
            rc.damage_report(c, level, 1000 + t)
        return rc.incident_step({c: (speeds or {}).get(c, 180) for c in range(CARS)}, field, 1000 + t)

    feed(0)   # everybody racing: a car that never moved is no stopped car
    return rc, feed


def run(feed, seconds, speeds, start=1, **kw):
    for t in range(start, start + seconds):
        feed(t, speeds, **kw)


def test_a_car_stopped_long_enough_brings_the_selected_vsc_or_fcy(monkeypatch):
    for category, expected in (("", "vsc"), ("GT3", "fcy")):
        rc, feed = race(monkeypatch, category=category)
        run(feed, 6, {2: 0})   # the existing 5 s confirmation (a spin): still only a local decision
        assert rc.flag == "green" and rc.autoflags.log[-1]["kind"] == "spin"
        run(feed, 4, {2: 0}, start=7)   # 8 s stopped
        assert rc.flag == expected, category
        assert rc.autoflags.log[-1]["kind"] == "stopped" and rc.autoflags.log[-1]["cars"] == [2]


def test_a_car_that_gets_going_again_before_the_threshold_brings_nothing(monkeypatch):
    rc, feed = race(monkeypatch)
    run(feed, 7, {2: 0})   # 6-7 s stopped
    run(feed, 6, {2: 120}, start=8)
    assert rc.flag == "green" and all(e.get("kind") != "stopped" for e in rc.autoflags.log)


def test_slow_cars_never_bring_a_flag_not_even_several_together(monkeypatch):
    rc, feed = race(monkeypatch)
    run(feed, 40, {0: 70, 1: 70, 2: 70})   # three slow drivers for 40 s, two at pace (the production audit: median 79 km/h): the old rule counted them as a pile-up
    assert rc.flag == "green" and not rc.notices
    assert rc.autoflags.log, "they are reported"
    assert all(e["kind"] == "slow_car" and e["decision"] == "local" for e in rc.autoflags.log)


def test_a_car_that_never_moves_brings_the_safety_car(monkeypatch):
    rc, feed = race(monkeypatch)
    run(feed, 59, {2: 0})
    assert rc.flag == "vsc" and rc.pace_car_state()["mode"] == "wait", "the VSC is out, the safety car is not yet"
    run(feed, 3, {2: 0}, start=60)   # 60 s
    assert rc.flag == "yellow" and rc.caution.pace_only and rc.pace_car_state()["mode"] == "wait"
    assert rc.autoflags.log[-1]["kind"] == "immobile" and rc.autoflags.log[-1]["decision"] == "SC"


def test_a_wreck_brings_the_safety_car_at_once_but_only_with_fresh_damage(monkeypatch):
    rc, feed = race(monkeypatch)
    run(feed, 6, {2: 0}, damage={2: 79})
    assert rc.flag == "green", "79 % is below the threshold"
    rc, feed = race(monkeypatch)
    run(feed, 6, {2: 0}, damage={2: 85})
    assert rc.flag == "yellow" and rc.autoflags.log[-1]["kind"] == "wreck", "stopped + 85 % damage: the safety car without waiting for the VSC"
    rc, feed = race(monkeypatch)   # the same wreck, but the hub's report is 40 s old
    rc.damage_report(2, 90, 1000 - 40)
    run(feed, 6, {2: 0})
    assert rc.flag == "green", "an old report is no report"
    rc, feed = race(monkeypatch)   # damage of a car that is still moving is the orange disc's business, not a wreck
    run(feed, 10, {}, damage={2: 95})
    assert rc.flag == "green"


def test_without_a_pace_car_the_safety_car_is_only_recommended(monkeypatch):
    rc, feed = race(monkeypatch, pace=False)
    run(feed, 62, {2: 0})
    assert rc.flag == "vsc" and "SC recomendado" in rc.notices[-1]["text"]


def test_shadow_records_the_decisions_and_does_nothing_visible(monkeypatch):
    rc, feed = race(monkeypatch, mode="shadow")
    run(feed, 62, {2: 0}, damage={2: 90})
    assert rc.flag == "green" and not rc.notices
    assert "wreck" in {e.get("kind") for e in rc.autoflags.log} and "stopped" not in {e.get("kind") for e in rc.autoflags.log}, "a wreck takes the safety car at once: it settles the VSC of that stop too"
    rc, feed = race(monkeypatch, mode="shadow")
    run(feed, 62, {2: 0})
    assert rc.flag == "green" and not rc.notices
    assert {"spin", "stopped", "immobile"} <= {e.get("kind") for e in rc.autoflags.log}


@pytest.mark.parametrize("off", [{"spin": SpinRule(escalate_s=0)}, {"severe": SevereRule(immobile_s=0, damage_pct=0)}])
def test_a_zero_switches_each_escalation_off(monkeypatch, off):
    rc, feed = race(monkeypatch, **off)
    run(feed, 70, {2: 0}, damage={2: 95})
    if "spin" in off:
        assert all(e.get("kind") != "stopped" for e in rc.autoflags.log)
    else:
        assert all(e.get("kind") not in ("wreck", "immobile") for e in rc.autoflags.log)


def test_the_rule_keeps_its_order_and_an_old_snapshot_gets_the_defaults():
    n = Neutralization()
    assert (n.spin.stopped_s, n.spin.escalate_s, n.severe.immobile_s, n.severe.damage_pct) == (5, 8, 60, 80)
    old = Neutralization.model_validate({k: v for k, v in n.model_dump().items() if k not in ("spin", "severe")} | {"spin": {"resume_kmh": 30, "clear_s": 5, "stopped_s": 5}, "severe": {"cars": 5}})
    assert (old.spin.escalate_s, old.severe.immobile_s) == (8, 60), "a reglamento saved before this validates and takes the defaults"
    assert Neutralization(spin=SpinRule(stopped_s=10)).spin.escalate_s == 8, "a league that only raises the confirmation does not become invalid"


def test_the_vsc_never_comes_before_the_stop_is_confirmed(monkeypatch):
    rc, feed = race(monkeypatch, spin=SpinRule(stopped_s=10, escalate_s=8))
    run(feed, 9, {2: 0})   # 9 s stopped: past escalate_s but not confirmed yet
    assert rc.flag == "green"
    run(feed, 3, {2: 0}, start=10)
    assert rc.flag == "vsc"


def test_the_damage_of_a_driver_who_left_is_not_inherited_by_the_next_one_in_the_slot(monkeypatch):
    rc, feed = race(monkeypatch)
    feed(1, {}, damage={2: 95})
    rc.drop_red(2)   # the car left the server (every connection_closed goes through here)
    run(feed, 6, {2: 0}, start=2)
    assert rc.flag == "green", "the new driver stopped in the slot with no damage of his own: no wreck"


def test_two_escalations_in_the_same_look_both_get_their_decision(monkeypatch):
    rc, feed = race(monkeypatch, mode="shadow", spin=SpinRule(escalate_s=0))   # (shadow: no safety car is deployed, so the watch keeps looking and both are recorded; live, the first SC ends the look)
    run(feed, 55, {1: 0})                       # car 1 stopped since t=1: immobile at 60 s
    run(feed, 5, {1: 0, 2: 0}, start=56, damage={2: 90})   # car 2 stopped at t=56 with a wreck: confirmed at 61 s (5 s), the same look
    run(feed, 3, {1: 0, 2: 0}, start=61, damage={2: 90})
    kinds = [(e.get("kind"), tuple(e.get("cars", ()))) for e in rc.autoflags.log if e.get("kind") in ("wreck", "immobile")]
    assert ("immobile", (1,)) in kinds and ("wreck", (2,)) in kinds, kinds


def test_a_spin_confirmed_in_the_same_look_as_an_escalation_is_still_judged_to_its_end(monkeypatch):
    rc, feed = race(monkeypatch)
    run(feed, 3, {1: 0})                        # car 1 stopped from t=1
    run(feed, 6, {1: 0, 2: 0}, start=4)         # car 2 from t=4: at t=9 car 1 reaches 8 s (VSC) and car 2 its 5 s (spin) together
    assert rc.flag == "vsc"
    assert 2 in rc._spins, "the spin of car 2 is registered, so its `spin_cleared` will come"


def test_a_pileup_confirmed_in_the_same_look_as_the_vsc_step_is_not_lost(monkeypatch):
    rc, feed = race(monkeypatch, mode="shadow", severe=SevereRule(cars=4))   # (shadow: the look goes on after the decision)
    run(feed, 3, {0: 0})                              # car 0 stopped from t=1: its 8 s are at t=9
    run(feed, 6, {0: 0, 1: 0, 2: 0, 3: 0}, start=4)   # cars 1-3 from t=4: their 5 s are at t=9 too: 4 cars in the window = severe
    assert [e["decision"] for e in rc.autoflags.log if e.get("kind") == "pileup"] == ["SC"], list(rc.autoflags.log)
