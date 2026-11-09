"""Live cut rounds become revocable, idempotent race time penalties."""
# ruff: noqa: F811 -- pytest imports the shared live fixture below.
import json
import re
import sqlite3
import time

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select
from test_cuts_counter import _rule_of
from test_racecontrol import live  # noqa: F401

from app import racecontrol as adapter
from app.db import _m013_cut_dues, engine
from app.live import racecontrol as live_rc
from app.models import Championship, CutDue, Notice, Penalty
from app.results import apply_penalties
from app.rulebook import CutRule, Detectors, Rulebook
from app.stewards.auto import cut_notices, cut_penalties, cut_seconds, stage_cut_dues

GUID = "76561190000000001"
RULE = {"max": 3, "per": 1, "step": 5, "cap": 600, "ladder": [5, 5, 10]}


def rule(**kw):
    return CutRule(tolerance=3, cuts_per_step=1, step_seconds=5, max_seconds=600, **kw)


@pytest.fixture(autouse=True)
def isolated_pending(monkeypatch):
    monkeypatch.setattr(live_rc, "_cut_dues_pending", {})


def dues(sid):
    with Session(engine) as s:
        return s.exec(select(CutDue).where(CutDue.server_id == sid).order_by(CutDue.round)).all()


def test_rounds_repeat_the_last_step_and_keep_old_state_keys(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    for total, seconds in ((3, []), (4, [5]), (8, [5, 5]), (12, [5, 5, 10]), (16, [5, 5, 10, 10])):
        rc = live_rc.state_of(sid, True, client.session, create=True)
        before = rc.cuts_client.get(3, 0)
        for _ in range(total - before):
            live_rc.on_cuts(sid, client.session, 3, 1, True)
        adapter.save_cut_dues()
        rows = dues(sid)
        assert [d.seconds for d in rows] == seconds
        assert [d.round for d in rows] == list(range(1, len(seconds) + 1))
        view = rc.cuts_view(RULE)
        assert view["cuts"] == {"3": total} and view["cuts_rule"] == {k: RULE[k] for k in ("cap", "max", "per", "step")}
        assert view["cuts_round"] == {"3": total % 4}
        assert view["cuts_rounds"] == {"3": total // 4}
        assert view["cuts_ladder"] == RULE["ladder"]
    # The two sources overlap: neither awards the same round again.
    live_rc.on_cuts(sid, client.session, 3, 16, False)
    adapter.save_cut_dues()
    assert len(dues(sid)) == 4
    assert rc.cuts_view(RULE)["cuts"] == {"3": 16}


def test_nine_hub_cuts_create_two_rounds_and_persistence_retries_are_safe(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    live_rc.on_cuts(sid, client.session, 3, 9, True)
    pending = live_rc.pending_cut_dues()
    assert [r["round"] for r in pending] == [1, 2]
    with Session(engine) as s:
        stage_cut_dues(s, pending)
        stage_cut_dues(s, pending)
        s.commit()
    adapter.save_cut_dues()
    adapter.save_cut_dues()
    assert [d.seconds for d in dues(sid)] == [5, 5]
    rc = live_rc.state_of(sid, True, client.session)
    assert rc.cuts_view(RULE)["cuts_round"] == {"3": 1}
    # A new session with the same AC session index is a different race.
    client.session = dict(client.session)
    live_rc.on_cuts(sid, client.session, 3, 4, True)
    adapter.save_cut_dues()
    assert len(dues(sid)) == 3
    assert len({d.session_key for d in dues(sid)}) == 2


def parsed():
    return {"type": "Race", "classification": [{"driver_guid": GUID, "driver_name": "Ana", "position": 1, "total_time_ms": 90000}],
            "laps": [{"driver_guid": GUID, "lap_time_ms": 90000, "cuts": 16}]}


def test_result_counting_caps_each_round_no_notice_and_dismissal_is_permanent(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    start = time.time() - 1
    live_rc.on_cuts(sid, client.session, 3, 16, True)
    rb = Rulebook(detectors=Detectors(cuts=rule(ladder=[5, 5, 10])))
    rb.neutralization.max_auto_time_penalty_s = 7
    with Session(engine) as s:
        c = Championship(name="ladder", rulebook=rb.model_dump())
        s.add(c)
        s.commit()
        # Result counting also stages the last lap while the monitor has not run.
        assert cut_penalties(s, c, sid, "ladder.json", parsed(), time.time() + 1, start) == 4
        assert cut_notices(s, c, sid, "ladder.json", parsed()) == 0
        s.commit()
        assert cut_penalties(s, c, sid, "ladder.json", parsed(), time.time() + 1, start) == 0
        penalties = s.exec(select(Penalty).where(Penalty.server_id == sid).order_by(Penalty.id)).all()
        assert [p.value for p in penalties] == [5000, 5000, 7000, 7000]
        assert all(p.created_by == "auto" and p.auto_key.startswith("cuts:") for p in penalties)
        assert apply_penalties(parsed(), penalties)["classification"][0]["time_penalty_ms"] == 24000
        penalties[0].dismissed = True
        s.add(penalties[0])
        s.commit()
        assert apply_penalties(parsed(), penalties)["classification"][0]["time_penalty_ms"] == 19000
        assert cut_penalties(s, c, sid, "ladder.json", parsed(), time.time() + 1, start) == 0
        s.commit()
        assert len(s.exec(select(Penalty).where(Penalty.server_id == sid)).all()) == 4
    adapter.save_cut_dues()  # queued rows cannot reset applied rows to due
    assert all(d.state == "applied" for d in dues(sid))


def test_legacy_rules_keep_their_calculation_and_notice(live):
    sid, _, _ = live
    legacy = rule()
    assert legacy.ladder is None
    assert cut_seconds(3, legacy) == 0
    assert cut_seconds(4, legacy) == 5
    assert cut_seconds(16, legacy) == 65
    with Session(engine) as s:
        c = Championship(name="legacy-cuts", rulebook=Rulebook(detectors=Detectors(cuts=legacy)).model_dump())
        s.add(c)
        s.commit()
        assert cut_notices(s, c, sid, "legacy.json", parsed()) == 1
        assert cut_notices(s, c, sid, "legacy.json", parsed()) == 0
        s.commit()
        assert s.exec(select(Notice).where(Notice.server_id == sid)).one().value == 10000


@pytest.mark.parametrize("ladder", [[], [-1], [601], [5] * 11])
def test_ladder_validation(ladder):
    with pytest.raises(ValidationError):
        rule(ladder=ladder)


def test_cut_migration_on_existing_database_is_repeatable_and_enforces_identity():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE existing_data (value TEXT)")
    con.execute("INSERT INTO existing_data VALUES ('kept')")
    _m013_cut_dues(con)
    _m013_cut_dues(con)
    con.execute("INSERT INTO cut_dues VALUES (1, 1, 'race', 'guid', 1, 5, 100, 'due')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO cut_dues VALUES (2, 1, 'race', 'guid', 1, 5, 101, 'due')")
    assert con.execute("SELECT value FROM existing_data").fetchone() == ("kept",)
    con.close()


def test_counting_the_actual_result_file_twice_and_failed_writes(live, monkeypatch):
    import json
    import os
    from pathlib import Path

    from app import league
    from app.config import settings
    from app.models import Event, Schedule
    from app.stewards import auto

    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    start = time.time() - 1
    live_rc.on_cuts(sid, client.session, 3, 9, True)
    real_stage = auto.stage_cut_dues
    with monkeypatch.context() as patch:
        def fail(sess, rows, *served):
            assert not live_rc._lock._is_owned()
            raise RuntimeError("disk full")
        patch.setattr(auto, "stage_cut_dues", fail)
        with pytest.raises(RuntimeError, match="disk full"):
            adapter.save_cut_dues()
    assert len(live_rc.pending_cut_dues()) == 2 and dues(sid) == []
    def checked_stage(sess, rows, *served):
        assert not live_rc._lock._is_owned()
        real_stage(sess, rows, *served)
    monkeypatch.setattr(auto, "stage_cut_dues", checked_stage)
    folder = Path(settings.data_dir) / "instances" / str(sid) / "results"
    folder.mkdir(parents=True, exist_ok=True)
    result = folder / "cuts.json"
    result.write_text(json.dumps({"Type": "Race", "TrackName": "spa", "Result": [
        {"DriverName": "Ana", "DriverGuid": GUID, "CarModel": "x", "BestLap": 90000, "TotalTime": 90000}], "Laps": []}))
    end = time.time() + 1
    os.utime(result, (end, end))
    with Session(engine) as s:
        c = Championship(name="count-ladder", rulebook=Rulebook(detectors=Detectors(cuts=rule(ladder=[5, 5, 10]))).model_dump())
        s.add(c)
        s.commit()
        ev = Event(title="R", data={"name": "r", "track": "spa", "cars": ["a"]}, league_id=c.id)
        s.add(ev)
        s.commit()
        sc = Schedule(event_id=ev.id, server_id=sid, start_at=start, state="running")
        s.add(sc)
        s.commit()
        assert league.count_results(s, sc, ev, end + 1) == 1
        assert league.count_results(s, sc, ev, end + 1) == 0
        penalties = s.exec(select(Penalty).where(Penalty.server_id == sid)).all()
        assert len(penalties) == 2
        penalty_id = penalties[0].id
        league_id = c.id
    from test_racecontrol import V, api
    base = f"{V}/servers/{sid}/results/cuts.json/penalties"
    assert api.delete(f"{base}/{penalty_id}").status_code == 204
    assert len(api.get(base).json()) == 1
    with Session(engine) as s:
        assert s.get(Penalty, penalty_id).dismissed is True
        c = s.get(Championship, league_id)
        assert cut_penalties(s, c, sid, "cuts.json", parsed(), end + 1, start) == 0
        assert len(s.exec(select(Penalty).where(Penalty.server_id == sid)).all()) == 2
    adapter.save_cut_dues()
    assert len(dues(sid)) == 2 and live_rc.pending_cut_dues() == []


def test_dues_stay_in_their_result_window_and_absent_drivers_expire(live):
    sid, _, _ = live
    with Session(engine) as s:
        c = Championship(name="cuts-window", rulebook=Rulebook(detectors=Detectors(cuts=rule(ladder=[5, 5, 10]))).model_dump())
        s.add(c)
        for guid, ts, round_no in ((GUID, 500., 1), (GUID, 1500., 2), ("absent", 1500., 1), (GUID, 2500., 3)):
            s.add(CutDue(server_id=sid, session_key="window", driver_guid=guid, round=round_no, seconds=5, ts=ts))
        s.commit()
        assert cut_penalties(s, c, sid, "r1.json", parsed(), 2000., 1000.) == 1
        s.commit()
        states = {(d.driver_guid, d.ts): d.state for d in dues(sid)}
        assert states == {(GUID, 500.): "due", (GUID, 1500.): "applied", ("absent", 1500.): "expired", (GUID, 2500.): "due"}
        assert cut_penalties(s, c, sid, "r2.json", parsed(), 3000., 2000.) == 1


def test_rounds_follow_the_driver_when_slots_change(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    live_rc.on_cuts(sid, client.session, 3, 4, True)
    client.cars[4] = client.cars.pop(3)
    client.cars[3] = {"driver_name": "Otro", "driver_guid": "76561190000000099"}
    live_rc.on_cuts(sid, client.session, 3, 1, True)
    live_rc.on_cuts(sid, client.session, 4, 4, True)
    adapter.save_cut_dues()
    assert [(d.driver_guid, d.round) for d in dues(sid)] == [(GUID, 1), (GUID, 2)]
    rc = live_rc.state_of(sid, True, client.session)
    assert rc.cuts_view(RULE)["cuts_rounds"] == {"3": 0, "4": 2}


HUB_CUT_RULE = r'"cuts_rule":\{"cap":(\d+),"max":(\d+),"per":(\d+),"step":(\d+)\}'


def test_ladder_state_preserves_the_fixed_hub_rule_contract(live, monkeypatch):
    from test_racecontrol import V, api

    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    # The response before RaceControl exists must also keep the fixed shape.
    state = api.get(f"{V}/servers/{sid}/race-state").json()
    assert re.search(HUB_CUT_RULE, json.dumps(state, sort_keys=True, separators=(",", ":")))
    live_rc.on_cuts(sid, client.session, 3, 4, True)
    rc = live_rc.state_of(sid, True, client.session)
    state = rc.cuts_view(RULE)
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
    assert re.search(HUB_CUT_RULE, encoded).groups() == ("600", "3", "1", "5")
    assert set(state["cuts_rule"]) == {"cap", "max", "per", "step"}
    assert state["cuts_round"] == {"3": 0} and state["cuts_rounds"] == {"3": 1}
    assert live_rc.cut_rule_peek(sid)["ladder"] == [5, 5, 10]
    assert RULE["ladder"] == [5, 5, 10], "serialization must not mutate the internal rule"


@pytest.mark.parametrize("initial_rule", [None, RULE])
def test_late_guid_monitor_tick_preserves_counts_and_subsequent_cuts(initial_rule):
    rc = live_rc.RaceControl({"session_type": 3})
    rc.add_cuts(3, 5, True, initial_rule)
    rc.add_cuts(3, 9, False, initial_rule)
    before = rc.cuts_view(RULE)["cuts"]
    # Same reconciliation entry as the monitor, with no timestamp for this GUID.
    rc.add_cuts(3, 0, False, RULE, GUID)
    assert rc.cuts_view(RULE)["cuts"] == before == {"3": 9}
    assert rc.cuts_totals[GUID] == (5, 9)
    assert [r["round"] for r in live_rc.pending_cut_dues()] == [1, 2]
    rc.add_cuts(3, 0, False, RULE, GUID)
    assert len(live_rc.pending_cut_dues()) == 2
    rc.add_cuts(3, 1, False, RULE, GUID)
    assert rc.cuts_view(RULE)["cuts"] == {"3": 10}


def test_a_cut_with_newly_known_guid_adds_to_the_unidentified_counts():
    rc = live_rc.RaceControl({"session_type": 3})
    rc.add_cuts(3, 3, True, RULE)
    rc.add_cuts(3, 1, True, RULE, GUID)
    assert rc.cuts_view(RULE)["cuts"] == {"3": 4}
    assert [r["round"] for r in live_rc.pending_cut_dues()] == [1]


def test_ladder_drafts_preview():
    """The calibration preview adds the steps of the completed rounds, each limited by the cap; leftover cuts cost nothing."""
    from app.rulebook import CutRule
    from app.stewards.auto import cut_drafts, ladder_drafts
    rule = CutRule(tolerance=2, cuts_per_step=1, step_seconds=5, max_seconds=600, ladder=[5, 5, 10])
    laps = lambda n: [{"driver_guid": "g", "lap_time_ms": 90000, "cuts": n}]
    parsed = lambda n: {"type": "Race", "laps": laps(n), "classification": [{"driver_guid": "g", "driver_name": "Ana"}]}
    sec = lambda n, cap=600: [d["seconds"] for d in ladder_drafts(parsed(n), rule, {"g"}, cap)]
    assert sec(2) == [] and sec(3) == [5] and sec(5) == [5] and sec(6) == [10] and sec(9) == [20] and sec(12) == [30]
    assert sec(9, cap=8) == [18], "each step is capped like the real penalty"
    assert cut_drafts(parsed(9), rule, {"g"}) == [], "the legacy notices stay off in ladder mode"


DT_RULE = {"max": 3, "per": 1, "step": 5, "cap": 600, "ladder": [0, 5, 5, 10]}


def count(sid, cap=10, fixed=10, ladder=(0, 5, 5, 10)):
    rb = Rulebook(detectors=Detectors(cuts=rule(ladder=list(ladder), dt_fixed_s=fixed)))
    rb.neutralization.max_auto_time_penalty_s = cap
    with Session(engine) as s:
        c = Championship(name=f"dt{cap}{fixed}", rulebook=rb.model_dump())
        s.add(c)
        s.commit()
        cut_penalties(s, c, sid, "dt.json", parsed(), time.time() + 1, time.time() - 60)
        s.commit()
        return [p.value for p in s.exec(select(Penalty).where(Penalty.server_id == sid).order_by(Penalty.id)).all()]


def test_drive_through_first_step_is_issued_live_and_served_by_a_pit_pass(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, DT_RULE)
    monkeypatch.setattr(live_rc, "_cut_served", set())
    live_rc.on_cuts(sid, client.session, 3, 4, True)
    rc = live_rc.state_of(sid, True, client.session)
    assert [(p["car_id"], p["laps"]) for p in rc.penalties] == [(3, live_rc.CUT_DT_LAPS)], "the drive-through is a live penalty"
    rc.report_pit(3, True, now=rc.dt_open and min(t for _, t in rc.dt_open.values()) - 1)   # a pass from before the penalty does not serve it
    assert len(live_rc.pending_cut_served()) == 0
    rc.report_pit(3, True)
    assert len(live_rc.pending_cut_served()) == 1 and not rc.dt_open
    adapter.save_cut_dues()   # served marks are saved with their rows, once
    adapter.save_cut_dues()
    assert [(d.round, d.seconds, d.state) for d in dues(sid)] == [(1, 0, "served")]
    assert live_rc.pending_cut_served() == set()
    assert count(sid) == [], "served: no time penalty"


def test_unserved_drive_through_costs_the_fixed_seconds_capped_then_5_5_10_10(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, DT_RULE)
    monkeypatch.setattr(live_rc, "_cut_served", set())
    live_rc.on_cuts(sid, client.session, 3, 20, True)   # 5 rounds: DT, 5, 5, 10, 10
    assert count(sid, cap=7, fixed=30) == [7000, 5000, 5000, 7000, 7000], "the fixed seconds obey the same cap as any automatic one"


def test_served_mark_outlives_the_session_state_and_is_saved_with_the_row(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, DT_RULE)
    monkeypatch.setattr(live_rc, "_cut_served", set())
    live_rc.on_cuts(sid, client.session, 3, 4, True)
    live_rc.state_of(sid, True, client.session).report_pit(3, True)
    live_rc.state_of(sid, False)   # the session ends before the monitor saved anything
    assert count(sid) == [], "result counting stages the row and the served mark together"
    adapter.save_cut_dues()
    assert [d.state for d in dues(sid)] == ["served"]


def test_drive_through_ladder_loads_and_old_ones_get_the_default_fixed_seconds():
    assert rule(ladder=[0, 5, 5, 10]).dt_fixed_s is None
    assert CutRule.model_validate({"tolerance": 2, "cuts_per_step": 1, "step_seconds": 5, "max_seconds": 600, "ladder": [5, 5, 10]}).dt_fixed_s is None
    from app.stewards.auto import ladder_drafts
    r = rule(ladder=[0, 5, 5, 10])
    p = {"type": "Race", "laps": [{"driver_guid": "g", "lap_time_ms": 9, "cuts": 8}], "classification": [{"driver_guid": "g", "driver_name": "A"}]}
    assert [(d["seconds"], d["drive_throughs"]) for d in ladder_drafts(p, r, {"g"}, 600)] == [(5, 1)]


def test_a_pass_after_the_row_was_saved_still_serves_it_before_the_result_is_counted(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, DT_RULE)
    monkeypatch.setattr(live_rc, "_cut_served", set())
    live_rc.on_cuts(sid, client.session, 3, 4, True)
    adapter.save_cut_dues()   # the row is already in the database as due...
    assert [d.state for d in dues(sid)] == ["due"]
    rc = live_rc.state_of(sid, True, client.session)
    assert rc.cuts_view(DT_RULE)["cuts_rule"] == {k: DT_RULE[k] for k in ("cap", "max", "per", "step")}, "the hub contract does not see the 0"
    rc.report_pit(3, True)   # ...and the pass comes later, still unsaved
    assert count(sid) == [], "result counting applies the mark to the saved row before it reads the dues"


@pytest.mark.parametrize("from_client", [True, False])
def test_pace_car_cuts_never_create_counts_or_dues(from_client):
    rc = live_rc.RaceControl({"session_type": 3})
    rc.pace_car = 3
    for ladder in (None, [0, 5]):
        rc.add_cuts(3, 20, from_client, {**RULE, "ladder": ladder}, GUID)
        rc.add_cuts(3, 0, False, {**RULE, "ladder": ladder}, GUID)
    assert not rc.cuts_client and not rc.cuts_server
    assert not rc.cuts_totals and not rc.cuts_owners and not rc.cuts_at
    assert not rc.cuts_emitted and not rc.dt_open and not rc.penalties
    assert not live_rc.pending_cut_dues()


def test_pace_car_cut_ingress_and_late_identity_tick_are_ignored(monkeypatch):
    session = {"session_type": 3}
    rc = live_rc.RaceControl(session)
    rc.pace_car = 3
    monkeypatch.setattr(live_rc, "_states", {9876: rc})
    calls = []
    monkeypatch.setattr(rc, "add_cuts", lambda *args: calls.append(args))
    for from_client in (True, False):
        live_rc.on_cuts(9876, session, 3, 10, from_client)
    assert calls == []
    rc = live_rc.RaceControl(session)
    rc.pace_car = 3
    # The monitor may recover an old count once a GUID or ladder becomes known.
    rc.cuts_client[3] = 20
    rc.add_cuts(3, 0, False, {**RULE, "ladder": [0, 5]}, GUID)
    assert not rc.cuts_totals and not rc.cuts_emitted and not rc.dt_open


@pytest.mark.parametrize("pit,server,hub,expected", [(True, 4, 0, 0), (False, 4, 0, 0), (True, 4, 1, 1)])
def test_pit_lap_server_cuts(live, monkeypatch, pit, server, hub, expected):
    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.report_pit(3, pit)
    # The monitor's identity recovery is not a completed lap.
    rc.add_cuts(3, 0, False, RULE, GUID)
    if hub:
        live_rc.on_cuts(sid, client.session, 3, hub, True)
    live_rc.on_cuts(sid, client.session, 3, server, False)
    assert max(rc.cuts_client.get(3, 0), rc.cuts_server.get(3, 0)) == expected
    assert rc.cuts_server.get(3, 0) == 0
    live_rc.on_cuts(sid, client.session, 3, 2, False)
    assert rc.cuts_server.get(3, 0) == 0


def test_zero_cut_lap_clears_pit_and_pace_car_stays_excluded(live, monkeypatch):
    sid, client, _ = live
    _rule_of(monkeypatch, RULE)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.report_pit(3, True)
    client._apply({"type": "lap_completed", "car_id": 3, "laptime_ms": 1000, "cuts": 0,
                   "leaderboard": [{"car_id": 3, "laps": 1, "laptime_ms": 1000}]})
    live_rc.on_cuts(sid, client.session, 3, 2, False)
    assert rc.cuts_server.get(3, 0) == 0
    rc.pace_car = 3
    rc.report_pit(3, True)
    live_rc.on_cuts(sid, client.session, 3, 5, False)
    live_rc.on_cuts(sid, client.session, 3, 5, True)
    assert rc.cuts_server.get(3, 0) == 0 and rc.cuts_client.get(3, 0) == 0


@pytest.mark.parametrize('session_type', [1, 2])
@pytest.mark.parametrize('manual', [False, True])
def test_informational_cuts_never_enter_ladder(monkeypatch, session_type, manual):
    from app import league
    from types import SimpleNamespace
    sid, client = 906, SimpleNamespace(session={'session_type': session_type})
    monkeypatch.setattr(live_rc, '_states', {})
    monkeypatch.setattr('app.supervisor.get', lambda sid: None)
    informational_rule = league._ManualCutRule(league.DEFAULT_CUT_RULE) if manual else {**RULE, 'ladder': [0, 5]}
    _rule_of(monkeypatch, informational_rule)
    rc = live_rc.state_of(sid, True, client.session, create=True)
    rc.pace_car = 4
    rev = rc.rev
    for _ in range(10):
        live_rc.on_cuts(sid, client.session, 3, 1, True)
        live_rc.on_cuts(sid, client.session, 4, 1, True)
    live_rc.on_cuts(sid, client.session, 3, 20, False)
    view = rc.cuts_view(informational_rule)
    assert view == {'cuts': {'3': 10}, 'cuts_rule': {k: informational_rule[k] for k in ('cap', 'max', 'per', 'step')}, 'cuts_info': True}
    assert rc.rev > rev
    assert not rc.cuts_emitted and not rc.dt_open and not rc.penalties
    assert not rc.cuts_totals and not live_rc.pending_cut_dues()
    live_rc.on_excursion(sid, client.session, 3, 1000, 1, 0.5)
    fresh = live_rc.state_of(sid, True, {'session_type': 3}, create=True)
    assert fresh.cuts_view(informational_rule)['cuts'] == {}
    assert 'cuts_info' not in fresh.cuts_view(informational_rule)
    assert 'cuts_info' not in live_rc.RaceControl(object()).cuts_view(informational_rule)


@pytest.mark.parametrize('manual', [False, True])
def test_race_ladder_still_executes_dt_and_dues(monkeypatch, manual):
    from app import league
    rule = league._ManualCutRule(league.DEFAULT_CUT_RULE) if manual else {**RULE, 'ladder': [0, 5]}
    rc = live_rc.RaceControl({'session_type': 3})
    rc.server_id = 907
    for _ in range(10):
        rc.add_cuts(3, 1, True, rule, GUID)
    rounds = 10 // (rule['max'] + 1)
    assert rc.cuts_emitted == {GUID: rounds}
    assert len(rc.dt_open) == len(rc.penalties) == 1
    assert len(live_rc.pending_cut_dues()) == (0 if manual else rounds)
    assert 'cuts_info' not in rc.cuts_view(rule)


@pytest.mark.parametrize('session', [None, object(), {'session_type': 0}, {'session_type': 99}])
def test_other_sessions_ignore_cuts(monkeypatch, session):
    monkeypatch.setattr(live_rc, '_states', {})
    live_rc.on_cuts(908, session, 3, 1, True)
    assert not live_rc._states
    rc = live_rc.RaceControl(session)
    rc.add_cuts(3, 10, True, RULE, GUID)
    assert not rc.cuts_client and not live_rc.pending_cut_dues()
    assert rc.cuts_view(RULE) == {'cuts': {}, 'cuts_rule': None}


@pytest.mark.parametrize("session_type", [1, 2, 3, 0])
@pytest.mark.parametrize("ladder", [None, [], [0, 5, 5, 10]])
def test_ladder_payload_only_in_race(session_type, ladder):
    rc = live_rc.RaceControl({"session_type": session_type})
    current = {k: v for k, v in RULE.items() if k != "ladder"}
    if ladder is not None:
        current["ladder"] = ladder
    view = rc.cuts_view(current)
    assert ("cuts_ladder" in view) == (session_type == 3 and bool(ladder))
    if "cuts_ladder" in view:
        assert view["cuts_ladder"] == ladder
        assert set(view["cuts_rule"]) == {"cap", "max", "per", "step"}
