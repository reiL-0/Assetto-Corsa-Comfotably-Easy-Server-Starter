"""Race control (app/live/racecontrol.py, app/racecontrol.py) and the CSP hub every server now sends (app/csp.py `extra_for`)."""

import secrets
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import csp, supervisor
from app.auth import _sha
from app.config import settings
from app.db import engine
from app.live import acsp
from app.live.racecontrol import RaceControl, state_of
from app.main import app
from app.models import Token, User

V = "/api/v1"
api = TestClient(app, headers=ADMIN)


@pytest.fixture(autouse=True)
def _hub_on(monkeypatch):
    monkeypatch.setattr(settings, "csp_hub", True)   # the default in production; the rest of the suite runs with it off (tests/conftest.py)


class _Inst:
    running = True

    def __init__(self, client):
        self.acsp = client


def _client(role):
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"{role}-{raw[:5]}", role=role)
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def _notices(sid):
    """The on-screen messages the hub would read (they replaced the chat lines)."""
    return [n["text"] for n in api.get(f"{V}/servers/{sid}/race-state").json()["notices"]]


@pytest.fixture
def live(monkeypatch):
    """A server row with a fake running instance whose ACSP client records what it would send."""
    sid = api.post(f"{V}/servers", json={"name": "rc"}).json()["id"]
    c = acsp.ACSPClient(sid)
    c.session = {"session_index": 0, "session_type": 3}   # a race: NASCAR arms a live engine only in a race (the arming outside one is only remembered)
    c.cars[3] = {"driver_name": "Ana", "driver_guid": "76561190000000001"}
    sent = []
    c.send = sent.append
    supervisor._instances[sid] = _Inst(c)
    yield sid, c, sent
    supervisor._instances.pop(sid, None)


def test_the_state_changes_and_every_change_raises_rev():
    rc = RaceControl()
    assert rc.snapshot()["flag"] == "green" and rc.rev == 0
    rc.set_flag("yellow", 60)   # a yellow is only a flag: no pace
    assert (rc.flag, rc.pace_kmh, rc.rev) == ("yellow", None, 1)
    rc.set_flag("vsc", 60)
    assert (rc.flag, rc.pace_kmh, rc.rev) == ("vsc", 60, 2) and rc.vsc_id
    keep = (rc.flag, rc.pace_kmh, rc.vsc_id, rc.rev, rc.evaluated)
    for bad in ({"flag": "vsc"}, {"flag": "vsc", "pace_kmh": 60, "grace_s": 1}):
        with pytest.raises(ValueError):
            rc.set_flag(**bad)
    assert (rc.flag, rc.pace_kmh, rc.vsc_id, rc.rev, rc.evaluated) == keep, "a refused VSC changes nothing"
    rc.set_flag("white")
    assert (rc.flag, rc.pace_kmh, rc.vsc_id) == ("white", None, 0)
    p = rc.add_penalty(3, 2)
    assert p == {"id": p["id"], "car_id": 3, "type": "pits", "laps": 2} and rc.rev == 4
    assert rc.add_penalty(3, 1)["id"] > p["id"] and RaceControl().add_penalty(4, 1)["id"] > p["id"], "ids rise across states too (a new session must not reuse one)"
    rc.clear_penalty(p["id"] + 1)
    assert rc.clear_penalty(p["id"]) and not rc.clear_penalty(p["id"]) and rc.rev == 7   # clearing nothing is not a change
    for bad in (lambda: rc.set_flag("red"), lambda: rc.set_flag("vsc", 5), lambda: rc.set_flag("vsc"), lambda: rc.set_flag("vsc", 60, grace_s=1), lambda: rc.set_flag("vsc", 60, tolerance_kmh=99),
                lambda: rc.set_flag("vsc", 60, penalty={"kind": "pits", "laps": 9}), lambda: rc.set_flag("vsc", 60, penalty={"kind": "time"}), lambda: rc.add_penalty(3, 0), lambda: rc.add_penalty(3, 9)):
        with pytest.raises(ValueError):
            bad()


