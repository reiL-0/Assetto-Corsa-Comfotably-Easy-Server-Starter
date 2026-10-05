import secrets
import shutil
from datetime import UTC, datetime

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import content
from app.auth import _sha
from app.db import engine
from app.main import app
from app.models import Token, User

V = "/api/v1"
api = TestClient(app, headers=ADMIN)

SESSION = {
    "name": "Liga", "password": "pw", "admin_password": "boss", "track": "evspa", "cars": ["evbmw", "evaudi"],
    "max_clients": 4, "practice_min": 10, "qualify_min": 5, "race_laps": 8, "reversed_grid": 3, "loop": False,
}


def _install():
    t, c = content._tracks_dir(), content._cars_dir()
    (t / "evspa" / "data").mkdir(parents=True, exist_ok=True)
    (t / "evspa" / "data" / "surfaces.ini").write_text("x")
    for car in ("evbmw", "evaudi"):
        (c / car).mkdir(exist_ok=True)
        (c / car / "data.acd").write_text("x")


def _client_with_role(role: str) -> TestClient:
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"{role}-{raw[:4]}", role=role)
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def _make(title="Spa sprint"):
    _install()
    r = api.post(f"{V}/events", json={"title": title, "notes": "dominical", "session": SESSION})
    assert r.status_code == 201
    return r.json()["id"]


def test_create_list_update_duplicate_delete():
    eid = _make()
    got = api.get(f"{V}/events/{eid}").json()
    assert got["title"] == "Spa sprint" and got["session"]["reversed_grid"] == 3 and got["session"]["loop"] is False
    assert any(e["id"] == eid for e in api.get(f"{V}/events").json())
    upd = api.put(f"{V}/events/{eid}", json={"title": "Spa largo", "notes": "", "session": {**SESSION, "race_laps": 20}}).json()
    assert upd["title"] == "Spa largo" and upd["session"]["race_laps"] == 20
    dup = api.post(f"{V}/events/{eid}/duplicate")
    assert dup.status_code == 201 and dup.json()["title"] == "Spa largo (copia)" and dup.json()["id"] != eid
    assert api.delete(f"{V}/events/{dup.json()['id']}").status_code == 204
    assert api.get(f"{V}/events/{dup.json()['id']}").status_code == 404
    assert api.get(f"{V}/events/{eid}").status_code == 200  # the original is untouched


def test_event_validation_and_permissions():
    _install()
    assert api.post(f"{V}/events", json={"title": "", "session": SESSION}).status_code == 422
    assert api.post(f"{V}/events", json={"title": "x", "session": {**SESSION, "cars": []}}).status_code == 422
    assert api.get(f"{V}/events/9999").status_code == 404
    steward = _client_with_role("steward")
    assert steward.get(f"{V}/events").status_code == 200  # may look...
    assert steward.post(f"{V}/events", json={"title": "x", "session": SESSION}).status_code == 403  # ...not change
    assert steward.post(f"{V}/events/1/run", json={"server_id": 1}).status_code == 403
    driver = _client_with_role("driver")
    assert driver.get(f"{V}/events").status_code == 403  # events carry passwords
    assert TestClient(app).get(f"{V}/events").status_code == 401


def test_run_applies_the_event_to_a_server():
    eid = _make()
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    r = api.post(f"{V}/events/{eid}/run", json={"server_id": sid, "restart": False})
    assert r.status_code == 200 and r.json()["restarted"] is False
    cfg = r.json()["config"]
    assert cfg["SERVER"]["TRACK"] == "evspa" and cfg["SERVER"]["CARS"] == "evbmw;evaudi" and cfg["RACE"]["LAPS"] == 8
    assert cfg["SERVER"]["REVERSED_GRID_RACE_POSITIONS"] == 3 and cfg["SERVER"]["LOOP_MODE"] == 0
    ini = api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    assert "REVERSED_GRID_RACE_POSITIONS=3" in ini and "LOOP_MODE=0" in ini
    assert api.post(f"{V}/events/{eid}/run", json={"server_id": 99999, "restart": False}).status_code == 404


def test_run_rechecks_the_content_that_is_installed_now():
    eid = _make("fragile")
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    shutil.rmtree(content._tracks_dir() / "evspa")  # the track was removed after the event was saved
    r = api.post(f"{V}/events/{eid}/run", json={"server_id": sid, "restart": False})
    assert r.status_code == 400 and "not installed" in r.json()["detail"]
    _install()


def test_default_preset_is_unique_and_entries_are_validated():
    a = api.post(f"{V}/events", json={"title": "A", "session": SESSION}).json()
    b = api.post(f"{V}/events", json={"title": "B", "session": SESSION}).json()
    assert not a["is_default"] and not a["derived"]
    assert api.post(f"{V}/events/{a['id']}/default").json()["is_default"]
    assert api.post(f"{V}/events/{b['id']}/default").json()["is_default"]
    flags = {e["id"]: e["is_default"] for e in api.get(f"{V}/events").json()}
    assert flags[b["id"]] and not flags[a["id"]]
    d = api.post(f"{V}/events", json={"title": "copy", "session": SESSION, "derived": True}).json()
    assert d["derived"] and api.post(f"{V}/events/{d['id']}/default").status_code == 400

    def entry(model, guid):
        return {"model": model, "guid": guid}
    ok = {**SESSION, "entries": [entry("evbmw", "76561190000000001"), entry("evaudi", "76561190000000002")]}
    assert api.post(f"{V}/events", json={"title": "ok", "session": ok}).status_code == 201
    other_car = {**SESSION, "entries": [entry("evferrari", "76561190000000001")]}
    assert api.post(f"{V}/events", json={"title": "x", "session": other_car}).status_code == 422
    twice = {**SESSION, "entries": [entry("evbmw", "76561190000000001"), entry("evaudi", "76561190000000001")]}
    assert api.post(f"{V}/events", json={"title": "x", "session": twice}).status_code == 422
    locked_empty = {**SESSION, "locked": True, "entries": [entry("evbmw", "")]}
    assert api.post(f"{V}/events", json={"title": "x", "session": locked_empty}).status_code == 422
