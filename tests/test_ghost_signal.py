"""The server may keep a disconnected car in its roster: stale velocity must not trigger a caution."""
import time

from app import racecontrol as monitor
from app.live.acsp import ACSPClient
from app.live.racecontrol import RaceControl


def test_stale_car_does_not_propose_nascar_caution(monkeypatch):
    from types import SimpleNamespace
    client = ACSPClient(840)
    client.session = {"session_index": 0, "session_type": 3}
    client.cars[2] = {"driver_guid": "76561190000000001", "driver_name": "slow", "velocity": [0, 0, 0]}
    client.field.update(2, 0.2, now=time.monotonic() - 30)
    rc = RaceControl(client.session)
    rc.nascar = SimpleNamespace(phase="racing", laps=2)
    inst = SimpleNamespace(server_id=840, acsp=client)
    monkeypatch.setattr(monitor.supervisor, "live", lambda: [inst])
    monkeypatch.setattr(monitor, "state_of", lambda *args, **kw: rc)
    monkeypatch.setattr(monitor, "_non_racing_guids", lambda now: frozenset())
    monkeypatch.setattr("app.league.neutralization_rule_cached", lambda sid: {"mode": "off"})
    # Seed the real watcher with a moving reading. A retained zero used to open a
    # caution after six seconds; a stale reading must instead leave it untouched.
    rc.watch.step(time.time() - 35, {2: 150}, set(), True)
    rc.watch.step(time.time() - 25, {2: 0}, set(), True)
    monitor.incident_tick(time.time())
    assert rc.watch.proposal is None
    assert rc.flag == "green"
    assert 2 not in rc.watch._moved
    # A new, fresh speed still reaches the real watcher.
    client.field.update(2, 0.2)
    client.cars[2]["velocity"] = [40, 0, 0]
    monitor.incident_tick(time.time())
    assert 2 in rc.watch._moved


def test_autoflags_ignores_stale_speed(monkeypatch):
    from types import SimpleNamespace
    client = ACSPClient(842)
    client.session = {"session_type": 3}
    client.cars[2] = {"driver_guid": "76561190000000001", "velocity": [0, 0, 0]}
    client.field.update(2, 0.2, now=time.monotonic() - 30)
    rc = RaceControl(client.session)
    seen = []
    monkeypatch.setattr(rc, "autoflags_tick", lambda samples, speeds, length, mono, now: seen.append(speeds))
    monkeypatch.setattr(rc, "pass_tick", lambda *args: [])
    monkeypatch.setattr(rc, "pace_tick", lambda *args: [])
    monkeypatch.setattr(monitor.supervisor, "live", lambda: [SimpleNamespace(server_id=842, acsp=client)])
    monkeypatch.setattr(monitor, "state_of", lambda *args, **kw: rc)
    monkeypatch.setattr(monitor, "_non_racing_guids", lambda now: frozenset())
    monkeypatch.setattr(monitor, "flag_modes_peek", lambda sid: {"blue": "shadow", "danger": "shadow"})
    monitor.autoflags_tick(time.time())
    client.field.update(2, 0.2)
    monitor.autoflags_tick(time.time())
    assert seen == [{}, {2: 0.0}]


def test_kick_requires_matching_driver_and_fails_closed_on_reused_slot(monkeypatch):
    from fastapi.testclient import TestClient
    from conftest import ADMIN
    from app.main import app
    from app.live.acsp import encode_kick_user
    from app import supervisor
    from sqlmodel import Session
    from app.db import engine
    from app.models import Server
    from types import SimpleNamespace
    with Session(engine) as s:
        row = Server(name="ghost-kick", base_port=12500)
        s.add(row); s.commit(); sid = row.id
    client, sent = ACSPClient(sid), []
    client.send = sent.append
    monkeypatch.setitem(supervisor._instances, sid, SimpleNamespace(acsp=client))
    client.cars[3] = {"driver_guid": "76561190000000001"}
    api = TestClient(app, headers=ADMIN)
    assert api.get(f"/api/v1/servers/{sid}/cars").json()["3"]["signal"] == "unknown"
    url = f"/api/v1/servers/{sid}/kick/3"
    assert api.post(url, json={"driver_guid": "76561190000000002"}).status_code == 409
    assert not sent
    assert api.post(url, json={"driver_guid": "76561190000000001"}).status_code == 200
    assert sent == [encode_kick_user(3)]
    client.cars[3] = {"driver_guid": "76561190000000002"}   # same slot, somebody else now
    assert api.post(url, json={"driver_guid": "76561190000000001"}).status_code == 409
    assert sent == [encode_kick_user(3)], "never kick the replacement"
    client.cars.pop(3)
    assert api.post(url, json={"driver_guid": "76561190000000002"}).status_code == 409
    assert api.post(url, json={}).status_code == 422, "old slot-only requests cannot kick a new occupant"
    assert api.post(f"/api/v1/servers/{sid}/kick/300", json={"driver_guid": "76561190000000001"}).status_code == 422


def test_car_snapshot_marks_stale_signal_without_claiming_disconnected():
    client = ACSPClient(841)
    client.cars[2] = {"driver_guid": "76561190000000001", "driver_name": "slow"}
    client.field.update(2, 0.2, now=time.monotonic() - 30)
    row = client.snapshot()[2]
    assert row["signal"] == "stale" and row["signal_age_s"] >= 25
    assert row.get("driver_guid") == "76561190000000001"
