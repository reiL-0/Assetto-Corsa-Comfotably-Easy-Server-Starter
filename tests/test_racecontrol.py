"""Race control (app/live/racecontrol.py, app/racecontrol.py) and the CSP hub every server now sends (app/csp.py `extra_for`)."""

import secrets
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

import time

from app import csp, supervisor
from app import racecontrol as rcmod
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


def _wait(sent, n, timeout=2.0):
    """The chat line of a flag change goes out late (rcmod.CHAT_DELAY, on a timer thread): wait for it."""
    end = time.time() + timeout
    while len(sent) < n and time.time() < end:
        time.sleep(0.01)
    return len(sent)


@pytest.fixture
def live(monkeypatch):
    """A server row with a fake running instance whose ACSP client records what it would send."""
    sid = api.post(f"{V}/servers", json={"name": "rc"}).json()["id"]
    c = acsp.ACSPClient(sid)
    c.session = {"session_index": 0}
    c.cars[3] = {"driver_name": "Ana", "driver_guid": "76561190000000001"}
    sent = []
    c.send = sent.append
    monkeypatch.setattr(rcmod, "CHAT_DELAY", 0.05)
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


def test_the_director_sets_the_flag_and_the_chat_says_it(live):
    sid, c, sent = live
    steward = _client("steward")
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "yellow", "pace_kmh": 60})   # a pace on a yellow is ignored: it is only a flag
    assert r.status_code == 200 and r.json()["flag"] == "yellow" and r.json()["pace_kmh"] is None and _wait(sent, 1) == 1
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
    assert p.status_code == 201 and p.json()["car_id"] == 3 and len(sent) == 1
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
    assert csp.decode(csp.welcome_with_extra("hola", both)) .strip().endswith("SERVER = 7")
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
    rc.set_flag("vsc", 60, grace_s=5, now=0.0)
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
    assert r.status_code == 200 and r.json()["flag"] == "vsc" and r.json()["pace_kmh"] == 60 and r.json()["grace_left"] in (4, 5) and r.json()["vsc_penalty"] == {"kind": "pits", "laps": 2}
    assert _wait(sent, 1) == 1
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


def test_the_chat_line_of_a_flag_goes_out_after_the_state_changes_and_an_fcy_is_a_pace_flag_too(live, monkeypatch):
    sid, c, sent = live
    monkeypatch.setattr(rcmod, "CHAT_DELAY", 0.3)
    steward = _client("steward")
    body = {"flag": "fcy", "pace_kmh": 60, "grace_s": 5, "tolerance_kmh": 5, "penalty": {"kind": "pits", "laps": 2}}
    r = steward.post(f"{V}/servers/{sid}/race-control", json=body)
    assert r.json()["flag"] == "fcy" and r.json()["pace_kmh"] == 60 and r.json()["vsc_id"] and r.json()["grace_left"] in (4, 5)
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "fcy" and sent == [], "the state is already there, the announcement is not"
    assert _wait(sent, 1) == 1
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "fcy"}).status_code == 422, "an FCY needs its pace like a VSC"
    rc = RaceControl()
    rc.set_flag("fcy", 60, 5, 5, {"kind": "pits", "laps": 1}, now=100.0)
    rc.sample({1: 120.0}, 104.0); rc.sample({1: 120.0}, 104.3); rc.sample({1: 120.0}, 104.6)
    assert rc.due(105.0) and [v["car_id"] for v in rc.evaluate(105.0)] == [1] and rc.penalties, "an FCY is judged like a VSC"
