"""Applied incident thresholds: shadow has no visible effects; live uses existing flags."""
from unittest.mock import Mock

import pytest

from app.live.field import Field
from app.live.racecontrol import RaceControl
from app.rulebook import Neutralization


def scenario(monkeypatch, mode="shadow", category="", count=3, stale=False):
    rc = RaceControl({"session_type": 3})
    rc.neutralization_rule = {**Neutralization(mode=mode, mode_by_category={"GT3": "FCY"}).model_dump(), "category": category}
    field = Field()
    clock = [0.0]
    monkeypatch.setattr("app.live.racecontrol.time.monotonic", lambda: clock[0])

    def feed(t, speed):
        clock[0] = t
        for c in range(count):
            field.update(c, .1, now=t - (20 if stale else 0))
        return rc.incident_step({c: speed for c in range(count)}, field, 1000 + t)

    feed(0, 180)
    return rc, feed


def test_shadow_pileup_records_evidence_without_commands_or_banners(monkeypatch):
    rc, feed = scenario(monkeypatch)
    setter = Mock(wraps=rc.set_flag)
    monkeypatch.setattr(rc, "set_flag", setter)
    for t in range(1, 8):
        assert feed(t, 0) == (None, [])
    setter.assert_not_called()
    assert rc.flag == "green" and not rc.notices and not rc.penalties
    d = rc.autoflags_view()["log"][-1]
    assert d["decision"] == "VSC" and d["kind"] == "pileup"
    assert d["cars"] == [0, 1, 2] and d["speeds"] == {0: 0, 1: 0, 2: 0}
    assert d["window_s"] == 10 and d["state"] == "green" and d["timestamp"] == 1006


@pytest.mark.parametrize("category, expected", [("GT3", "fcy"), ("", "vsc")])
def test_live_uses_category_and_existing_continuous_judge(monkeypatch, category, expected):
    rc, feed = scenario(monkeypatch, "live", category)
    for t in range(1, 8):
        feed(t, 0)
    assert rc.flag == expected and rc.pace_kmh == rc.pace_car_kmh
    assert rc.judging_continuously() and rc.vsc_id and not rc.caution


@pytest.mark.parametrize("mode, stale, killed", [("off", False, False), ("live", True, False), ("live", False, True)])
def test_off_stale_and_director_kill_do_nothing(monkeypatch, mode, stale, killed):
    rc, feed = scenario(monkeypatch, mode, stale=stale)
    if killed:
        rc.auto_off.add("neutralization")
    for t in range(1, 10):
        assert feed(t, 0) == (None, [])
    assert rc.flag == "green" and not rc.autoflags.log and not rc.penalties


def test_severe_is_shadow_even_when_live(monkeypatch):
    rc, feed = scenario(monkeypatch, "live", count=5)
    for t in range(1, 8):
        feed(t, 0)
    assert rc.flag == "green" and not rc.notices
    assert rc.autoflags.log[-1]["decision"] == "SC"
    assert rc.autoflags.log[-1]["mode"] == "shadow"


def test_stale_gap_cannot_complete_sustained_stop(monkeypatch):
    rc, feed = scenario(monkeypatch, "live")
    feed(1, 0)
    feed(7, 0)
    assert rc.flag == "green" and not rc.autoflags.log


def test_applied_cache_uses_snapshot_and_category_not_edited_league():
    from sqlmodel import Session

    from app.db import engine
    from app.league import invalidate_rule_cache, neutralization_rule, neutralization_rule_cached
    from app.models import Championship, Event, Schedule, Server

    with Session(engine) as db:
        league = Championship(name="Neutralization cache", rulebook={"neutralization": {"mode": "live"}})
        db.add(league); db.commit(); db.refresh(league)
        server = Server(name="Neutralization cache", base_port=15555, session={
            "category": "GT3", "rulebook_snapshot": {"neutralization": {"mode": "shadow"}}})
        db.add(server); db.commit(); db.refresh(server)
        event = Event(title="Neutralization cache", league_id=league.id, data={})
        db.add(event); db.commit(); db.refresh(event)
        schedule = Schedule(server_id=server.id, event_id=event.id, state="running", start_at=0)
        db.add(schedule); db.commit()
        sid = server.id
        invalidate_rule_cache(sid)
        assert neutralization_rule_cached(sid) is None
        rule = neutralization_rule(sid)
        assert rule["mode"] == "shadow" and rule["category"] == "GT3"
        assert neutralization_rule_cached(sid) == rule
        schedule.state = "done"
        db.add(schedule); db.commit()
        invalidate_rule_cache(sid)
        assert neutralization_rule(sid) == {"mode": "off"}


