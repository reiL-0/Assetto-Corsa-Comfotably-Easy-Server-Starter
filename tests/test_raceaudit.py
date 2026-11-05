"""Audit persistence, bounded failure and decisions without external services."""
import json
import logging
import queue
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.live import raceaudit
from app.live.field import Field
from app.live.racecontrol import RaceControl
from app.rulebook import Neutralization


def test_jsonl_and_thread_safety():
    sid = 987654
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: raceaudit.record(sid, "test", number=n, executed=False), range(40)))
    assert raceaudit.flush()
    events = raceaudit.read(sid, limit=100)["events"]
    assert len(events) == 40
    assert {e["number"] for e in events} == set(range(40))
    assert all(e["ts"].endswith("+00:00") and e["session"] is None for e in events)
    assert raceaudit._path().stat().st_mode & 0o777 == 0o600
    assert raceaudit._path().parent.stat().st_mode & 0o777 == 0o700
    assert len(raceaudit.read(sid, limit=3)["events"]) == 3
    assert not raceaudit.read(sid, since="9999")["events"]


def test_rotation(tmp_path):
    path = tmp_path / "events.jsonl"
    handler = raceaudit._Handler(path, maxBytes=180, backupCount=2)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for n in range(40):
            handler.handle(logging.LogRecord("test", logging.INFO, "", 0, json.dumps({"n": n, "text": "x" * 50}), (), None))
    finally:
        handler.close()
    assert len(list(tmp_path.iterdir())) == 3
    for file in tmp_path.iterdir():
        assert file.stat().st_size <= 180
        assert file.stat().st_mode & 0o777 == 0o600
        for line in file.read_text().splitlines():
            assert isinstance(json.loads(line)["n"], int)


def test_write_failure_counted_without_raising(monkeypatch):
    assert raceaudit.flush()
    before = raceaudit.stats()["errors"]
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(raceaudit._Handler, "handle", fail)
    raceaudit.record(987655, "failure")
    assert raceaudit.flush()
    assert raceaudit.stats()["errors"] == before + 1


def test_invalid_evidence_never_raises():
    before = raceaudit.stats()
    raceaudit.record(1, "invalid", value=object())
    assert raceaudit.stats()["errors"] == before["errors"] + 1
    assert raceaudit.stats()["dropped"] == before["dropped"]


def test_full_queue_is_observable(monkeypatch):
    assert raceaudit.flush()
    class Worker:
        def is_alive(self):
            return True
    pending = queue.Queue(maxsize=1)
    pending.put({})
    before = raceaudit.stats()["dropped"]
    monkeypatch.setattr(raceaudit, "_worker", Worker())
    monkeypatch.setattr(raceaudit, "_pending", pending)
    raceaudit.record(1, "full")
    assert raceaudit.stats()["dropped"] == before + 1


@pytest.mark.parametrize("mode,stale,killed", [("shadow", False, False), ("live", False, False), ("live", True, False), ("live", False, True)])
def test_neutralization_decisions(monkeypatch, mode, stale, killed):
    events = []
    monkeypatch.setattr(raceaudit, "record", lambda sid, kind, **fields: events.append({"kind": kind, **fields}))
    rc = RaceControl({"session_type": 3, "rulebook_revision": 7})
    rc.neutralization_rule = Neutralization(mode=mode).model_dump()
    if killed:
        rc.auto_off.add("neutralization")
    initial = (rc.flag, rc.pace_kmh, rc.rev, rc.ts, rc._neutralization_flag,
               rc._neutralization_sc, rc.pace_out, rc.caution, list(rc.penalties))
    field = Field()
    clock = [0]
    monkeypatch.setattr("app.live.racecontrol.time.monotonic", lambda: clock[0])
    for t in range(9):
        clock[0] = t
        for car in range(3):
            field.update(car, .1, now=t - (20 if stale else 0))
        rc.incident_step({car: 180 if t == 0 else 0 for car in range(3)}, field, 1000 + t)
    if stale or killed:
        gate = next(e for e in events if e["kind"] == "neutralization_gate")
        assert not gate["executed"]
        assert gate["stale_cars"] if stale else gate["kill_switch"]
        assert not any(e["kind"] == "neutralization_applied" for e in events)
    elif mode == "shadow":
        decision = next(e for e in events if e["kind"] == "neutralization_decision")
        assert not decision["executed"] and decision["rulebook_revision"] == 7
        assert decision["decision"]["speeds"] == {0: 0, 1: 0, 2: 0}
        assert not any(e["kind"] == "neutralization_applied" for e in events)
        assert (rc.flag, rc.pace_kmh, rc.rev, rc.ts, rc._neutralization_flag,
                rc._neutralization_sc, rc.pace_out, rc.caution, rc.penalties) == initial
    else:
        assert any(e["kind"] == "neutralization_applied" and e["executed"] for e in events)