def test_the_state_is_forgotten_when_the_server_stops_or_the_session_changes():
    s1, s2 = {"n": 1}, {"n": 2}
    rc = state_of(901, True, s1, create=True)
    rc.set_flag("vsc", 60)
    assert state_of(901, True, s1) is rc
    fresh = state_of(901, True, s2)   # a new session: back to green, and rev moves on so the clients notice
    assert fresh is not rc and fresh.flag == "green" and fresh.rev == 1 and not fresh.penalties
    assert state_of(901, False, s2) is None and state_of(901, True, s2) is None   # stopped: nothing left, a later start begins clean


def test_the_hub_token_reads_the_state_and_only_a_steward_changes_it(live):
    sid, _, _ = live
    url = f"{V}/servers/{sid}/race-state"
    first = api.get(url).json()
    assert first["rev"] == 0 and first["flag"] == "green" and first["running"] is True and first["penalties"] == [] and first["violations"] == [] and first["grace_left"] == 0
    for role in ("reporter", "driver", "steward"):
        assert _client(role).get(url).status_code == 200, role
    assert TestClient(app).get(url).status_code in (401, 403)
    assert api.get(f"{V}/servers/99999/race-state").status_code == 404
    body = {"flag": "yellow"}
    for role in ("reporter", "driver"):
        assert _client(role).post(f"{V}/servers/{sid}/race-control", json=body).status_code == 403, role
    assert _client("steward").post(f"{V}/servers/{sid}/race-control", json=body).status_code == 200


def test_the_director_sets_the_flag_and_the_screen_says_it(live):
    sid, c, sent = live
    steward = _client("steward")
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "yellow", "pace_kmh": 60})   # a pace on a yellow is ignored: it is only a flag
    assert r.status_code == 200 and r.json()["flag"] == "yellow" and r.json()["pace_kmh"] is None and _notices(sid) == ["BANDERA AMARILLA: sin adelantar"] and sent == [], "no chat line any more"
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "yellow"
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "red"}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "vsc"}).status_code == 422   # a VSC needs its pace
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "vsc", "pace_kmh": 5}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "green"}).json()["pace_kmh"] is None
    c.session = {"session_index": 1}   # the next session: the state goes back to green on its own
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "green"


def test_a_drive_through_is_listed_once_per_id_and_needs_a_car_on_the_server(live):
    sid, _, sent = live
    steward = _client("steward")
    assert steward.post(f"{V}/servers/{sid}/race-control/penalties", json={"car_id": 9, "laps": 3}).status_code == 409
    p = steward.post(f"{V}/servers/{sid}/race-control/penalties", json={"car_id": 3, "laps": 3})
    assert p.status_code == 201 and p.json()["car_id"] == 3 and sent == [] and "DRIVE-THROUGH para Ana" in _notices(sid)[0]
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["penalties"] == [p.json()]
    assert steward.post(f"{V}/servers/{sid}/race-control/penalties", json={"car_id": 3, "laps": 9}).status_code == 422
    assert steward.delete(f"{V}/servers/{sid}/race-control/penalties/{p.json()['id']}").status_code == 204
    assert steward.delete(f"{V}/servers/{sid}/race-control/penalties/{p.json()['id']}").status_code == 404
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["penalties"] == []


def test_race_control_needs_a_running_server():
    sid = api.post(f"{V}/servers", json={"name": "stopped"}).json()["id"]
    assert _client("steward").post(f"{V}/servers/{sid}/race-control", json={"flag": "yellow"}).status_code == 409
    assert api.get(f"{V}/servers/{sid}/race-state").json()["running"] is False


def test_every_server_sends_the_hub_and_keeps_its_own_options(monkeypatch):
    hub = csp.extra_for(7, "")
    assert hub.startswith("[SCRIPT_100]") and "opr_hub.lua?v=" in hub and "SERVER = 7" in hub
    mine = '[SCRIPT_100]\nSCRIPT = "mine.lua"\n[EXTRA_RULES]\nA = 1'
    both = csp.extra_for(7, mine)
    assert both.startswith(mine) and "[SCRIPT_101]" in both and both.count("opr_hub.lua") == 1   # the admin's script is not replaced
    assert "SERVER = 7" in csp.decode(csp.welcome_with_extra("hola", both))
    monkeypatch.setattr(settings, "csp_hub", False)   # ACM_CSP_HUB=false: the kill switch
    assert csp.extra_for(7, mine) == mine