def test_isolated_slow_car_is_local_and_uses_applied_ratio_and_duration():
    from app.live.field import Sample
    from app.live.incidents import NeutralizationWatch
    w = NeutralizationWatch()
    rule = Neutralization(mode="shadow", slow_car={"pace_ratio": .7, "sustained_s": 2}).model_dump()

    def step(t, speeds):
        return w.step(t, {c: Sample(.1, 1, 0, t, 0) for c in speeds}, speeds, set(), True, rule)

    step(0, {0: 180, 1: 180})
    assert step(1, {0: 100, 1: 180}) is None
    assert step(2, {0: 100, 1: 180}) is None
    decision = step(3, {0: 100, 1: 180})
    assert decision["kind"] == "slow_car" and decision["cars"] == [0]
    assert step(4, {0: 100, 1: 180}) is None


@pytest.mark.parametrize("mode,killed", [("off", False), ("shadow", False), ("live", False), ("live", True)])
@pytest.mark.parametrize("flag", ["vsc", "fcy"])
@pytest.mark.parametrize("judge", ["pace", "pass"])
def test_neutralization_age_does_not_change_existing_judges(mode, killed, flag, judge):
    from app.live.field import Sample

    def drive(rule):
        rc = RaceControl({"session_type": 3})
        rc.neutralization_rule = rule
        if killed:
            rc.auto_off.add("neutralization")
        rc.set_flag(flag, 80, grace_s=5, now=1000)
        events = []
        for i in range(225):
            t = 10 + i * .2
            positions = {1: .5, 2: .49 if i < 15 else .51}
            samples = {c: Sample(pos, 1, 0, t - .2, 0) for c, pos in positions.items()}
            speeds = {1: 100, 2: 100}
            events += getattr(rc, judge + "_tick")(samples, speeds, t, 1000 + t)
        return events, [{k: v for k, v in p.items() if k != "id"} for p in rc.penalties], list(rc.violations)

    baseline = drive({"mode": "off"})
    assert baseline[0] and baseline[1]
    assert drive(Neutralization(mode=mode, stale_telemetry_s=.1).model_dump()) == baseline


