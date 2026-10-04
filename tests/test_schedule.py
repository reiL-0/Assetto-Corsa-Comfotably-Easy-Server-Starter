import asyncio
import time

from conftest import ADMIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import discord, schedule
from app.db import engine
from app.main import app
from app.models import Schedule

client = TestClient(app, headers=ADMIN)


def _setup(start_in: float, reminders=(60, 10), duration=None):
    sid = client.post("/api/v1/servers", json={"name": "SchedServer"}).json()["id"]
    eid = client.post("/api/v1/events", json={"title": "Endurance R5", "session": {"name": "x", "track": "spa", "cars": ["bmw"]}}).json()["id"]
    r = client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + start_in,
                                               "reminders": list(reminders), "duration_min": duration})
    assert r.status_code == 201, r.text
    return sid, r.json()


def _tick(now, monkeypatch, applied=None):
    said = []
    monkeypatch.setattr(discord, "announce", said.append)

    async def fake_apply(sess, srv, body):
        if isinstance(applied, Exception):
            raise applied
        (applied if applied is not None else []).append((srv.id, body.restart))
    monkeypatch.setattr(schedule, "apply_to_server", fake_apply)
    asyncio.run(schedule.tick(now))
    return said


def _row(sc_id):
    with Session(engine) as s:
        return s.get(Schedule, sc_id)


def test_validation():
    sid, sc = _setup(3600)
    eid = sc["event_id"]
    assert client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() - 5}).status_code == 422
    assert client.post("/api/v1/schedules", json={"event_id": eid, "server_id": 9999, "start_at": time.time() + 9}).status_code == 404
    assert client.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + 9, "reminders": [0]}).status_code == 422
    assert any(s["id"] == sc["id"] for s in client.get("/api/v1/schedules").json())
    assert client.delete(f"/api/v1/schedules/{sc['id']}").status_code == 204


def test_reminders_then_start(monkeypatch):
    sid, sc = _setup(7200)
    t0 = sc["start_at"]
    started = []
    assert _tick(t0 - 7000, monkeypatch, started) == []                      # too early: nothing
    said = _tick(t0 - 3500, monkeypatch, started)                             # inside the 60 min mark
    assert len(said) == 1 and "Endurance R5" in said[0] and f"<t:{int(t0)}:R>" in said[0]
    assert _tick(t0 - 3400, monkeypatch, started) == []                      # not repeated
    assert len(_tick(t0 - 500, monkeypatch, started)) == 1                    # the 10 min mark
    assert started == [] and _row(sc["id"]).sent == [60, 10]
    said = _tick(t0 + 1, monkeypatch, started)
    assert started == [(sid, True)] and _row(sc["id"]).state == "done" and "en marcha" in said[-1]
    assert _tick(t0 + 30, monkeypatch, started) == [] and len(started) == 1  # done ones are left alone


def test_late_manager_posts_only_the_nearest_reminder_and_old_starts_are_missed(monkeypatch):
    _, sc = _setup(7200)
    t0 = sc["start_at"]
    assert len(_tick(t0 - 300, monkeypatch, [])) == 1 and _row(sc["id"]).sent == [60, 10]   # both due at once -> one post
    started = []
    _tick(t0 + schedule.LATE + 5, monkeypatch, started)
    r = _row(sc["id"])
    assert started == [] and r.state == "missed" and r.result


def test_failed_start_is_reported(monkeypatch):
    _, sc = _setup(100, reminders=())
    said = _tick(sc["start_at"] + 1, monkeypatch, HTTPException(409, "track not installed"))
    r = _row(sc["id"])
    assert r.state == "failed" and "track not installed" in r.result and "no pudo iniciarse" in said[-1]


class _FakeAcsp:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)


class _FakeInstance:
    def __init__(self):
        self.running, self.acsp, self.stopped_for = True, _FakeAcsp(), None

    async def stop(self, reason="manual"):
        self.running, self.stopped_for = False, reason


def test_a_scheduled_event_with_a_duration_runs_then_stops_the_server(monkeypatch):
    sid, sc = _setup(100, reminders=(), duration=60)
    t0, inst = sc["start_at"], _FakeInstance()
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: inst)
    started = []
    said = _tick(t0 + 1, monkeypatch, started)
    assert started == [(sid, True)] and _row(sc["id"]).state == "running" and "en marcha" in said[-1]
    assert _tick(t0 + 1800, monkeypatch, []) == [] and inst.running                    # half way: nothing
    assert _tick(t0 + 3600 - 200, monkeypatch, []) == [] and _row(sc["id"]).end_warned  # 5 min before the end: chat, not Discord
    assert len(inst.acsp.sent) == 1
    said = _tick(t0 + 3601, monkeypatch, [])
    r = _row(sc["id"])
    assert not inst.running and inst.stopped_for == "event_end" and r.state == "done" and "terminó" in said[-1]
    assert _tick(t0 + 3700, monkeypatch, []) == []


def test_window_opens_an_hour_before_and_closes_at_the_end(monkeypatch):
    sid, sc = _setup(7200, reminders=(), duration=90)
    t0 = sc["start_at"]
    with Session(engine) as s:
        assert schedule.open_window(s, sid, t0 - 3601) is None
        assert schedule.open_window(s, sid, t0 - 3500).id == sc["id"]
        assert schedule.open_window(s, sid, t0 + 90 * 60 - 1).id == sc["id"]
        assert schedule.open_window(s, sid, t0 + 90 * 60) is None


def test_wake_loads_the_event_once_then_just_starts_the_server(monkeypatch):
    sid, sc = _setup(1800, reminders=(), duration=60)
    t0, applied, started = sc["start_at"], [], []
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)

    async def fake_apply(sess, srv, body):
        applied.append((srv.id, body.restart))

    async def fake_start(server_id, sess):
        started.append(server_id)
    monkeypatch.setattr(schedule, "apply_to_server", fake_apply)
    monkeypatch.setattr(schedule, "start_server", fake_start)
    assert asyncio.run(schedule.wake(sid, t0 - 7200)) is False and applied == []          # window not open yet
    assert asyncio.run(schedule.wake(sid, t0 - 600)) is True and applied == [(sid, True)] and _row(sc["id"]).loaded
    assert asyncio.run(schedule.wake(sid, t0 - 300)) is True and started == [sid] and len(applied) == 1   # loaded: no second apply
    # at the start time the event is already on the server: not applied again (it would kick the early arrivals)
    _tick(t0 + 1, monkeypatch, [])
    assert len(applied) == 1 and _row(sc["id"]).state == "running"
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _FakeInstance())
    assert asyncio.run(schedule.wake(sid, t0 + 10)) is False                                # already running

