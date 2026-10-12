import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app.live import acsp
from app.main import app
from app.stewards import engine
from app.stewards.detectors import detect

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
A, B = "76561190000000001", "76561190000000002"


@pytest.fixture(autouse=True)
def _intake_open():
    engine.accept(True)   # another test's app shutdown (lifespan) closes the intake for the rest of the process


def _incidents(url):
    assert engine.flush()   # the incidents are written by a background thread: wait for it before reading
    return api.get(url)


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
    assert _incidents(f"{V}/servers/{sid}/incidents").json() == []   # off by default

    assert api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"}).json()["stewards"] == "shadow"
    c._apply(_hit(0, 1))
    c._apply(_hit(1, 0))   # the same contact seen from the other car, within a second
    c._apply(_hit(0))
    c._apply({"type": "lap_completed", "car_id": 1, "laptime_ms": 90000, "cuts": 3, "leaderboard": [], "grip_level": None})
    rows = _incidents(f"{V}/servers/{sid}/incidents").json()
    assert sorted(r["kind"] for r in rows) == ["contact", "cuts", "wall"]
    contact = next(r for r in rows if r["kind"] == "contact")
    assert {contact["driver_guid"], contact["other_guid"]} == {A, B}
    assert next(r for r in rows if r["kind"] == "cuts")["value"] == 3
    assert [r["kind"] for r in _incidents(f"{V}/servers/{sid}/incidents?kind=wall").json()] == ["wall"]


def test_dedup_window_expires():
    sid = api.post(f"{V}/servers", json={"name": "st2"}).json()["id"]
    api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"})
    c = acsp.ACSPClient(sid)
    engine.on_event(c, _hit(0), now=1000.0)
    engine.on_event(c, _hit(0), now=1000.5)
    engine.on_event(c, _hit(0), now=1004.0)
    assert len(_incidents(f"{V}/servers/{sid}/incidents").json()) == 2


def _car(x, z, vx, vz=0.0):
    return {"pos": [x, 0.0, z], "velocity": [vx, 0.0, vz]}


def test_at_fault_blames_only_a_clear_rear_end_hit():
    from app.stewards.detectors import at_fault
    assert at_fault(_car(0, 0, 20), _car(5, 0, 10))[0] == 0     # a drives into b's back
    assert at_fault(_car(5, 0, 10), _car(0, 0, 20))[0] == 1     # same, roles swapped
    assert at_fault(_car(0, 0, 20), _car(0, 3, 20))[0] is None  # side by side
    assert at_fault(_car(0, 0, 20), _car(5, 0, -20))[0] is None # head-on
    assert at_fault(_car(0, 0, 0), _car(5, 0, 0))[0] is None    # both stopped


def _upd(car, x, z, vx):
    return {"type": "car_update", "car_id": car, "pos": [x, 0.0, z], "velocity": [vx, 0.0, 0.0], "gear": 3, "rpm": 5000, "spline_pos": 0.1}


def test_contact_incident_keeps_the_suggested_fault_and_the_evidence():
    sid = api.post(f"{V}/servers", json={"name": "st3"}).json()["id"]
    api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"})
    c = acsp.ACSPClient(sid)
    c.cars = {0: {"driver_guid": A, "driver_name": "A"}, 1: {"driver_guid": B, "driver_name": "B"}}
    hit = _hit(1, 0); hit["world_pos"] = [100.0, 0.0, 0.0]
    c._apply(_upd(0, 98.0, 0.0, 20.0)); c._apply(_upd(1, 102.0, 0.0, 10.0))   # A right behind B, a moment before the hit
    c._apply(_upd(0, 160.0, 0.0, 20.0)); c._apply(_upd(1, 150.0, 0.0, 10.0))  # ...and well past it when the late report arrives
    c._apply(hit)
    row = _incidents(f"{V}/servers/{sid}/incidents").json()[0]
    assert row["fault_guid"] == A and len(row["evidence"]["cars"]) == 2