def test_endpoint_permissions_and_limits():
    from conftest import ADMIN
    from fastapi.testclient import TestClient

    from app.main import app
    api = TestClient(app, headers=ADMIN)
    sid = api.post("/api/v1/servers", json={"name": "audit"}).json()["id"]
    url = f"/api/v1/servers/{sid}/race-control/audit"
    raceaudit.record(sid, "endpoint_test", executed=False)
    assert raceaudit.flush()
    result = api.get(url, params={"limit": 1})
    assert result.status_code == 200
    assert result.json()["events"][0]["kind"] == "endpoint_test"
    assert "errors" in result.json()["stats"]
    assert api.get(url, params={"limit": 1001}).status_code == 422
    assert api.get(url, params={"since": "invalid"}).status_code == 422
    assert TestClient(app).get(url).status_code in (401, 403)


def test_periodic_status_and_pace_park(monkeypatch):
    events = []
    monkeypatch.setattr(raceaudit, "record", lambda sid, kind, **fields: events.append({"kind": kind, **fields}))
    rc = RaceControl({"session_type": 3})
    rc.neutralization_rule = Neutralization(mode="live").model_dump()
    rc.set_pace_car(9)
    rc.set_flag("yellow")
    rc.set_flag("green")
    assert any(e["kind"] == "state_change" and e["state"]["pace_park"] for e in events)
    rc.set_flag("vsc", pace_kmh=120)
    field = Field()
    for t in range(12):
        monkeypatch.setattr("app.live.racecontrol.time.monotonic", lambda t=t: t)
        field.update(1, 2.1, now=t)
        rc.incident_step({1: 100}, field, 1000 + t)
    periodic = [e for e in events if e["kind"] in ("auto_green_status", "neutralization_status")]
    assert len(periodic) == 3


def test_unreadable_file_still_exposes_write_counters(monkeypatch, tmp_path):
    monkeypatch.setattr(raceaudit, "_path", lambda: tmp_path)
    response = raceaudit.read(1)
    assert response["read_error"] == "IsADirectoryError"
    assert "errors" in response["stats"]


def test_full_queue_skips_serialization_of_giant_field(monkeypatch):
    assert raceaudit.flush()
    pending = queue.Queue(maxsize=1)
    pending.put({})
    monkeypatch.setattr(raceaudit, "_pending", pending)
    calls = []
    monkeypatch.setattr(raceaudit.json, "dumps", lambda *a, **kw: calls.append(a) or "{}")
    before = raceaudit.stats()["dropped"]
    class Evidence(dict):
        def items(self):
            calls.append("snapshot")
            raise AssertionError("full queue must not inspect evidence")
    raceaudit.record(1, "full_giant", value=Evidence(giant="x" * 1_000_000))
    assert calls == []
    assert raceaudit.stats()["dropped"] == before + 1


def test_giant_evidence_is_truncated_and_detached():
    evidence = {"text": "x" * 1_000_000, "items": list(range(10000)), "nested": [[[[[[[[["deep"]]]]]]]]]}
    raceaudit.record(987656, "giant", value=evidence)
    evidence["text"] = "changed"
    assert raceaudit.flush()
    events = raceaudit.read(987656)["events"]
    assert len(events) == 1
    line = json.dumps(events[0])
    assert len(line.encode()) < 128 * 1024
    assert "truncated" in events[0]["value"]["text"]
    assert "truncated" in str(events[0]["value"]["items"][-1])
    assert "truncated" in str(events[0]["value"]["nested"])


