"""Track-limit reports with their decision (2026-11-23: 44 of 87 cuts of one race were filtered and nothing said by how much, so nobody could tell whether a filter was too strict or too lax):
the audit keeps what the driver's game saw, and the stewards read it per driver (GET /servers/{id}/track-limits/reports)."""
import asyncio
import secrets
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

import app.stewards.api as api
from app.auth import _sha
from app.db import engine
from app.live import racecontrol as rc
from app.main import app
from app.models import Token, User
from app.stewards import limits_config as store
from app.stewards.api import LimitsIn, report_limits
from app.stewards.limits_filter import TrackLimitsConfig as Config

V = "/api/v1"
BASE = dict(wheels=4, speed=120.0, ms=1000, vo=100.0, vi=100.0, vp=100.0, lap=3, spline=0.4, pos=[1.0, 2.0, 3.0])
SID = 931


def _as(role):
    raw = secrets.token_urlsafe(16)
    with Session(engine) as s:
        u = User(username=f"{role}-{raw[:5]}", role=role)
        s.add(u)
        s.commit()
        s.add(Token(user_id=u.id, token_hash=_sha(raw), name="t", created_at=datetime.now(UTC)))
        s.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


@pytest.fixture
def world(monkeypatch):
    session = {"session_type": 3}
    client = SimpleNamespace(cars={1: {"driver_name": "Ana", "driver_guid": "76561190000000001"}, 2: {"driver_name": "Beto", "driver_guid": "76561190000000002"}}, session=session, session_t0=time.time() - 60)
    monkeypatch.setattr(api, "_get", lambda *a: None)
    monkeypatch.setattr(api.supervisor, "live", lambda: [SimpleNamespace(server_id=SID, acsp=client)])
    monkeypatch.setattr(store, "load", lambda sid: Config(min_speed_kmh=40, between_s=3))
    state = rc.RaceControl(session)
    monkeypatch.setattr(rc, "_states", {SID: state})
    audit = Mock()
    monkeypatch.setattr(state, "audit", audit)
    monkeypatch.setattr(api.engine, "record_limits", Mock(return_value=True))
    monkeypatch.setattr(rc, "on_cuts", Mock())
    monkeypatch.setattr(rc, "on_excursion", Mock())

    async def immediate(fn, *args):
        return fn(*args)
    monkeypatch.setattr(api.asyncio, "to_thread", immediate)
    api._recent.pop(SID, None)
    yield client, state, audit
    api._recent.pop(SID, None)


def _report(car, name, now, monkeypatch, **over):
    monkeypatch.setattr(api.time, "monotonic", lambda: now)
    asyncio.run(report_limits(SID, LimitsIn(car_id=car, name=name, **{**BASE, **over}), None))


def test_every_decision_keeps_what_the_game_reported_in_the_audit_and_for_the_stewards(world, monkeypatch):
    client, state, audit = world
    _report(1, "Ana", 10, monkeypatch, vo=30.0)      # under 40 km/h: filtered (min_speed)
    _report(1, "Ana", 20, monkeypatch)               # counted
    _report(2, "Beto", 21, monkeypatch, ms=4000)     # 4 s outside: over the default 3 s of max_cut_s, filtered
    kinds = [(c.args[0], c.kwargs.get("reason")) for c in audit.call_args_list]
    assert ("cut_filtered", "min_speed") in kinds and ("cut_counted", None) in kinds
    for c in audit.call_args_list:
        report = c.kwargs["report"]
        assert set(report) == {"ms", "wheels", "speed", "vo", "vi", "vp", "lap", "spline"} and "pos" not in report, "the evidence, not where on the map"
    rows = list(api._recent[SID])
    assert [(r["name"], r["guid"], r["decision"]) for r in rows] == [("Ana", "76561190000000001", "min_speed"), ("Ana", "76561190000000001", "counted"), ("Beto", "76561190000000002", "max_cut_s")]
    assert rows[0]["report"]["vo"] == 30.0 and rows[0]["session_type"] == 3


def test_stewards_read_a_summary_per_driver_and_can_filter_it(world, monkeypatch):
    _report(1, "Ana", 10, monkeypatch, vo=30.0)
    _report(1, "Ana", 20, monkeypatch)
    _report(1, "Ana", 21, monkeypatch)                # 1 s later: between_s
    _report(2, "Beto", 30, monkeypatch, ms=4000)
    steward = _as("steward")
    r = steward.get(f"{V}/servers/{SID}/track-limits/reports")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(d["name"], d["counted"], d["filtered"]) for d in body["summary"]] == [("Ana", 1, {"min_speed": 1, "between_s": 1}), ("Beto", 0, {"max_cut_s": 1})], "the most filtered first"
    assert len(body["reports"]) == 4
    only = steward.get(f"{V}/servers/{SID}/track-limits/reports?guid=76561190000000002").json()
    assert [r["name"] for r in only["reports"]] == ["Beto"]
    one = steward.get(f"{V}/servers/{SID}/track-limits/reports?decision=between_s").json()
    assert [r["decision"] for r in one["reports"]] == ["between_s"]
    assert len(steward.get(f"{V}/servers/{SID}/track-limits/reports?limit=2").json()["reports"]) == 2


def test_only_the_current_session_unless_asked_for_all(world, monkeypatch):
    client, _, _ = world
    _report(1, "Ana", 10, monkeypatch)
    api._recent[SID].append({"t": client.session_t0 - 500, "session_type": 2, "car_id": 1, "name": "Ana", "guid": "76561190000000001", "decision": "min_speed", "report": {}})   # the qualifying before
    steward = _as("steward")
    assert len(steward.get(f"{V}/servers/{SID}/track-limits/reports").json()["reports"]) == 1
    assert len(steward.get(f"{V}/servers/{SID}/track-limits/reports?all_sessions=true").json()["reports"]) == 2


def test_the_reports_are_for_stewards_not_for_drivers_or_the_website_reporter(world):
    for role in ("driver", "reporter"):
        assert _as(role).get(f"{V}/servers/{SID}/track-limits/reports").status_code == 403, role
    assert TestClient(app).get(f"{V}/servers/{SID}/track-limits/reports").status_code == 401
    assert _as("steward").get(f"{V}/servers/{SID}/track-limits/reports").status_code == 200


def test_memory_is_bounded_per_server(world, monkeypatch):
    for i in range(api.REPORTS_KEPT + 50):
        _report(1, "Ana", 100 + 10 * i, monkeypatch)
    assert len(api._recent[SID]) == api.REPORTS_KEPT
    summary = _as("steward").get(f"{V}/servers/{SID}/track-limits/reports?limit=5").json()
    assert summary["summary"][0]["counted"] > 0 and len(summary["reports"]) == 5


def test_with_the_server_stopped_the_last_reports_kept_are_still_read(world, monkeypatch):
    """After a race the server idles out and that is when a steward reads them: no running session, nothing to cut them at."""
    _report(1, "Ana", 10, monkeypatch, vo=30.0)
    monkeypatch.setattr(api.supervisor, "live", lambda: [])
    body = _as("steward").get(f"{V}/servers/{SID}/track-limits/reports").json()
    assert [r["decision"] for r in body["reports"]] == ["min_speed"]