def test_at_contact_uses_the_moment_they_touched_not_where_they_are_now():
    from app.stewards.detectors import at_contact
    # a (fast) closes on b (slow) along x; they touch at x~100, then drift 50 m apart before the late report arrives
    ta = [(t / 5, [20.0 * t, 0, 0], [20.0, 0, 0]) for t in range(12)]
    tb = [(t / 5, [103.0 + 10.0 * (t - 5), 0, 0.0], [10.0, 0, 0]) for t in range(12)]
    pa, pb, _ = at_contact(ta, tb, [100.0, 0, 0])
    assert abs(pb["pos"][0] - pa["pos"][0]) < 25 and pa["pos"][0] < pb["pos"][0]   # a still behind b
    assert at_contact([], tb, [0, 0, 0]) is None


def test_a_real_33_byte_car_update_parses():
    # captured from a real acServer: gear is one byte, so the datagram is 33 bytes
    raw = bytes.fromhex("3500d2bdb6c339792440aac4de4207291c3f7d1f5f3e9dc16cc206e934c8a8b63d")
    e = acsp.parse(raw)
    assert e["type"] == "car_update" and e["car_id"] == 0 and e["gear"] == 6 and e["rpm"] == 13545
    assert round(e["pos"][0]) == -365 and round(e["velocity"][2]) == -59 and 0.08 < e["spline_pos"] < 0.1


def test_a_drivers_track_limit_report_becomes_an_incident_only_in_shadow_mode(monkeypatch):
    from types import SimpleNamespace

    from app import supervisor
    sid = api.post(f"{V}/servers", json={"name": "st4"}).json()["id"]
    c = acsp.ACSPClient(sid)
    c.cars = {3: {"driver_guid": A, "driver_name": "Ana"}}
    monkeypatch.setattr(supervisor, "live", lambda: [SimpleNamespace(server_id=sid, acsp=c)])
    body = {"car_id": 3, "name": "ana", "ms": 1800, "wheels": 4, "speed": 120.5, "lap": 2, "spline": 0.4, "pos": [1.0, 2.0, 3.0]}
    url = f"{V}/servers/{sid}/stewards/report"
    assert api.post(url, json=body).status_code == 204
    assert _incidents(f"{V}/servers/{sid}/incidents").json() == []   # off: ignored on purpose
    api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"})
    assert api.post(url, json=body).status_code == 204
    assert api.post(url, json=body).status_code == 204            # same instant: a flood, ignored
    rows = _incidents(f"{V}/servers/{sid}/incidents").json()
    assert len(rows) == 1 and rows[0]["kind"] == "limits" and rows[0]["value"] == 1800 and rows[0]["driver_guid"] == A
    assert rows[0]["evidence"]["source"] == "client" and rows[0]["world_pos"] == [1.0, 2.0, 3.0]
    assert api.post(url, json={**body, "name": "someone else"}).status_code == 409   # that slot is not them
    assert api.post(url, json={**body, "wheels": 9}).status_code == 422


# --- the writer thread: the UDP callback never waits for SQLite ------------------------------------------------------------------------

def _shadow_server(name):
    sid = api.post(f"{V}/servers", json={"name": name}).json()["id"]
    api.put(f"{V}/servers/{sid}/stewards", json={"mode": "shadow"})
    c = acsp.ACSPClient(sid)
    c.cars = {i: {"driver_guid": f"G{i}", "driver_name": f"D{i}"} for i in range(8)}
    return sid, c


def _blocked_writer(monkeypatch, fail=()):
    """The writer's database waits on an Event (and fails for the listed incident numbers): what a busy SQLite looks like to it."""
    import threading

    gate, calls, real = threading.Event(), [], engine.Session
    monkeypatch.setattr(engine, "_stats", {"written": 0, "failed": 0, "dropped": 0, "ignored": 0})
    monkeypatch.setattr(engine, "_recent", {})

    def session(*a, **k):
        calls.append(1)
        gate.wait(5)
        if len(calls) in fail:
            raise RuntimeError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(engine, "Session", session)
    return gate