def test_a_server_with_no_welcome_and_no_extra_still_gets_the_hub_in_cfg_welcome():
    from app.models import Server
    from app.services import server_service
    sid = api.post(f"{V}/servers", json={"name": "hubbed"}).json()["id"]
    with Session(engine) as s:
        d = server_service.write_instance(s.get(Server, sid))
    text = (Path(d) / "cfg" / "welcome.txt").read_text()
    assert "SERVER = %d" % sid in csp.decode(text) and "WELCOME_MESSAGE" in (Path(d) / "cfg" / "server_cfg.ini").read_text()


def _speeds(rc, grace_end, by_car, step=0.25, seconds=3.0):
    """Feed `rc` samples (km/h by car id; a list = one value per sample, repeated if short) for the `seconds` before the grace ends."""
    n = int(seconds / step)
    for i in range(n):
        t = grace_end - seconds + i * step
        rc.sample({c: (v[min(i, len(v) - 1)] if isinstance(v, list) else v) for c, v in by_car.items()}, t)


def test_a_vsc_judges_who_is_still_over_the_pace_when_the_grace_ends():
    rc = RaceControl()
    rc.set_flag("vsc", 60, grace_s=10, tolerance_kmh=5, penalty={"kind": "pits", "laps": 2}, now=1000.0)
    assert rc.snapshot(now=1000.0)["grace_left"] == 10 and rc.snapshot(now=1004.2)["grace_left"] == 6 and rc.snapshot(now=1011)["grace_left"] == 0
    assert not rc.due(1009.9) and rc.due(1010.0)
    rc.report_pit(5, True, now=1008.5)
    _speeds(rc, 1010.0, {
        1: 62,                                   # at the pace: fine
        2: 66,                                   # inside the tolerance (60 + 5 = 65)? 66 is over: judged
        3: [150, 150, 150, 150, 40, 40, 40, 40, 40, 40, 40, 40],   # slowed down in time: the median of the last 1.5 s is 40
        4: [60, 60, 60, 60, 60, 60, 200, 60, 60, 60, 60, 60],       # one wild sample (a lost packet): the median ignores it
        5: 80,                                   # over, but the hub said it is in the pit lane
        6: 3,                                    # standing still (spun, parked): not a violator
        7: [90],                                 # fast
    })
    out = rc.evaluate(1010.0)
    assert sorted((v["car_id"], v["speed_kmh"]) for v in out) == [(2, 66), (7, 90)]
    assert [(p["car_id"], p["laps"]) for p in rc.penalties] == [(2, 2), (7, 2)], "the drive-through of the VSC setting, once per car"
    assert rc.evaluate(1011.0) == [] and len(rc.penalties) == 2, "a VSC is judged only once"
    assert rc.snapshot()["violations"] == out and rc.snapshot()["exempt"] == [{"car_id": 5, "speed_kmh": 80}], "the pit-lane exemption stays visible to the director"
    late = RaceControl()   # the monitor ran late: the exemption is judged against the end of the grace, not against when it happened to run
    late.set_flag("vsc", 60, grace_s=10, penalty={"kind": "pits", "laps": 1}, now=0.0)
    late.report_pit(5, True, now=9.5)
    _speeds(late, 10.0, {5: 80})
    assert late.evaluate(30.0) == [] and late.penalties == [] and late.snapshot()["exempt"][0]["car_id"] == 5


def test_a_vsc_without_penalty_only_records_and_too_few_samples_are_not_judged():
    rc = RaceControl()
    rc.set_flag("vsc", 60, grace_s=5, now=0.0, penalty={"kind": "none"})
    rc.sample({1: 100}, 4.0); rc.sample({1: 100}, 4.5)   # two samples: not enough to judge a car fairly
    _speeds(rc, 5.0, {2: 100})
    out = rc.evaluate(5.0)
    assert [v["car_id"] for v in out] == [2] and rc.penalties == [], "penalty none: recorded, not punished"
    rc2 = RaceControl()
    rc2.report_pit(3, False)   # a "no" is not kept: it can never cancel a "yes"
    rc2.report_pit(3, True, now=100.0); rc2.report_pit(3, False, now=101.0)
    assert rc2._pit_seen == {3: 100.0}


