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


def test_severe_live_without_pace_car_warns(monkeypatch):
    rc, feed = scenario(monkeypatch, "live", count=5)
    for t in range(1, 8):
        feed(t, 0)
    assert rc.flag == "green" and "SC recomendado" in rc.notices[-1]["text"]
    assert rc.autoflags.log[-1]["decision"] == "SC"
    assert rc.autoflags.log[-1]["mode"] == "live"


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
    assert rc.flag == "green" and not rc.penalties
    assert bool(rc.notices) == (mode == "live")
    assert len(rc.autoflags.log) == 1
    decision = rc.autoflags.log[-1]
    assert decision["decision"] == "SC"
    assert decision["mode"] == mode
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


@pytest.fixture
def green_race(monkeypatch):
    from app.live.field import Sample
    clock = [0.0]
    monkeypatch.setattr("app.live.racecontrol.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.live.racecontrol.time.time", lambda: 1000 + clock[0])
    rc = RaceControl({"session_type": 3})
    rc.neutralization_rule = Neutralization(mode="live", green_after_laps=2,
        green_clear_s=2, green_max_extra_laps=1).model_dump()
    field = Mock()
    rc.set_flag("vsc", 80, now=1000)

    def feed(t, lap=5, speed=80, crossing=False, stale=False, car=1, epoch=1):
        clock[0] = t
        samples = {1: Sample(lap + .01, epoch, 0, t - (10 if stale else 0), 0, lap),
                   2: Sample(lap - .1, 1, 0, t, 0, lap - 1)}
        field.samples.return_value = samples
        field.snapshot.return_value = {c: s.progress for c, s in samples.items()}
        rc.incident_step({1: 80, 2: speed}, field, 1000 + t)
        return rc.green_if_due(car, field) if crossing else None

    return rc, field, feed


@pytest.mark.parametrize("flag", ["vsc", "fcy"])
def test_green_after_full_leader_laps_and_continuous_clear_track(green_race, flag):
    rc, _field, feed = green_race
    rc.set_flag(flag, 80)
    feed(0, crossing=True)  # partial first lap
    feed(1)
    assert feed(2, lap=6, crossing=True) is None
    feed(3, lap=6)
    assert feed(4, lap=7, crossing=True) == []
    assert rc.flag == "green" and not rc.penalties