def test_the_callback_returns_at_once_and_a_full_queue_drops_the_newest_and_counts_it(monkeypatch):
    import time
    sid, c = _shadow_server("w1")
    gate = _blocked_writer(monkeypatch)
    monkeypatch.setattr(engine, "MAX_QUEUED", 2)
    t0 = time.monotonic()
    engine.on_event(c, _hit(0), now=1000.0)
    deadline = time.monotonic() + 3
    while engine._current is None:   # the writer took it and is now stuck in the database
        assert time.monotonic() < deadline, "the writer never took the incident"
        time.sleep(0.01)
    for car in range(1, 5):   # different cars = different dedup keys: 2 wait in the queue, 2 do not fit
        engine.on_event(c, _hit(car), now=1000.0 + car)
    assert time.monotonic() - t0 < 0.5, "on_event waited for the database"
    assert engine.stats()["dropped"] == 2 and engine.stats()["queued"] == 3   # 1 being written + 2 waiting
    gate.set()
    assert engine.flush()
    rows = _incidents(f"{V}/servers/{sid}/incidents").json()
    assert [r["car_id"] for r in sorted(rows, key=lambda r: r["ts"])] == [0, 1, 2]   # the oldest three, in order; the newest were dropped
    assert engine.stats()["written"] == 3
    engine.on_event(c, _hit(4), now=1004.0)   # a dropped event did not keep its dedup key: it can be reported again
    assert engine.flush() and len(_incidents(f"{V}/servers/{sid}/incidents").json()) == 4


def test_a_failed_write_is_counted_and_frees_the_key_so_the_next_report_is_recorded(monkeypatch):
    sid, c = _shadow_server("w2")
    gate = _blocked_writer(monkeypatch, fail={1})
    gate.set()
    engine.on_event(c, _hit(0), now=2000.0)
    assert engine.flush() and engine.stats()["failed"] == 1 and engine.stats()["written"] == 0
    assert _incidents(f"{V}/servers/{sid}/incidents").json() == []
    engine.on_event(c, _hit(0), now=2000.5)   # inside the 3 s window: only possible because the failure released the key
    assert engine.flush() and engine.stats()["written"] == 1
    assert len(_incidents(f"{V}/servers/{sid}/incidents").json()) == 1


def test_a_late_failure_does_not_free_the_key_of_a_newer_report(monkeypatch):
    sid, c = _shadow_server("w3")
    gate = _blocked_writer(monkeypatch, fail={1})
    engine.on_event(c, _hit(0), now=3000.0)       # stuck in the writer, and it will fail
    engine.on_event(c, _hit(0), now=3010.0)       # the same key, later: admitted behind it
    key = (sid, "wall", (0, None))
    gate.set()
    assert engine.flush()
    assert engine._recent.get(key) == 3010.0       # the old failure left the newer reservation alone
    assert engine.stats()["failed"] == 1 and engine.stats()["written"] == 1


def test_mode_off_when_the_writer_gets_there_is_ignored_and_counted(monkeypatch):
    sid = api.post(f"{V}/servers", json={"name": "w4"}).json()["id"]   # off by default
    c = acsp.ACSPClient(sid)
    c.cars = {0: {"driver_guid": A, "driver_name": "A"}}
    gate = _blocked_writer(monkeypatch)
    gate.set()
    engine.on_event(c, _hit(0), now=4000.0)
    assert engine.flush() and engine.stats()["ignored"] == 1
    assert _incidents(f"{V}/servers/{sid}/incidents").json() == []


def test_nothing_is_admitted_after_the_intake_is_closed_for_shutdown(monkeypatch):
    sid, c = _shadow_server("w5")
    gate = _blocked_writer(monkeypatch)
    gate.set()
    engine.accept(False)
    try:
        engine.on_event(c, _hit(0), now=5000.0)
        assert engine.stats()["queued"] == 0 and engine.flush()
    finally:
        engine.accept(True)
    engine.on_event(c, _hit(0), now=5000.1)   # reopened (a new startup): admitted again, and the closed attempt left no dedup key behind
    assert engine.flush() and len(_incidents(f"{V}/servers/{sid}/incidents").json()) == 1