def test_the_monitor_samples_and_judges_for_the_servers_it_is_given():
    from app.live.racecontrol import _states, tick
    rc = state_of(902, True, {"s": 1}, create=True)
    rc.auto_off.add("pace")   # (the continuous judge decides by default; this single judgement is what is left when the director switches it off)
    rc.set_flag("vsc", 60, grace_s=3, penalty={"kind": "pits", "laps": 1}, now=500.0)
    cars = {1: {"velocity": [0, 0, 25.0]}, 2: {"velocity": [0, 0, 10.0]}, 3: {"driver_name": "no velocity yet"}}   # 90 km/h, 36 km/h
    done = []
    for i in range(14):
        done += tick(500.0 + i * 0.25, [(902, cars)])
    assert len(done) == 1 and done[0][0] == 902 and [v["car_id"] for v in done[0][1]] == [1], "judged once, only the fast car"
    assert tick(600.0, [(902, cars)]) == [] and tick(600.0, [(999, cars)]) == []
    _states.pop(902, None)


def test_the_vsc_through_the_api_and_the_pit_report(live):
    sid, c, sent = live
    c.cars[3] = {"driver_name": "Ana", "driver_guid": "76561190000000001", "velocity": [0, 0, 33.0]}   # ~119 km/h
    steward, hub = _client("steward"), _client("reporter")
    body = {"flag": "vsc", "pace_kmh": 60, "grace_s": 5, "tolerance_kmh": 5, "penalty": {"kind": "pits", "laps": 2}}
    r = steward.post(f"{V}/servers/{sid}/race-control", json=body)
    assert r.status_code == 200 and r.json()["flag"] == "vsc" and r.json()["pace_kmh"] == 60 and r.json()["grace_left"] in (4, 5) and r.json()["vsc_penalty"] == {"kind": "pits", "laps": 2, "warnings": 2, "severe_s": 10}
    assert _notices(sid) and sent == []
    pit = f"{V}/servers/{sid}/race-control/pit"
    assert hub.post(pit, json={"car_id": 3, "name": "ana", "pit": False}).status_code == 204
    assert hub.post(pit, json={"car_id": 3, "name": "Beto", "pit": True}).status_code == 409, "the name must be the driver's"
    assert hub.post(pit, json={"car_id": 9, "name": "Ana", "pit": True}).status_code == 409
    assert _client("driver").post(pit, json={"car_id": 3, "name": "Ana", "pit": True}).status_code == 204
    assert TestClient(app).post(pit, json={"car_id": 3, "name": "Ana", "pit": True}).status_code in (401, 403)
    assert steward.post(f"{V}/servers/{sid}/race-control", json={**body, "grace_s": 1}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={**body, "penalty": {"kind": "time"}}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "green"}).json()["vsc_id"] == 0


def test_the_games_own_penalties_are_off_whenever_the_hub_is_on(monkeypatch):
    from app.services import ini_generator
    ports = {"tcp": 9600, "udp": 9600, "http_internal": 9601, "plugin": 9602, "plugin_local": 9603}
    mine = {"SERVER": {"START_RULE": 2, "RACE_GAS_PENALTY_DISABLED": 0, "ALLOWED_TYRES_OUT": 2, "NAME": "x"}}
    on = ini_generator.server_cfg(mine, ports, False, game_penalties_off=True)
    assert "START_RULE=0" in on and "RACE_GAS_PENALTY_DISABLED=1" in on and "ALLOWED_TYRES_OUT=-1" in on and "NAME=x" in on, "the admin's values do not win here"
    off = ini_generator.server_cfg(mine, ports, False)
    assert "START_RULE=2" in off and "ALLOWED_TYRES_OUT=2" in off
    sid = api.post(f"{V}/servers", json={"name": "nopen", "config": mine}).json()["id"]
    text = api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    assert "ALLOWED_TYRES_OUT=-1" in text and "START_RULE=0" in text and "RACE_GAS_PENALTY_DISABLED=1" in text, "the file a server really starts with"
    monkeypatch.setattr(settings, "csp_hub", False)   # the kill switch also gives the game's own penalties back
    assert "ALLOWED_TYRES_OUT=2" in api.get(f"{V}/servers/{sid}/server_cfg.ini").text