def test_dirty_track_extends_then_warns_once_without_forcing_and_can_recover(green_race):
    rc, _field, feed = green_race
    for t in range(9):
        feed(t, lap=5 + t // 2, speed=0, crossing=t % 2 == 0)
    assert rc.flag == "vsc"
    assert len(rc.notices) == 1 and "máximo" in rc.notices[0]["text"]
    feed(9, lap=9)
    feed(10, lap=10, crossing=True)  # only one clear second
    feed(11, lap=10)
    assert feed(12, lap=11, crossing=True) == []
    assert rc.flag == "green"


@pytest.mark.parametrize("mode,killed", [("off", False), ("shadow", False), ("live", True)])
def test_green_shadow_off_and_kill_switch_emit_no_green(green_race, mode, killed):
    rc, _field, feed = green_race
    rc.neutralization_rule["mode"] = mode
    if killed:
        rc.auto_off.add("neutralization")
    setter = Mock(wraps=rc.set_flag)
    rc.set_flag = setter
    for t in range(9):
        feed(t, lap=5 + t // 2, crossing=t % 2 == 0)
    setter.assert_not_called()
    assert rc.flag == "vsc" and not rc.notices and not rc.penalties
    decisions = [d for d in rc.autoflags.log if d.get("op") == "green"]
    assert len(decisions) == (1 if mode == "shadow" else 0)
    if decisions:
        assert decisions[0]["lap"] == 7 and "vuelta 7" in decisions[0]["text"]


def test_manual_green_cancels_and_new_neutralization_resets(green_race):
    rc, field, feed = green_race
    feed(0, crossing=True)
    feed(1)
    feed(2, lap=6, crossing=True)
    rc.set_flag("green", field=field)
    assert feed(3, lap=7, crossing=True) is None
    assert rc.flag == "green"
    rc.set_flag("fcy", 80, field=field)
    for t in range(4, 8):
        assert feed(t, lap=7 + (t - 4) // 2, crossing=t % 2 == 0) is None
    assert rc.flag == "fcy"
    assert feed(8, lap=9, crossing=True) == []


def test_new_flag_while_active_restarts_lap_and_clear_counters(green_race):
    rc, field, feed = green_race
    feed(0, crossing=True)
    feed(1)
    feed(2, lap=6, crossing=True)
    rc.set_flag("fcy", 80, field=field)
    feed(3, lap=6, crossing=True)
    feed(4, lap=6)
    assert feed(5, lap=7, crossing=True) is None
    feed(6, lap=7)
    assert feed(7, lap=8, crossing=True) == []


@pytest.mark.parametrize("bad", ["stale", "pit", "prestart", "no_leader", "follower", "epoch"])
def test_invalid_leader_does_not_advance_green_count(green_race, bad):
    rc, field, feed = green_race
    for t in range(7):
        if bad == "pit":
            rc.report_pit(1, True, 1000 + t)
        feed(t, lap=0 if bad == "prestart" else 5 + t,
             stale=bad == "stale", car=2 if bad == "follower" else 1,
             epoch=t if bad == "epoch" else 1, crossing=bad != "no_leader")
        if bad == "no_leader":
            field.samples.return_value = {}
            assert rc.green_if_due(1, field) is None
    assert rc.flag == "vsc"


def test_stale_telemetry_interrupts_clear_interval(green_race):
    rc, _field, feed = green_race
    feed(0, crossing=True)
    feed(1)
    feed(2, lap=6, crossing=True, stale=True)
    feed(3, lap=6)
    assert feed(4, lap=7, crossing=True) is None
    assert rc.flag == "vsc"


@pytest.mark.parametrize("flag", ["vsc", "fcy"])
@pytest.mark.parametrize("order", ["pace_first", "pass_first"])
def test_new_incident_neutralization_judges_share_one_penalty(monkeypatch, flag, order):
    from app.live.field import Sample
    rc, feed = scenario(monkeypatch, "live")
    rc.neutralization_rule["default_mode"] = flag.upper()
    for t in range(1, 8):
        feed(t, 0)
    assert rc.flag == flag
    sample = {1: Sample(5.1, 1, 0, 10, 0)}
    monkeypatch.setattr(rc.pacejudge, "step", lambda *args: [{"car": 1, "generation": 0, "speed_kmh": 150, "over_s": 2}])
    monkeypatch.setattr(rc.passjudge, "step", lambda *args: [("due", 1, 0)])
    judges = (rc.pace_tick, rc.pass_tick) if order == "pace_first" else (rc.pass_tick, rc.pace_tick)
    for judge in judges:
        judge(sample, {1: 150}, 10, rc.grace_end + 1)
    assert len(rc.penalties) == 1
    assert len(rc.violations) == 1


def test_auto_green_adapter_uses_current_telemetry_and_announces_existing_green(green_race, monkeypatch):
    from types import SimpleNamespace

    from app import racecontrol as adapter
    rc, field, feed = green_race
    cars = {1: {"velocity": [0, 0, 80 / 3.6]}, 2: {"velocity": [0, 0, 80 / 3.6]}}
    inst = SimpleNamespace(acsp=SimpleNamespace(field=field, cars=cars))
    monkeypatch.setattr(adapter, "_live", lambda sid: inst)
    say, actions = Mock(), Mock()
    monkeypatch.setattr(adapter, "_say", say)
    monkeypatch.setattr(adapter, "nascar_actions", actions)
    for t in range(5):
        feed(t, lap=5 + t // 2)
        if t % 2 == 0:
            adapter.auto_green(42, rc, 1)
    assert rc.flag == "green"
    say.assert_called_once_with(rc, adapter.SAY["green"], "flag")
    actions.assert_called_once_with(42, rc, [])


def test_lap_packet_dispatches_existing_auto_green_for_virtual_neutralization(monkeypatch):
    from app import racecontrol as adapter
    from app.live import acsp
    from app.live import racecontrol as live
    client = acsp.ACSPClient(4242)
    client.session = {"session_type": 3}
    rc = RaceControl(client.session)
    rc.set_flag("fcy", 80)
    monkeypatch.setattr(live, "state_of", lambda *args, **kw: rc)
    monkeypatch.setattr(live, "on_lap", lambda *args: (rc, []))
    monkeypatch.setattr(live, "on_cuts", lambda *args: None)
    monkeypatch.setattr(adapter, "plain_winner", lambda *args: None)
    def check_counter(*args):
        assert client.field.completed_laps[1] == 5
    auto_green = Mock(side_effect=check_counter)
    monkeypatch.setattr(adapter, "auto_green", auto_green)
    client._apply({"type": "lap_completed", "car_id": 1, "laptime_ms": 90000,
                   "cuts": 0, "leaderboard": [{"car_id": 1, "laps": 5}]})
    auto_green.assert_called_once_with(4242, rc, 1)


def test_virtual_green_still_applies_when_nascar_is_armed(green_race):
    from app.live.nascar import Nascar
    rc, _field, feed = green_race
    rc.nascar = Nascar(100)
    for t in range(5):
        feed(t, lap=5 + t // 2, crossing=t % 2 == 0)
    assert rc.flag == "green"


def test_physical_sc_exit_is_not_taken_over_by_virtual_green(green_race):
    rc, _field, feed = green_race
    rc.pace_out = True
    for t in range(9):
        feed(t, lap=5 + t // 2, crossing=t % 2 == 0)
    assert rc.flag == "vsc" and not rc.notices


@pytest.mark.parametrize("progress, counters, expected", [
    ([3.1, 4.999, 5.001], [4, 5, 5], 1),
    ([4.1, 4.2, 4.3], [4, 5, 6], 2),
    ([4.1, 5.1, 6.1], [None, None, None], -1),
    ([4.1, 5.1, 6.1, 7.1], [4, 5, 4, 5], 1),
])
def test_green_counts_only_strictly_increasing_completed_laps(green_race, progress, counters, expected):
    from types import SimpleNamespace
    rc, field, feed = green_race
    rc.neutralization_rule["green_clear_s"] = 100
    for t, (p, completed) in enumerate(zip(progress, counters)):
        feed(t)
        field.samples.return_value = {1: SimpleNamespace(progress=p, epoch=1,
            generation=0, stamp=t, completed_laps=completed)}
        rc.green_if_due(1, field)
    assert rc._green_laps == expected


@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_severe_reuses_real_pace_car_route(monkeypatch, mode):
    rc, feed = scenario(monkeypatch, mode, count=5)
    # The pace car is connected and has recent session telemetry.
    rc.set_pace_car(4)
    rc.neutralization_rule["severe"]["cars"] = 4
    setter = Mock(wraps=rc.set_flag)
    monkeypatch.setattr(rc, "set_flag", setter)
    for t in range(1, 8):
        feed(t, 0)
    if mode == "shadow":
        setter.assert_not_called()
        assert not rc.notices and rc.pace_car_state()["mode"] == "wait"
    else:
        setter.assert_called_once()
        assert setter.call_args.args == ("yellow",)
        assert setter.call_args.kwargs["source"] == "incident"
        assert rc.caution.pace_only and rc.pace_car_state()["mode"] == "lead"
        for t in range(8, 20):
            feed(t, 0)
        assert setter.call_count == 1


@pytest.mark.parametrize("flag", ["vsc", "fcy"])
def test_severe_escalates_only_automatic_virtual_flags(monkeypatch, flag):
    for source in ("director", "incident"):
        rc, feed = scenario(monkeypatch, "live", count=5)
        rc.set_pace_car(4)
        rc.neutralization_rule["severe"]["cars"] = 4
        rc.set_flag(flag, 80, source=source)
        for t in range(1, 8):
            feed(t, 0)
        assert rc.flag == ("yellow" if source == "incident" else flag)


@pytest.mark.parametrize("blocked", ["kill", "formation", "practice", "stale_pace"])
def test_severe_deployment_guards(monkeypatch, blocked):
    rc, feed = scenario(monkeypatch, "live", count=5)
    rc.set_pace_car(9 if blocked == "stale_pace" else 4)
    rc.neutralization_rule["severe"]["cars"] = 4
    if blocked == "kill":
        rc.auto_off.add("neutralization")
    elif blocked == "formation":
        rc.formation = True
    elif blocked == "practice":
        rc.session_ref["session_type"] = 1
    setter = Mock(wraps=rc.set_flag)
    rc.set_flag = setter
    for t in range(1, 8):
        feed(t, 0)
    setter.assert_not_called()
    assert rc.flag == "green"


def test_manual_green_cancels_sc_and_cooldown_prevents_chain(monkeypatch):
    rc, feed = scenario(monkeypatch, "live", count=5)
    rc.set_pace_car(4)
    rc.neutralization_rule["severe"]["cars"] = 4
    for t in range(1, 8):
        feed(t, 0)
    assert rc.flag == "yellow"
    rc.set_flag("green")
    assert rc.caution is None and rc.caution_lap(4, {}, 1008) is None
    for t in range(8, 23):
        feed(t, 180 if t < 11 else 0)
    assert rc.flag == "green"



def test_additional_cars_escalate_same_incident_to_severe(monkeypatch):
    from app.live.field import Sample
    rc = RaceControl({"session_type": 3})
    rc.neutralization_rule = Neutralization(mode="live", spin={"stopped_s": 1}).model_dump()
    rc.set_pace_car(9)
    field = Mock()
    for t in range(6):
        monkeypatch.setattr("app.live.racecontrol.time.monotonic", lambda: t)
        speeds = {c: 0 if t >= (3 if c >= 3 else 1) else 180 for c in range(5)}
        field.samples.return_value = {c: Sample(.1, 1, 0, t, 0) for c in [0, 1, 2, 3, 4, 9]}
        field.snapshot.return_value = {c: .1 for c in speeds}
        rc.incident_step(speeds, field, 1000 + t)
        if t == 2:
            assert rc.flag == "vsc"
    assert rc.flag == "yellow" and rc.pace_car_state()["mode"] == "lead"


def test_kill_switch_stops_automatic_sc_restart(monkeypatch):
    rc, feed = scenario(monkeypatch, "live", count=5)
    rc.set_pace_car(4)
    rc.neutralization_rule["severe"]["cars"] = 4
    for t in range(1, 8):
        feed(t, 0)
    rc.auto_off.add("neutralization")
    for _ in range(10):
        assert rc.caution_lap(4, {}, 1100) is None
    assert rc.flag == "yellow" and not rc.pace_car_in
    rc.set_flag("green")
    assert rc.flag == "green" and rc.caution is None
    assert rc.pace_car_state()["mode"] == "in" and not rc.pace_car_in


@pytest.mark.parametrize("nascar", [False, True])
@pytest.mark.parametrize("via", ["yellow", "vsc", "fcy"])
def test_manual_green_returns_pace_car_without_restart(nascar, via):
    rc = RaceControl({"session_type": 3})
    if nascar:
        rc.configure_nascar(70)
    rc.set_pace_car(4)
    field = Mock()
    field.snapshot.return_value = {0: .5, 1: .4, 4: .6}
    rc.set_flag("yellow", field=field)
    if via != "yellow":
        rc.set_flag(via, 80, field=field)
    laps = rc.caution_laps
    rc.set_flag("green", field=field)
    assert rc.pace_car_state() == {"car": 4, "kmh": 120, "mode": "in"}
    assert not rc.pace_car_in and not rc.pace_car_pitted and not rc.pace_out
    assert rc.caution is None and rc.caution_laps == laps
    rc.set_flag("green")  # repeated green must not abandon the returning car
    rc.report_pit(0, True)
    rc.report_pit(4, False)
    assert rc.pace_car_state()["mode"] == "in"
    rc.report_pit(4, True)
    assert rc.pace_car_state()["mode"] == "in"
    assert not rc.pace_car_in and not rc.pace_car_pitted


@pytest.mark.parametrize("reset", ["yellow", "set_pace_car"])
def test_manual_green_return_is_cancelled_by_new_deployment(reset):
    rc = RaceControl()
    rc.set_pace_car(4)
    rc.set_flag("yellow")
    rc.set_flag("green")
    assert rc.pace_car_state()["mode"] == "in"
    if reset == "yellow":
        rc.set_flag("yellow")
        assert rc.pace_car_state()["mode"] == "lead"
    else:
        rc.set_pace_car(5)
        assert rc.pace_car_state()["mode"] == "wait"


def test_manual_green_keeps_pace_car_returning_after_pit_reports(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.live.racecontrol.time.time", lambda: clock[0])
    rc = RaceControl()
    rc.set_pace_car(4)
    rc.set_flag("yellow")
    assert rc.pace_out
    rc.set_flag("green")
    assert rc.pace_car_state()["mode"] == "in"
    rc.report_pit(4, True)
    assert rc.pace_car_state()["mode"] == "in"
    clock[0] += 120
    assert rc.pace_car_state()["mode"] == "in"
    rc.report_pit(4, True)
    assert rc.pace_car_state()["mode"] == "in"
    assert not rc.pace_car_in and not rc.pace_car_pitted
    rc.set_flag("yellow")
    assert rc.pace_car_state()["mode"] == "lead"


def test_manual_green_without_pace_car():
    rc = RaceControl()
    rc.set_flag("yellow")
    rc.set_flag("green")
    assert rc.pace_car_state() is None


def test_automatic_sc_uses_existing_to_go_and_green(monkeypatch):
    rc, feed = scenario(monkeypatch, "live", count=5)
    rc.set_pace_car(4)
    rc.neutralization_rule["severe"]["cars"] = 4
    for t in range(1, 8):
        feed(t, 0)
    assert rc.caution.pace_only
    assert [rc.caution_lap(4, {}, 1100 + i) for i in range(3)] == [None, None, "two"]
    rc.restart_plan({0: .5, 1: .4, 2: .3, 3: .2, 4: .6}, 2)
    assert rc.caution_lap(4, {}, 1104) == "one"
    leader = rc.caution.restart[0]
    rc.go_armed = True
    assert rc.green_if_due(leader) is None  # waits for the physical pace car
    rc.report_pit(4, True, now=1105)
    assert rc.green_if_due(leader) == []
    assert rc.flag == "green" and rc.caution is None
    assert rc.pace_car_state()["mode"] == "in" and rc.pace_car_in


@pytest.mark.parametrize("flag", ["vsc", "fcy"])
@pytest.mark.parametrize("previous", ["counted", "two", "one"])
def test_escalated_sc_starts_own_caution_and_ignores_virtual_green(monkeypatch, flag, previous):
    rc, feed = scenario(monkeypatch, "live", count=5)
    rc.set_pace_car(4)
    rc.neutralization_rule["severe"]["cars"] = 4
    field = Mock()
    field.snapshot.return_value = {0: .5, 1: .4, 2: .3, 3: .2, 4: .6}
    rc.set_flag(flag, 80, source="incident", field=field)
    old_caution = rc.caution
    for i in range(5):
        rc.caution_lap(4, {}, 1000 + i)
    if previous != "counted":
        rc.restart_plan(field.snapshot(), 2 if previous == "two" else 1)
    # Virtual green counters have also progressed before the escalation.
    rc._green_laps = rc.neutralization_rule["green_after_laps"]
    rc._green_clear_since = rc._green_last_look = 0
    for t in range(1, 8):
        feed(t, 0)
    assert rc.flag == "yellow" and rc._neutralization_sc
    assert rc.caution is not old_caution
    assert rc.caution_laps == -1
    assert not rc.one_more and not rc.pace_car_in and not rc.caution_notified
    assert rc.pace_car_state()["mode"] == "lead"
    assert rc.vsc_id == 0 and rc._green_laps == -1
    for t in range(8, 20):
        feed(t, 80)
        assert rc.green_if_due(0, field) is None
    assert rc.flag == "yellow"
    if previous == "counted":
        # A blocked track reaches the warning only after six of the SC's own full laps.
        assert [rc.caution_lap(4, {0: 0}, 1100 + i) for i in range(7)] == [None] * 6 + ["warn"]
        assert rc.caution_laps == 6 and not rc.pace_car_in
        return
    # Partial crossing, then two full laps: only now may 'two to go' be called.
    assert [rc.caution_lap(4, {}, 1100 + i) for i in range(3)] == [None, None, "two"]
    rc.restart_plan(field.snapshot(), 2)
    assert rc.caution_lap(4, {}, 1103) == "one"
