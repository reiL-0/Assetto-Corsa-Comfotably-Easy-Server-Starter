"""Shared NASCAR clearance, strict virtual countdown and local spin lifecycle."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.live.caution import Caution
from app.live.field import Sample
from app.live.racecontrol import INVOLVED_FOR, PIT_CLEAR, STANDING, RaceControl
from app.rulebook import Neutralization


@pytest.mark.parametrize("speeds,pits,contacts,stopped,involved,verdict", [
    ({1: 0}, {}, {}, [1], set(), None),
    ({1: 0}, {1: 99}, {}, [], set(), "two"),
    ({1: 80}, {}, {1: 99}, [], {1}, None),
    ({}, {}, {}, [], set(), "two"),
    ({1: STANDING}, {}, {}, [], set(), "two"),
    ({1: 0}, {1: 100 - PIT_CLEAR}, {}, [1], set(), None),
    ({1: 80}, {}, {1: 100 - INVOLVED_FOR}, [], set(), "two"),
    ({1: 80, 9: 0}, {}, {}, [], set(), "two"),
])
def test_nascar_characterization_preserves_clearance_and_audit(speeds, pits, contacts, stopped, involved, verdict):
    rc = RaceControl()
    rc.flag = "yellow"
    rc.caution = Caution({1: 5.2}, 0)
    rc.caution.pits_open = True
    rc.caution_laps = 1
    rc.pace_car = 9
    rc._pit_seen = pits
    rc.watch.involved = contacts
    rc.audit = Mock()
    result = rc.track_clear(speeds, pits, 100, pace_car=9)
    assert (result.stopped, result.involved, result.clear) == (stopped, involved, not stopped and not involved)
    assert rc.caution_lap(9, speeds, 100) == verdict
    assert rc.audit.call_args_list[0].args == ("caution_lap",)
    rc.audit.assert_any_call("caution_clear", laps=2, stopped=stopped, involved=sorted(involved),
                             min_laps=3, max_laps=6, executed=False)
    assert rc.audit.call_count == 2


def test_strict_missing_telemetry_is_dirty_but_nascar_is_clear():
    rc = RaceControl()
    assert rc.track_clear({}, {}, 100, connected={1}).clear
    assert not rc.track_clear({}, {}, 100, connected={1}, fresh=set(), strict=True).clear
    assert not rc.track_clear({1: 80}, {}, 100, connected={1}, fresh=set(), strict=True).clear
    assert rc.track_clear({}, {1: 99}, 100, connected={1}, fresh=set(), strict=True).clear


@pytest.fixture
def virtual():
    clock = [0.0]
    rc = RaceControl({"session_type": 3}, clock=SimpleNamespace(
        time=lambda: 1000 + clock[0], monotonic=lambda: clock[0]))
    rc.neutralization_rule = Neutralization(mode="live", green_after_laps=2,
        green_clear_s=1, green_max_extra_laps=3, green_max_wait_s=60).model_dump()
    rc.set_flag("vsc", 80)
    rc.audit = Mock()
    field = Mock()

    def feed(t, lap=5, crossing=False, speed=80, missing=None, contact=False):
        clock[0] = t
        samples = {1: Sample(lap + .1, 1, 0, t, 0, lap), 2: Sample(lap - .1, 1, 0, t, 0, lap - 1)}
        speeds = {1: 80, 2: speed}
        if missing == "sample":
            samples.pop(2)
            speeds.pop(2)
        if missing == "speed":
            speeds.pop(2)
        if missing == "stale":
            samples[2] = samples[2]._replace(stamp=t - 10)
        field.samples.return_value = samples
        field.snapshot.return_value = {c: s.progress for c, s in samples.items()}
        rc.neutralization_clear_tick(samples, speeds, t, 1000 + t, connected={1, 2})
        if contact:  # arrives after the clear tick, immediately before the green decision
            rc.contact(1000 + t, 2, 1, 40)
        return rc.green_if_due(1, field) if crossing else None

    return rc, feed


@pytest.mark.parametrize("flag", ["vsc", "fcy"])
def test_virtual_two_one_then_green_on_distinct_crossings(virtual, flag):
    rc, feed = virtual
    rc.set_flag(flag, 80)
    feed(0, crossing=True)
    feed(1)
    assert feed(2, lap=6, crossing=True) is None
    assert "DOS" in rc.notices[-1]["text"]
    feed(3, lap=6)
    assert feed(4, lap=7, crossing=True) is None
    assert "UNA" in rc.notices[-1]["text"]
    # Duplicate lap packet cannot advance the countdown.
    assert feed(4.1, lap=7, crossing=True) is None
    feed(5, lap=7)
    assert feed(6, lap=8, crossing=True) == []
    assert rc.flag == "green"
    assert [c.kwargs["verdict"] for c in rc.audit.call_args_list if c.args == ("auto_green_notice",)] == ["two", "one"]


@pytest.mark.parametrize("hazard", ["stopped", "slow", "contact", "sample", "speed", "stale"])
def test_green_rechecks_after_one_to_go(virtual, hazard):
    rc, feed = virtual
    for t in range(5):
        feed(t, lap=5 + t // 2, crossing=t % 2 == 0)
    assert "UNA" in rc.notices[-1]["text"]
    kwargs = {"speed": 0 if hazard == "stopped" else 20} if hazard in ("stopped", "slow") else (
        {"contact": True} if hazard == "contact" else {"missing": hazard})
    assert feed(5, lap=7, **kwargs) is None
    assert feed(6, lap=8, crossing=True, **kwargs) is None
    assert rc.flag == "vsc"
    held = [c.kwargs for c in rc.audit.call_args_list if c.args == ("green_held",)]
    assert held[-1]["reason"] == "track_not_clear"
    if hazard == "contact":
        assert held[-1]["involved"] == [1, 2]


@pytest.mark.parametrize("missing", ["sample", "speed", "stale"])
def test_absent_telemetry_never_announces_countdown(virtual, missing):
    rc, feed = virtual
    for t in range(7):
        feed(t, lap=5 + t // 2, crossing=t % 2 == 0, missing=missing)
    assert rc.flag == "vsc" and not rc.notices


@pytest.mark.parametrize("mode", ["live", "shadow"])
def test_time_limit_without_leader_crossings_hands_off_once(virtual, mode):
    rc, feed = virtual
    rc.neutralization_rule["mode"] = mode
    for t in range(63):
        feed(t, missing="sample")
    assert rc._green_warned and rc.flag == "vsc"
    assert len(rc.notices) == (1 if mode == "live" else 0)
    assert sum(c.args == ("auto_green_extension",) for c in rc.audit.call_args_list) == 1
    for t in range(63, 75):
        feed(t, lap=t, crossing=True)
    assert rc.flag == "vsc"


def test_shadow_records_two_one_green_without_emitting(virtual):
    rc, feed = virtual
    rc.neutralization_rule["mode"] = "shadow"
    for t in range(9):
        feed(t, lap=5 + t // 2, crossing=t % 2 == 0)
    assert rc.flag == "vsc" and not rc.notices
    assert [d["op"] for d in rc.autoflags.log] == ["two", "one", "green"]
    assert all(not c.kwargs["executed"] for c in rc.audit.call_args_list if c.args == ("auto_green_notice",))


@pytest.mark.parametrize("mode,killed", [("off", False), ("live", True)])
def test_disabled_timeout_and_countdown_emit_nothing(virtual, mode, killed):
    rc, feed = virtual
    rc.neutralization_rule["mode"] = mode
    if killed:
        rc.auto_off.add("neutralization")
    for t in range(65):
        feed(t, lap=t, crossing=True)
    assert not rc.notices and not rc.autoflags.log and rc.flag == "vsc"


@pytest.mark.parametrize("recovery", ["resume", "pits", "retired"])
@pytest.mark.parametrize("mode", ["live", "shadow"])
def test_spin_decision_closes_without_inventing_a_local_flag(recovery, mode):
    clock = [0.0]
    rc = RaceControl({"session_type": 3}, clock=SimpleNamespace(
        time=lambda: 1000 + clock[0], monotonic=lambda: clock[0]))
    rc.neutralization_rule = Neutralization(mode=mode, spin={"stopped_s": 1, "clear_s": 2}).model_dump()
    rc.audit = Mock()
    field = Mock()

    def feed(t, speed, present=True):
        clock[0] = t
        field.samples.return_value = {1: Sample(5.1, 1, 0, t, 0, 5)} if present else {}
        return rc.incident_step({1: speed} if present else {}, field, 1000 + t, connected={1} if present else set())

    feed(0, 80)
    feed(1, 0)
    feed(2, 0)
    assert rc._spins and rc.autoflags.log[-1]["kind"] == "spin"
    rc.watch.on_contact(1003, 1, 2, 40)
    for t in range(3, 19):
        if recovery == "pits":
            rc.report_pit(1, True, 1000 + t)
        feed(t, 40 if recovery == "resume" else 0, present=recovery != "retired")
    assert rc._spins  # contact expires at 1018; clear_s has only just started
    feed(19, 40 if recovery == "resume" else 0, present=recovery != "retired")
    feed(20, 40 if recovery == "resume" else 0, present=recovery != "retired")
    assert not rc._spins
    cleared = [c.kwargs for c in rc.audit.call_args_list if c.args == ("spin_cleared",)]
    assert len(cleared) == 1
    assert cleared[0]["reason"] == {"resume": "resumed", "pits": "pits", "retired": "retired"}[recovery]
    assert rc.flag == "green" and not rc.notices
    assert rc.autoflags.log[-1]["decision"] == "spin_cleared"


@pytest.mark.parametrize("field,value", [("green_max_wait_s", 59), ("green_max_wait_s", 1801),
    ("resume_kmh", 4), ("resume_kmh", 101), ("clear_s", 0), ("clear_s", 61)])
def test_new_rule_bounds(field, value):
    kwargs = {field: value} if field == "green_max_wait_s" else {"spin": {field: value}}
    with pytest.raises(ValueError):
        Neutralization(**kwargs)


def test_old_snapshot_receives_new_defaults():
    rule = Neutralization.model_validate({"mode": "live"}).model_dump()
    assert rule["green_max_wait_s"] == 600
    assert rule["spin"]["resume_kmh"] == 30 and rule["spin"]["clear_s"] == 5


def test_adapter_includes_connected_car_with_no_sample_or_velocity(virtual, monkeypatch):
    from app import racecontrol as adapter

    rc, feed = virtual
    field = Mock()
    cars = {1: {"velocity": [0, 0, 80 / 3.6]}, 2: {"velocity": [0, 0, 80 / 3.6]}, 3: {}}
    inst = SimpleNamespace(acsp=SimpleNamespace(field=field, cars=cars))
    monkeypatch.setattr(adapter, "_live", lambda sid: inst)
    for t in range(7):
        feed(t, lap=5 + t // 2)
        field.samples.return_value = {c: Sample(5 + t // 2 + (0.1 if c == 1 else -0.1),
            1, 0, t, 0, 5 + t // 2) for c in (1, 2)}
        monkeypatch.setattr(adapter.time, "time", lambda t=t: 1000 + t)
        monkeypatch.setattr(adapter.time, "monotonic", lambda t=t: t)
        adapter.auto_green(42, rc, 1)
    assert rc.flag == "vsc" and not rc.notices
    assert rc._green_connected == {1, 2, 3}
    assert rc._green_clear_since is None


def test_spin_missing_telemetry_and_resume_threshold_do_not_clear():
    clock = [0.0]
    rc = RaceControl({"session_type": 3}, clock=SimpleNamespace(
        time=lambda: 1000 + clock[0], monotonic=lambda: clock[0]))
    rc.neutralization_rule = Neutralization(mode="live", spin={"stopped_s": 1, "clear_s": 2}).model_dump()
    field = Mock()

    def feed(t, speed, sample=True):
        clock[0] = t
        field.samples.return_value = {1: Sample(5.1, 1, 0, t, 0, 5)} if sample else {}
        rc.incident_step({1: speed} if sample else {}, field, 1000 + t, connected={1})

    feed(0, 80)
    feed(1, 0)
    feed(2, 0)
    for t in range(3, 7):
        feed(t, 80, sample=False)
    assert rc._spins
    for t in range(7, 11):
        feed(t, 30)
    assert rc._spins
    feed(11, 31)
    feed(12, 31)
    assert rc._spins
    feed(13, 31)
    assert not rc._spins


def test_shadow_green_does_not_warn_after_its_simulated_finish(virtual):
    rc, feed = virtual
    rc.neutralization_rule["mode"] = "shadow"
    for t in range(65):
        feed(t, lap=5 + t // 2, crossing=t % 2 == 0)
    assert [d["op"] for d in rc.autoflags.log] == ["two", "one", "green"]
    assert not rc._green_warned
