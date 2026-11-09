"""DRS rules (app/live/drs.py): start, restart, rain with hysteresis, one sanction per window, the grace after DRS goes off."""
from app.live.drs import GRACE_S, Drs
from app.rulebook import Detectors, DrsRule
import pytest


def rule(**kw):
    return DrsRule(**kw).model_dump()


def test_start_then_on():
    d = Drs(rule(start_laps_off=2))
    assert not d.on and d.why == "start" and d.window == 1
    d.step(1, 0, True, None); d.step(2, 1, True, None)
    assert not d.on and d.why == "start" and d.until == 2
    assert d.step(3, 2, True, None) and d.on and d.why is None


def test_no_start_laps_means_on_from_the_green():
    d = Drs(rule(start_laps_off=0))
    assert d.on and d.window == 0


def test_caution_and_restart():
    d = Drs(rule(start_laps_off=0, restart_laps_off=2))
    d.step(1, 5, True, None)
    assert d.on
    d.step(2, 5, False, None)
    assert not d.on and d.why == "flag" and d.window == 1
    d.step(3, 6, True, None)   # green again with the leader at 6 completed laps: DRS from lap 8
    assert not d.on and d.why == "restart" and d.until == 8
    d.step(4, 7, True, None)
    assert not d.on
    d.step(5, 8, True, None)
    assert d.on
    d.step(6, 9, False, None); d.step(7, 9, True, None)   # a second restart counts from its own green, not the first
    assert d.until == 11 and d.window == 2


def test_rain_has_hysteresis():
    d = Drs(rule(start_laps_off=0, dry_confirm_s=30))
    d.step(0, 3, True, 0.0)
    assert d.on
    d.step(10, 3, True, 0.3)
    assert not d.on and d.why == "rain"
    d.step(20, 3, True, 0.0)           # dry reading: the countdown starts
    d.step(40, 3, True, 0.1)           # between the thresholds: still wet, the countdown starts over
    d.step(50, 3, True, 0.0)
    d.step(79, 3, True, 0.0)
    assert not d.on, "29 s dry is not enough"
    d.step(81, 3, True, 0.0)
    assert d.on, "30 s under dry_below allows DRS again"


def test_rain_off_switch_and_unknown_weather():
    d = Drs(rule(start_laps_off=0, rain_disables=False))
    d.step(0, 1, True, 1.0)
    assert d.on
    d = Drs(rule(start_laps_off=0))
    d.step(0, 1, True, None)
    assert d.on, "no weather reading counts as dry"


def test_one_sanction_per_window_and_grace():
    d = Drs(rule(start_laps_off=0, restart_laps_off=0))
    d.step(0, 1, True, None)
    assert not d.used("ana", 1), "DRS is on: nothing to sanction"
    d.step(10, 1, False, None)
    assert not d.used("ana", 10 + GRACE_S - 0.1), "inside the grace"
    assert d.used("ana", 10 + GRACE_S + 0.1)
    assert not d.used("ana", 20), "once per window and driver"
    assert d.used("bob", 20), "another driver is judged on his own"
    d.step(30, 1, True, None); d.step(31, 1, False, None)
    assert d.used("ana", 40), "a new window, a new offence"


def test_rule_validation():
    with pytest.raises(ValueError):
        DrsRule(wet_on=0.1, dry_below=0.1)
    assert Detectors().drs is None and Detectors(drs={}).drs.unserved_s == 10 and Detectors(drs={}).drs.penalty == "dt"


def test_an_event_keeps_its_drs_rules_and_the_session_snapshot_carries_them():
    from fastapi.testclient import TestClient
    from conftest import ADMIN
    from app.main import app
    api = TestClient(app, headers=ADMIN)
    base = {"title": "drs", "session": {"name": "x", "track": "monza", "cars": ["abarth500"], "max_clients": 5}}
    e = api.post("/api/v1/events", json={**base, "drs_rule": {"start_laps_off": 3, "unserved_s": 20}})
    assert e.status_code == 201 or e.status_code == 200, e.text
    eid = e.json()["id"]
    assert e.json()["drs_rule"]["start_laps_off"] == 3 and e.json()["drs_rule"]["unserved_s"] == 20
    kept = api.put(f"/api/v1/events/{eid}", json=base)   # an older client that does not send it keeps the saved rules
    assert kept.json()["drs_rule"]["start_laps_off"] == 3
    gone = api.put(f"/api/v1/events/{eid}", json={**base, "drs_rule": None})
    assert "drs_rule" not in gone.json() or gone.json()["drs_rule"] is None
    bad = api.put(f"/api/v1/events/{eid}", json={**base, "drs_rule": {"wet_on": 0.1, "dry_below": 0.2}})
    assert bad.status_code == 422
    from app.db import engine
    from app.league import session_for
    from app.models import Event
    from sqlmodel import Session
    api.put(f"/api/v1/events/{eid}", json={**base, "drs_rule": {"start_laps_off": 4}})
    with Session(engine) as s:
        snap = session_for(s, s.get(Event, eid), 0).rulebook_snapshot
    assert snap.detectors.drs.start_laps_off == 4, "a league-less event gets a snapshot with its DRS rules"


def test_the_live_offence_is_queued_once_and_a_pit_pass_serves_it():
    from app.live import racecontrol as rcmod
    from app.live.racecontrol import RaceControl
    rcmod._drs_dues_pending.clear(); rcmod._drs_served.clear()
    rc = RaceControl(session_ref={"session_type": 3}); rc.server_id = 5
    rule = DrsRule(start_laps_off=2, unserved_s=15).model_dump()
    rc.drs_tick(rule, 1000.0, 0, None)
    assert rc.drs_use(3, "G1", 1000.0) == {"why": "start", "laps": 3} and len(rc.penalties) == 1
    [row] = rcmod.pending_drs_dues()
    assert (row["driver_guid"], row["period"], row["seconds"]) == ("G1", 1, 15)
    assert rc.drs_use(3, "G1", 1001.0) is None and len(rcmod.pending_drs_dues()) == 1
    rc.report_pit(3, True, now=rc.clock.time() + 5)   # a pit-lane pass after the offence serves the drive-through
    assert rcmod.pending_drs_served() == {(5, rc.cuts_session_key, "G1", 1)}
    rcmod.acknowledge_drs(rcmod.pending_drs_dues(), rcmod.pending_drs_served())
    assert not rcmod.pending_drs_dues() and not rcmod.pending_drs_served()
    live = RaceControl(session_ref={"session_type": 3}); live.server_id = 6
    from app.league import _ManualDrsRule
    live.drs_tick(_ManualDrsRule(rule), 1000.0, 0, None); live.drs_use(4, "G2", 1000.0)
    assert len(live.penalties) == 1 and not rcmod.pending_drs_dues(), "a manual session: the drive-through, nothing on the result"