def test_276_automatic_decisions_in_one_step_are_accounted():
    from app.live.field import Sample

    rc = RaceControl({"session_type": 3})
    rc.server_id = 987657
    samples = {car: Sample(car + .125, 0, 0, 10, 0) for car in range(24)}
    rc.autoflags_tick(samples, {car: 100 for car in samples}, 5000, 10, 1000)
    assert raceaudit.flush()
    events = raceaudit.read(987657, limit=1000)["events"]
    decisions = [e for e in events if e["kind"] == "automatic_flag"]
    gaps = [e for e in events if e["kind"] == "audit_gap"]
    assert len(rc.autoflags.blue) == 276
    assert len(decisions) == 276 or (len(decisions) == 200 and sum(e["lost"] for e in gaps) == 76)


def test_close_drains_stops_and_rejects_records(monkeypatch, tmp_path):
    # A separate module instance isolates its terminal lifecycle from other tests.
    import importlib.util

    spec = importlib.util.spec_from_file_location("isolated_raceaudit", raceaudit.__file__)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    path = tmp_path / "audit" / "events.jsonl"
    monkeypatch.setattr(audit, "_path", lambda: path)
    for number in range(40):
        audit.record(1, "before_close", number=number)
    assert audit.close(timeout=2)
    assert not audit._worker.is_alive()
    assert [json.loads(line)["number"] for line in path.read_text().splitlines()] == list(range(40))
    before = audit.stats()
    audit.record(1, "after_close", invalid=object())
    assert audit.stats() == {**before, "dropped": before["dropped"] + 1}
    assert audit.close(timeout=0.1)


def test_field_files_isolate_servers_and_retain_oldest_closed(tmp_path, monkeypatch):
    import importlib.util
    import os
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('isolated_field_retention', raceaudit.__file__)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    monkeypatch.setattr(audit, 'settings', SimpleNamespace(data_dir=tmp_path))
    directory = tmp_path / 'race-audit' / 'field'
    directory.mkdir(parents=True)
    old = directory / 'server-9-session-old-000000.jsonl'
    newer = directory / 'server-8-session-newer-000000.jsonl'
    old.write_text('x' * 100)
    newer.write_text('y' * 100)
    os.utime(old, (1, 1))
    os.utime(newer, (2, 2))
    monkeypatch.setattr(audit, 'FIELD_TOTAL_BYTES', 300, raising=False)
    for sid in (1, 2):
        assert audit.enqueue({'format': 'acm-field-v1', 'server_id': sid, 'session': 'start', 'ts': 1000, 'seq': 1})
    assert audit.flush()
    assert not old.exists()
    assert newer.exists()
    files = list(directory.glob('*start*.jsonl'))
    assert len(files) == 2
    assert {json.loads(p.read_text())['server_id'] for p in files} == {1, 2}
    assert audit.close(2)


def test_field_file_cap_preserves_active_other_server(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(raceaudit, 'settings', SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(raceaudit, 'FIELD_FILE_BYTES', 180)
    monkeypatch.setattr(raceaudit, 'FIELD_TOTAL_BYTES', 350)
    writer = raceaudit._FieldWriter()
    event = {'format': 'acm-field-v1', 'server_id': 1, 'session': '1000-a', 'seq': 1}
    try:
        writer.write(event)
        other = next(writer.directory.glob('server-1-*'))
        writer.write({**event, 'server_id': 2})
        for seq in range(2, 8):
            writer.write({**event, 'server_id': 2, 'seq': seq})
        assert other.exists()
        assert json.loads(other.read_text())['server_id'] == 1
        assert all(p.stat().st_size <= 180 for p in writer.directory.iterdir())
        assert sum(p.stat().st_size for p in writer.directory.iterdir()) <= 350
    finally:
        writer.close()
