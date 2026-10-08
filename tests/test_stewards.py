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
    engine.on_event(c, _hit(0), now=1004.0)
    assert len(api.get(f"{V}/servers/{sid}/incidents").json()) == 2


def _car(x, z, vx, vz=0.0):
    return {"pos": [x, 0.0, z], "velocity": [vx, 0.0, vz]}


def test_at_fault_blames_only_a_clear_rear_end_hit():
    from app.stewards.detectors import at_fault
    assert at_fault(_car(0, 0, 20), _car(5, 0, 10))[0] == 0     # a drives into b's back
    assert at_fault(_car(5, 0, 10), _car(0, 0, 20))[0] == 1     # same, roles swapped
    assert at_fault(_car(0, 0, 20), _car(0, 3, 20))[0] is None  # side by side
    assert at_fault(_car(0, 0, 20), _car(5, 0, -20))[0] is None # head-on
    assert at_fault(_car(0, 0, 0), _car(5, 0, 0))[0] is None    # both stopped


def test_contact_incident_keeps_the_suggested_fault_and_the_evidence():
    sid = api.post(f"{V}/servers", json={"name": "st3"}).json()["id"]
    api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"})
    c = acsp.ACSPClient(sid)
    c.cars = {0: {"driver_guid": A, "driver_name": "A", **_car(0, 0, 20)}, 1: {"driver_guid": B, "driver_name": "B", **_car(5, 0, 10)}}
    c._apply(_hit(1, 0))   # reported by the car in front: it is still A who hit B
    row = api.get(f"{V}/servers/{sid}/incidents").json()[0]
    assert row["fault_guid"] == A and len(row["evidence"]["cars"]) == 2


def test_project_moves_a_stale_position_forward_but_not_too_far():
    from app.stewards.detectors import project
    car = {"pos": [0.0, 0.0, 0.0], "velocity": [10.0, 0.0, 0.0], "seen": 100.0}
    assert round(project(car, 100.2)["pos"][0], 6) == 2.0
    assert project(car, 160.0)["pos"][0] == 5.0   # capped at MAX_AGE_S (0.5 s)


def test_a_real_33_byte_car_update_parses():
    # captured from a real acServer: gear is one byte, so the datagram is 33 bytes
    raw = bytes.fromhex("3500d2bdb6c339792440aac4de4207291c3f7d1f5f3e9dc16cc206e934c8a8b63d")
    e = acsp.parse(raw)
    assert e["type"] == "car_update" and e["car_id"] == 0 and e["gear"] == 6 and e["rpm"] == 13545
    assert round(e["pos"][0]) == -365 and round(e["velocity"][2]) == -59 and 0.08 < e["spline_pos"] < 0.1
