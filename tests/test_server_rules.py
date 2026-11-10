"""The rules belong to the SERVER: they work with no league, a preset sits under the server's own settings, and a league event only overrides what it says (app/league.py `effective_rules`)."""
import time

from sqlmodel import Session

from app import league
from app.db import engine
from app.models import Championship, Event, Schedule, Server
from app.rulebook import Detectors, DrsRule, Rulebook


def _server(port, **kw):
    with Session(engine) as s:
        sv = Server(name=f"r{port}", base_port=port, **kw)
        s.add(sv); s.commit()
        return sv.id


def test_a_server_with_nothing_set_still_sanctions_and_has_no_drs():
    league._EFFECTIVE.clear()
    sid = _server(21000)
    r = league.effective_rules(sid)
    assert set(r["flag_modes"].values()) == {"live"} and r["drs"] is None and r["cut"] == league.DEFAULT_CUT_RULE


def test_the_servers_own_drs_and_flags_apply_with_no_league_and_a_preset_sits_under_them():
    league._EFFECTIVE.clear()
    sid = _server(21010, rules={"preset": "vrl-base", "detectors": {"drs": DrsRule(start_laps_off=4).model_dump(), "flags": {"yellow": "off"}}})
    r = league.effective_rules(sid)
    assert r["drs"]["start_laps_off"] == 4 and r["drs"].live_only, "the server's DRS, live only"
    assert r["flag_modes"]["yellow"] == "off" and r["flag_modes"]["blue"] == "live", "the rest stay as the defaults say"
    bare = league.effective_rules(_server(21011, rules={"preset": "vrl-base"}))
    assert set(bare["flag_modes"].values()) == {"live"} and set(bare["flag_rules"]) == set(league.FLAG_RULE_KINDS), "a preset alone reads like any other rules"


def test_the_session_own_cuts_and_drs_win_over_the_server_and_its_preset():
    league._EFFECTIVE.clear()
    mine = {"cuts_per_step": 1, "step_seconds": 5, "max_seconds": 600, "tolerance": 4, "ladder": [0, 10]}
    sid = _server(21090, rules={"preset": "vrl-base", "detectors": {"drs": DrsRule(start_laps_off=4).model_dump()}},
                  session={"cuts_rule": mine, "drs_rule": DrsRule(start_laps_off=1).model_dump()})
    r = league.effective_rules(sid)
    assert r["cut"]["ladder"] == [0, 10] and r["cut"]["max"] == 4, "the session's cuts beat the preset's"
    assert r["drs"]["start_laps_off"] == 1, "the session's DRS beats the server's"
    none = league.effective_rules(_server(21091, rules={"preset": "vrl-base"}, session={}))
    assert none["cut"]["ladder"] == [0, 5, 5, 10], "with nothing of its own the session takes the server's"


def test_a_league_event_only_overrides_what_it_says():
    league._EFFECTIVE.clear()
    sid = _server(21020, rules={"detectors": {"drs": DrsRule(start_laps_off=5).model_dump()}})
    with Session(engine) as s:
        c = Championship(name="silent", rulebook=Rulebook(detectors=Detectors(drs=None)).model_dump())
        s.add(c); s.commit()
        ev = Event(title="e", league_id=c.id, data={}); s.add(ev); s.commit()
        s.add(Schedule(server_id=sid, event_id=ev.id, start_at=time.time(), state="running")); s.commit()
        league.invalidate_rule_cache(sid)
        silent = league.drs_rule(sid)
        assert silent["start_laps_off"] == 5 and silent.live_only, "a league with no DRS rule does not switch the server's off"
        c.rulebook = Rulebook(detectors=Detectors(drs=DrsRule(start_laps_off=1))).model_dump()
        s.add(c); s.commit()
        league.invalidate_rule_cache(sid)
        own = league.drs_rule(sid)
        assert own["start_laps_off"] == 1 and not getattr(own, "live_only", False), "the league's own rule wins and counts in its results"


def test_the_rules_endpoint_saves_validates_and_reads_back():
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from app.main import app
    api = TestClient(app, headers=ADMIN)
    sid = api.post("/api/v1/servers", json={"name": "rules"}).json()["id"]
    got = api.get(f"/api/v1/servers/{sid}/rules").json()
    assert got["preset"] is None and got["detectors"] == {} and "vrl-base" in got["presets"] and got["effective"]["drs"] is None
    put = api.put(f"/api/v1/servers/{sid}/rules", json={"preset": "vrl-base", "detectors": {"drs": {"start_laps_off": 3}}})
    assert put.status_code == 200 and put.json()["effective"]["drs"]["start_laps_off"] == 3
    assert league.drs_rule(sid)["start_laps_off"] == 3, "the live race control sees it at once (the cache is dropped)"
    assert api.put(f"/api/v1/servers/{sid}/rules", json={"detectors": {"drs": {"start_laps_off": 99}}}).status_code == 422
    assert api.put(f"/api/v1/servers/{sid}/rules", json={"detectors": {"nonsense": 1}}).status_code == 422
    assert api.put(f"/api/v1/servers/{sid}/rules", json={"preset": "nope"}).status_code == 422
    assert api.put(f"/api/v1/servers/{sid}/rules", json={}).json()["detectors"] == {} and league.drs_rule(sid) is None


def test_a_league_with_default_flags_leaves_the_servers_flags_and_a_presets_give_back_counts():
    league._EFFECTIVE.clear()
    sid = _server(21030, rules={"detectors": {"flags": {"yellow": "off"}}})
    with Session(engine) as s:
        c = Championship(name="plain", rulebook=Rulebook().model_dump())
        s.add(c); s.commit()
        ev = Event(title="e", league_id=c.id, data={}); s.add(ev); s.commit()
        s.add(Schedule(server_id=sid, event_id=ev.id, start_at=time.time(), state="running")); s.commit()
        league.invalidate_rule_cache(sid)
        assert league.flag_modes(sid)["yellow"] == "off", "the league said nothing about flags"
        c.rulebook = Rulebook(detectors=Detectors(flags={"yellow": "live", "blue": "off"})).model_dump()
        s.add(c); s.commit()
        league.invalidate_rule_cache(sid)
        assert league.flag_modes(sid)["blue"] == "off", "what the league sets, wins"