def test_an_fcy_is_a_pace_flag_too_and_its_notice_comes_with_the_state(live):
    sid, _, sent = live
    steward = _client("steward")
    body = {"flag": "fcy", "pace_kmh": 60, "grace_s": 5, "tolerance_kmh": 5, "penalty": {"kind": "pits", "laps": 2}}
    r = steward.post(f"{V}/servers/{sid}/race-control", json=body)
    assert r.json()["flag"] == "fcy" and r.json()["pace_kmh"] == 60 and r.json()["vsc_id"] and r.json()["grace_left"] in (4, 5)
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "fcy" and "FULL COURSE YELLOW" in _notices(sid)[0] and sent == []
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "fcy"}).status_code == 422, "an FCY needs its pace like a VSC"
    rc = RaceControl()
    rc.set_flag("fcy", 60, 5, 5, {"kind": "pits", "laps": 1}, now=100.0)
    rc.sample({1: 120.0}, 104.0); rc.sample({1: 120.0}, 104.3); rc.sample({1: 120.0}, 104.6)
    assert rc.due(105.0) and [v["car_id"] for v in rc.evaluate(105.0)] == [1] and rc.penalties, "an FCY is judged like a VSC"


def test_a_flag_shown_by_hand_is_a_notice_for_one_driver_or_for_everybody(live):
    sid, _, _ = live
    steward = _client("steward")
    url = f"{V}/servers/{sid}/race-control/flag-notice"
    assert steward.post(url, json={"kind": "blue"}).status_code == 422, "a blue flag is for somebody"
    assert steward.post(url, json={"kind": "nope", "car_id": 3}).status_code == 422
    assert steward.post(url, json={"kind": "blue", "car_id": 3}).status_code == 200
    assert steward.post(url, json={"kind": "red"}).status_code == 200, "the red is for everybody"
    n = {x["text"]: x for x in steward.get(f"{V}/servers/{sid}/race-state").json()["notices"]}
    assert n["BANDERA AZUL: deja pasar al más rápido"]["to"] == 3 and n["BANDERA ROJA: carrera detenida"]["to"] == -1
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "green" and steward.get(f"{V}/servers/{sid}/race-state").json()["penalties"] == [], "only a notice: no rule changes"


def test_the_winner_stays_on_the_screen_until_the_session_restarts():
    ref = object()
    rc = state_of(77, True, ref, create=True)
    rc.notify("GANADOR: Ana", "winner", hold="winner")
    rc.set_flag("yellow"); rc.set_flag("green")   # a flag leaving a caution drops the held notices... except the winner
    held = [n for n in rc.snapshot()["notices"] if n["text"] == "GANADOR: Ana"]
    assert held and held[0]["kind"] == "hold", "the winner is held through the flags"
    rc.notify("FREE PASS: Beto", "race", hold="freepass"); rc.set_flag("white"); rc.set_flag("green")
    assert not any(n["text"].startswith("FREE PASS") for n in rc.snapshot()["notices"]), "other held notices still go with the caution"
    fresh = state_of(77, True, object(), create=True)   # the session restarted
    assert not fresh.snapshot()["notices"], "a new session starts clean"
    state_of(77, False)


def test_notices_expire_are_capped_and_never_carry_quotes():
    rc = RaceControl()
    a = rc.notify('di "hola" \\ adios', "race", now=100.0)
    assert '"' not in a["text"] and "\\" not in a["text"]
    snap = rc.snapshot(now=103.0)
    assert snap["notices"] == [{"id": a["id"], "kind": "race", "left_s": 7, "text": a["text"], "to": -1}]
    assert rc.snapshot(now=111.0)["notices"] == [], "gone after its ttl"


