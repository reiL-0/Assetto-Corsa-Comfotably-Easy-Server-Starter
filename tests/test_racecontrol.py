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


@pytest.fixture
def live():
    """A server row with a fake running instance whose ACSP client records what it would send."""
    sid = api.post(f"{V}/servers", json={"name": "rc"}).json()["id"]
    c = acsp.ACSPClient(sid)
    c.session = {"session_index": 0}
    c.cars[3] = {"driver_name": "Ana", "driver_guid": "76561190000000001"}
    sent = []
    c.send = sent.append
    supervisor._instances[sid] = _Inst(c)
    yield sid, c, sent
    supervisor._instances.pop(sid, None)


def test_the_state_changes_and_every_change_raises_rev():
    rc = RaceControl()
    assert rc.snapshot()["flag"] == "green" and rc.rev == 0
    rc.set_flag("yellow", 60)
    assert (rc.flag, rc.pace_kmh, rc.rev) == ("yellow", 60, 1)
    rc.set_flag("white", 60)   # only a yellow carries a pace
    assert (rc.flag, rc.pace_kmh) == ("white", None)
    p = rc.add_penalty(3, 2)
    assert p == {"id": p["id"], "car_id": 3, "type": "pits", "laps": 2} and rc.rev == 3
    assert rc.add_penalty(3, 1)["id"] > p["id"] and RaceControl().add_penalty(4, 1)["id"] > p["id"], "ids rise across states too (a new session must not reuse one)"
    rc.clear_penalty(p["id"] + 1)
    assert rc.clear_penalty(p["id"]) and not rc.clear_penalty(p["id"]) and rc.rev == 6   # clearing nothing is not a change
    for bad in (lambda: rc.set_flag("red"), lambda: rc.set_flag("yellow", 5), lambda: rc.add_penalty(3, 0), lambda: rc.add_penalty(3, 9)):
        with pytest.raises(ValueError):
            bad()


def test_the_state_is_forgotten_when_the_server_stops_or_the_session_changes():
    s1, s2 = {"n": 1}, {"n": 2}
    rc = state_of(901, True, s1, create=True)
    rc.set_flag("yellow", 60)
    assert state_of(901, True, s1) is rc
    fresh = state_of(901, True, s2)   # a new session: back to green, and rev moves on so the clients notice
    assert fresh is not rc and fresh.flag == "green" and fresh.rev == 1 and not fresh.penalties
    assert state_of(901, False, s2) is None and state_of(901, True, s2) is None   # stopped: nothing left, a later start begins clean


def test_the_hub_token_reads_the_state_and_only_a_steward_changes_it(live):
    sid, _, _ = live
    url = f"{V}/servers/{sid}/race-state"
    assert api.get(url).json() == {"rev": 0, "ts": 0, "running": True, "flag": "green", "pace_kmh": None, "penalties": []}
    for role in ("reporter", "driver", "steward"):
        assert _client(role).get(url).status_code == 200, role
    assert TestClient(app).get(url).status_code in (401, 403)
    assert api.get(f"{V}/servers/99999/race-state").status_code == 404
    body = {"flag": "yellow", "pace_kmh": 60}
    for role in ("reporter", "driver"):
        assert _client(role).post(f"{V}/servers/{sid}/race-control", json=body).status_code == 403, role
    assert _client("steward").post(f"{V}/servers/{sid}/race-control", json=body).status_code == 200


def test_the_director_sets_the_flag_and_the_chat_says_it(live):
    sid, c, sent = live
    steward = _client("steward")
    r = steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "yellow", "pace_kmh": 60})
    assert r.status_code == 200 and r.json()["flag"] == "yellow" and r.json()["pace_kmh"] == 60 and len(sent) == 1
    assert steward.get(f"{V}/servers/{sid}/race-state").json()["flag"] == "yellow"
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "red"}).status_code == 422
    assert steward.post(f"{V}/servers/{sid}/race-control", json={"flag": "yellow", "pace_kmh": 5}).status_code == 422
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