def test_monitor_keeps_legacy_judge_cars_with_small_neutralization_age(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import racecontrol as monitor

    rc = RaceControl({"session_type": 3})
    rc.neutralization_rule = Neutralization(mode="shadow", stale_telemetry_s=.1).model_dump()
    cars = {1: {"speed_kmh": 100}}
    inst = SimpleNamespace(server_id=42, acsp=SimpleNamespace(
        session={}, cars=cars, field=SimpleNamespace(samples=lambda: {})))
    monkeypatch.setattr(monitor.supervisor, "live", lambda: [inst])
    monkeypatch.setattr(monitor, "state_of", lambda *args: rc)
    for name in ("finish_nascar", "caution_tick", "incident_tick", "autoflags_tick", "pace_car_check"):
        monkeypatch.setattr(monitor, name, lambda *args: None)
    for name in ("flag_modes_stale_servers", "give_back_stale_servers", "give_back_tick"):
        monkeypatch.setattr(monitor, name, lambda *args: [])
    monkeypatch.setattr("app.league.neutralization_rule_stale", lambda sid: False)
    monkeypatch.setattr(monitor, "_unsaved_dues", [])
    seen = []
    monkeypatch.setattr(monitor, "tick", lambda now, live: seen.extend(live) or [])

    async def stop(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(monitor.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.monitor_forever())
    assert seen == [(42, cars)]


@pytest.mark.parametrize("later_stop, is_pileup", [(3, True), (5, False)])
def test_pileup_window_counts_recent_involved_cars_even_after_recovery(later_stop, is_pileup):
    from app.live.field import Sample
    from app.live.incidents import NeutralizationWatch
    w = NeutralizationWatch()
    rule = Neutralization(mode="shadow", spin={"stopped_s": 1},
                          pileup={"cars": 3, "window_s": 2}).model_dump()
    decisions = []
    for t in range(later_stop + 2):
        speeds = {0: 0 if t in (1, 2) else 180,
                  1: 0 if t >= later_stop else 180,
                  2: 0 if t >= later_stop else 180}
        d = w.step(t, {c: Sample(.1, 1, 0, t, 0) for c in speeds}, speeds, set(), True, rule)
        if d:
            decisions.append(d)
    assert (decisions[-1]["kind"] == "pileup") is is_pileup
    if is_pileup:
        assert decisions[-1]["cars"] == [0, 1, 2]
        assert decisions[-1]["events"][0]["t"] == 2


@pytest.mark.parametrize("recovery_ticks", [1, 3])
def test_same_pileup_is_not_repeated_within_window(recovery_ticks):
    from app.live.field import Sample
    from app.live.incidents import NeutralizationWatch

    watch = NeutralizationWatch()
    rule = Neutralization(mode="shadow", spin={"stopped_s": 1}).model_dump()
    decisions = []
    for t in range(10):
        speed = 180 if t == 0 or 3 <= t < 3 + recovery_ticks else 0
        speeds = {c: speed for c in range(3)}
        decision = watch.step(t, {c: Sample(.1, 1, 0, t, 0) for c in speeds},
                              speeds, set(), True, rule)
        if decision:
            decisions.append(decision)
    assert len(decisions) == 1
    assert decisions[0]["kind"] == "pileup"
    assert all(event["t"] == 2 for event in decisions[0]["events"].values())


def test_one_recovery_tick_does_not_rearm_car_after_window():
    from app.live.field import Sample
    from app.live.incidents import NeutralizationWatch

    watch = NeutralizationWatch()
    rule = Neutralization(mode="shadow", spin={"stopped_s": 1},
                          pileup={"window_s": 2}).model_dump()
    decisions = []
    for t in range(9):
        speed = 180 if t in (0, 3) else 0
        decision = watch.step(t, {0: Sample(.1, 1, 0, t, 0)}, {0: speed}, set(), True, rule)
        if decision:
            decisions.append(decision)
    assert len(decisions) == 1


@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_severe_threshold_below_pileup_records_sc_without_execution(monkeypatch, mode):
    rc, feed = scenario(monkeypatch, mode=mode, count=2)
    rc.neutralization_rule["severe"]["cars"] = 2
    rc.neutralization_rule["pileup"]["cars"] = 3
    setter = Mock(wraps=rc.set_flag)
    monkeypatch.setattr(rc, "set_flag", setter)
    for t in range(1, 8):
        assert feed(t, 0) == (None, [])
    setter.assert_not_called()
    assert rc.flag == "green" and not rc.notices and not rc.penalties
    assert len(rc.autoflags.log) == 1
    decision = rc.autoflags.log[-1]
    assert decision["decision"] == "SC"
    assert decision["mode"] == "shadow"
    assert decision["cars"] == [0, 1]


def test_recovery_requires_distinct_fresh_ticks_and_can_rearm_after_window():
    from app.live.field import Sample
    from app.live.incidents import NeutralizationWatch, RECOVERY_TICKS

    watch = NeutralizationWatch()
    rule = Neutralization(mode="shadow", spin={"stopped_s": 1},
                          pileup={"window_s": 2}).model_dump()

    def step(t, speed, stamp=None):
        return watch.step(t, {0: Sample(.1, 1, 0, t if stamp is None else stamp, 0)},
                          {0: speed}, set(), True, rule)

    step(0, 180)
    step(1, 0)
    assert step(2, 0)
    step(3, 180)
    step(3.1, 180, stamp=3)
    assert 0 in watch.flagged
    for i in range(1, RECOVERY_TICKS):
        assert step(3 + i, 180) is None
    assert 0 not in watch.flagged
    step(5, 180)
    step(6, 0)
    assert step(7, 0)["cars"] == [0]


def test_additional_car_does_not_repeat_overlapping_pileup_within_window():
    from app.live.field import Sample
    from app.live.incidents import NeutralizationWatch

    watch = NeutralizationWatch()
    rule = Neutralization(mode="shadow", spin={"stopped_s": 1}).model_dump()
    decisions = []
    for t in range(7):
        speeds = {c: 0 if t >= (3 if c == 3 else 1) else 180 for c in range(4)}
        decision = watch.step(t, {c: Sample(.1, 1, 0, t, 0) for c in speeds},
                              speeds, set(), True, rule)
        if decision:
            decisions.append(decision)
    assert len(decisions) == 1 and decisions[0]["kind"] == "pileup"