def test_manual_checkered_flag_writes_finish_marker(live, tmp_path, monkeypatch):
    import json
    sid, c, sent = live
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    rc = state_of(sid, True, c.session, create=True)
    rc.leader_laps_seen = 78
    steward = _client("steward")
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "checkered"})
    assert r.status_code == 200
    markers = list((tmp_path / "instances" / str(sid) / "results").glob("*.finish"))
    assert len(markers) == 1
    m = json.loads(markers[0].read_text())
    assert m["laps"] == 78
    ids = [rc.notify(f"n{i}", now=200.0)["id"] for i in range(100)]
    assert ids == sorted(set(ids)), "ids only rise"
    assert len(rc.snapshot(now=201.0)["notices"]) == 64, "the last 64 are kept (a full field gets one instruction each)"
    rev = rc.rev
    rc.notify("x", "info", 5, now=202.0)
    assert rc.rev == rev + 1 and rc.snapshot(now=202.5)["notices"][-1]["to"] == 5


def test_the_probe_goes_only_to_the_servers_named_in_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "csp_trainer_servers", "")   # (the trainer is on everywhere by default: this one counts the probe's scripts alone)
    assert "opr_rc_probe" not in csp.extra_for(2, "")   # off by default
    monkeypatch.setattr(settings, "csp_probe_servers", "2, 3")
    on = csp.extra_for(2, "")
    assert on.count("opr_hub.lua") == 1 and "[SCRIPT_101]\nSCRIPT = \"https://www.oppenpaddockracing.site/static/csp/opr_rc_probe.lua\"" in on
    assert "opr_rc_probe" not in csp.extra_for(7, "")   # another server: only the hub
    assert csp.extra_for(3, '[SCRIPT_100]\nSCRIPT = "mine.lua"').count("[SCRIPT_") == 3   # the admin's script, the hub (101) and the probe (102) all stay


def test_the_trainer_goes_only_to_the_servers_named_when_the_setting_is_an_explicit_list(monkeypatch):
    monkeypatch.setattr(settings, "csp_trainer_servers", "")
    assert "opr_trainer" not in csp.extra_for(2, "")   # empty = nobody: the emergency stop
    monkeypatch.setattr(settings, "csp_trainer_servers", "2")
    on = csp.extra_for(2, "")
    assert on.count("opr_hub.lua") == 1 and '[SCRIPT_101]\nSCRIPT = "https://www.oppenpaddockracing.site/static/csp/opr_trainer.lua?v=' in on
    assert "opr_trainer" not in csp.extra_for(7, "")   # another server: only the hub
    monkeypatch.setattr(settings, "csp_probe_servers", "2")
    both = csp.extra_for(2, "")
    assert "[SCRIPT_101]\nSCRIPT = \"https://www.oppenpaddockracing.site/static/csp/opr_rc_probe.lua\"" in both and "[SCRIPT_102]" in both and both.index("opr_rc_probe") < both.index("opr_trainer")


def test_the_server_selector_means_every_server_unless_it_is_told_otherwise():
    on = csp.server_listed
    assert on(7, "*") and on(1, " * ") and on(4242, "*")
    assert not on(7, "") and not on(7, "  ")                        # empty = nobody
    assert on(2, "2") and not on(3, "2") and on(3, "2, 3")          # a list of ids
    assert not on(12, "1,2") and not on(1, "12")                    # ids, never substrings
    assert on(1, "*,!2") and not on(2, "*,!2") and on(3, "*, !2")   # every server but one: the brake for a single server
    assert not on(2, "!2") and not on(1, "!2")                      # an exclusion alone turns nothing on
    assert not on(2, "*,!2,2")                                      # the brake wins over a listing


