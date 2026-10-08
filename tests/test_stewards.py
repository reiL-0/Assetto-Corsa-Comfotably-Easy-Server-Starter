from conftest import ADMIN
from fastapi.testclient import TestClient

from app.live import acsp
from app.main import app
from app.stewards import engine
from app.stewards.detectors import detect

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
A, B = "76561190000000001", "76561190000000002"


def _hit(car, other=None, speed=40.0):
    return {"type": "client_event", "event_type": acsp.COLLISION_WITH_CAR if other is not None else acsp.COLLISION_WITH_ENV,
            "car_id": car, "other_car_id": other, "speed": speed, "world_pos": [1.0, 2.0, 3.0], "rel_pos": [0, 0, 0]}


def test_detect_wall_contact_cuts_and_ignores_the_rest():
    assert detect(_hit(1))["kind"] == "wall"
    assert detect(_hit(1, 2))["kind"] == "contact"
    assert detect(_hit(1, speed=3.0)) is None   # a scrape
    assert detect({"type": "lap_completed", "car_id": 4, "cuts": 2})["value"] == 2
    assert detect({"type": "lap_completed", "car_id": 4, "cuts": 0}) is None
    assert detect({"type": "car_update"}) is None


def test_shadow_mode_records_one_incident_per_contact_and_off_records_nothing():
    sid = api.post(f"{V}/servers", json={"name": "st"}).json()["id"]
    c = acsp.ACSPClient(sid)
    c.cars = {0: {"driver_guid": A, "driver_name": "A"}, 1: {"driver_guid": B, "driver_name": "B"}}
    c._apply(_hit(0, 1))
    assert api.get(f"{V}/servers/{sid}/incidents").json() == []   # off by default

    assert api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"}).json()["stewards"] == "shadow"
    c._apply(_hit(0, 1))
    c._apply(_hit(1, 0))   # the same contact seen from the other car, within a second
    c._apply(_hit(0))
    c._apply({"type": "lap_completed", "car_id": 1, "laptime_ms": 90000, "cuts": 3, "leaderboard": [], "grip_level": None})
    rows = api.get(f"{V}/servers/{sid}/incidents").json()
    assert sorted(r["kind"] for r in rows) == ["contact", "cuts", "wall"]
    contact = next(r for r in rows if r["kind"] == "contact")
    assert {contact["driver_guid"], contact["other_guid"]} == {A, B}
    assert next(r for r in rows if r["kind"] == "cuts")["value"] == 3
    assert [r["kind"] for r in api.get(f"{V}/servers/{sid}/incidents?kind=wall").json()] == ["wall"]


def test_dedup_window_expires():
    sid = api.post(f"{V}/servers", json={"name": "st2"}).json()["id"]
    api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"})
    c = acsp.ACSPClient(sid)
    engine.on_event(c, _hit(0), now=1000.0)
    engine.on_event(c, _hit(0), now=1000.5)
    engine.on_event(c, _hit(0), now=1002.0)
    assert len(api.get(f"{V}/servers/{sid}/incidents").json()) == 2