def test_a_selector_that_is_not_one_is_refused_at_start_instead_of_switching_things_off_quietly():
    import pytest
    from app.config import Settings
    for ok in ("*", "", "  ", "2", "2, 3", "*,!3", "*, !3 ,", ",1"):   # what is allowed, with the blanks and the stray comma people leave
        assert Settings(csp_claim_servers=ok, csp_trainer_servers=ok, csp_probe_servers=ok)
    for bad in ("all", "true", "1;2", "*,!*", "*,!", "!", "2-3", "a", "**", "1 2", "*,!x"):
        for field in ("csp_claim_servers", "csp_trainer_servers", "csp_probe_servers"):
            with pytest.raises(ValueError):
                Settings(**{field: bad})


def test_every_server_gets_the_trainer_by_default_and_the_selector_can_stop_one(monkeypatch):
    for sid in (1, 2, 3, 7, 4242):   # the three of today and any server made later
        e = csp.extra_for(sid, "")
        assert e.count("opr_hub.lua") == 1 and "opr_trainer.lua?v=" in e, sid
        assert "opr_rc_probe" not in e, sid                          # the probe stays temporary and opt-in
    monkeypatch.setattr(settings, "csp_trainer_servers", "*,!3")
    assert "opr_trainer" not in csp.extra_for(3, "") and "opr_trainer" in csp.extra_for(4, "") and csp.extra_for(3, "").count("opr_hub.lua") == 1
    monkeypatch.setattr(settings, "csp_hub", False)                  # the hub's own kill switch also stops the trainer: it rides on the hub's section
    assert csp.extra_for(4, "") == ""


def test_rulebook_edits_are_refused_in_a_race_and_allowed_between_races(live):
    from app.models import Championship, Event, Schedule
    sid, client, _ = live
    cid = api.post(f"{V}/championships", json={"name": "Frozen rules"}).json()["id"]
    with Session(engine) as s:
        ev = Event(title="Frozen", league_id=cid)
        s.add(ev)
        s.commit()
        sc = Schedule(server_id=sid, event_id=ev.id, start_at=0, state="running")
        s.add(sc)
        s.commit()
        schedule_id = sc.id
    url = f"{V}/championships/{cid}/rulebook"
    assert api.put(url, json={"neutralization": {"green_after_laps": 4}}).status_code == 409
    assert api.patch(f"{V}/championships/{cid}", json={"name": "Frozen rules", "auto_rules": {"cuts_per_step": 1, "step_seconds": 5, "max_seconds": 5}}).status_code == 409
    with Session(engine) as s:
        assert s.get(Championship, cid).rulebook is None
    client.session = {"session_type": 1}
    assert api.put(url, json={"neutralization": {"green_after_laps": 4}}).status_code == 200
    with Session(engine) as s:
        sc = s.get(Schedule, schedule_id)
        sc.state = "done"
        s.add(sc)
        s.commit()


def test_applied_rulebook_cannot_be_replaced_during_a_race(live, monkeypatch):
    import asyncio
    from app.models import Server
    from app.rulebook import Rulebook
    from app.schemas.servers import SessionIn
    from app.services import server_service
    sid, client, _ = live
    monkeypatch.setattr(server_service, "check_content", lambda *args: {"bmw": [""]})
    with Session(engine) as s:
        server = s.get(Server, sid)
        server.session = {"rulebook_snapshot": Rulebook().model_dump(), "rulebook_league_id": 123}
        s.add(server)
        s.commit()
        body = SessionIn(name="Changed", track="spa", cars=["bmw"], race_laps=5,
                         rulebook_league_id=123,
                         rulebook_snapshot=Rulebook(neutralization={"green_after_laps": 6}))
        with pytest.raises(server_service.ServerError) as caught:
            asyncio.run(server_service.apply(s, server, body))
        assert caught.value.status == 409
        s.refresh(server)
        assert server.session["rulebook_snapshot"]["neutralization"]["green_after_laps"] == 3
        client.session = {"session_type": 1}
        body.restart = False
        from app import league
        monkeypatch.setitem(league._FLAG_MODES, sid, (0, {"blue": "live", "danger": "live"}))
        asyncio.run(server_service.apply(s, server, body))
        assert sid not in league._FLAG_MODES
        assert server.session["rulebook_snapshot"]["neutralization"]["green_after_laps"] == 6
