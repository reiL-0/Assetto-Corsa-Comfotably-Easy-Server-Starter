"""Phase 0 regressions, isolated SQLite files and filesystem."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app import catalog, metrics, schedule, servers, supervisor
from app.config import settings
from app.main import count_server_errors
from app.models import (Activity, CatalogEvent, Championship, ChampionshipEvent, ContentHolder, ContentSeal, Incident, Penalty,
                        Schedule, Server, Setting, Token, User)
from app.services import server_service


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    @event.listens_for(db, "connect")
    def pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
    SQLModel.metadata.create_all(db)
    for module in (catalog, metrics, schedule):
        monkeypatch.setattr(module, "engine", db)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(server_service, "server_locks", {})
    monkeypatch.setattr(supervisor, "_instances", {})
    yield db
    db.dispose()


def create(db):
    with Session(db) as sess:
        return servers.create(servers.ServerIn(name="phase0"), sess).id


def test_ids_survive_delete_and_new_sessions_without_removing_history(store):
    sid = create(store)
    results = Path(settings.data_dir) / "instances" / str(sid) / "results"
    results.mkdir(parents=True)
    (results / "race.json").write_text("{}")
    with Session(store) as sess:
        sess.add(Activity(server_id=sid, ts=0, kind="old"))
        sess.commit()
        asyncio.run(servers.delete(sid, sess))
        assert sess.exec(select(Activity)).one().server_id == sid
    assert create(store) > sid
    assert (results / "race.json").exists()
    with Session(store) as sess:
        saved = sess.get(Setting, "server_id_seq").value
        for row in sess.exec(select(Server)):
            sess.delete(row)
        sess.commit()
    assert create(store) > saved


@pytest.mark.parametrize("model,values", [
    (Activity, dict(ts=0, kind="old")),
    (Incident, dict(ts=0, kind="wall", car_id=0)),
    (Penalty, dict(filename="r", driver_guid="g", kind="time", reason="old")),
    (Schedule, dict(event_id=1, start_at=0)),
])
def test_ids_skip_orphan_history(store, model, values):
    with Session(store) as sess:
        sess.add_all([model(server_id=i, **values) for i in (1, 2)])
        sess.commit()
    assert create(store) >= 3


def test_ids_skip_championship_results(store):
    with Session(store) as sess:
        league = Championship(name="old")
        sess.add(league)
        sess.commit()
        sess.add(ChampionshipEvent(championship_id=league.id, server_id=37, filename="race.json"))
        sess.commit()
    assert create(store) == 38


def test_ids_skip_folders_and_persisted_counter_and_concurrent_creates(store):
    (Path(settings.data_dir) / "instances" / "41").mkdir(parents=True)
    assert create(store) == 42
    with Session(store) as sess:
        seq = sess.get(Setting, "server_id_seq")
        seq.value = 80
        sess.commit()
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: create(store), range(2)))
    assert sorted(ids) == [81, 82]


def test_delete_waits_for_start_and_checks_running_inside_lock(store, monkeypatch):
    sid = create(store)
    async def start_inside_lock(server_id, sess):
        assert server_service.server_lock(server_id).locked()
        await asyncio.sleep(0.02)
        monkeypatch.setitem(supervisor._instances, sid, SimpleNamespace(running=True))
        return {}
    monkeypatch.setattr(server_service, "start_unlocked", start_inside_lock)
    async def scenario():
        with Session(store) as starting, Session(store) as deleting:
            start = asyncio.create_task(server_service.start(starting, sid))
            await asyncio.sleep(0)
            task = asyncio.create_task(servers.delete(sid, deleting))
            await asyncio.sleep(0)
            assert not task.done()
            await start
            with pytest.raises(HTTPException) as error:
                await task
            assert error.value.status_code == 409
            assert deleting.get(Server, sid)

    asyncio.run(scenario())


def test_retention_batches_and_preserves_boundaries_and_other_tables(store):
    now = datetime(2026, 10, 10, tzinfo=UTC)
    cutoff = now.timestamp() - 120 * 86400
    with Session(store) as sess:
        user = User(username="retention")
        sess.add(user)
        sess.commit()
        sess.add_all([Activity(server_id=1, ts=cutoff - 1, kind="old") for _ in range(7)])
        sess.add(Activity(server_id=1, ts=cutoff, kind="keep"))
        for i, expiry in enumerate((None, now, now + timedelta(days=1), now - timedelta(seconds=1))):
            sess.add(Token(user_id=user.id, token_hash=str(i), expires_at=expiry))
        sess.add(ContentSeal(key="car:keep"))
        sess.add(Incident(server_id=1, ts=0, kind="wall", car_id=0))
        sess.add(Schedule(server_id=1, event_id=1, start_at=0, state="done"))
        sess.commit()
    deletes = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE"):
            deletes.append((statement, parameters))
    event.listen(store, "before_cursor_execute", capture)
    assert metrics.purge(now.timestamp(), batch_size=2) == 8
    assert len(deletes) == 5
    with Session(store) as sess:
        assert sess.exec(select(Activity)).one().kind == "keep"
        assert len(sess.exec(select(Token)).all()) == 3
        assert sess.exec(select(Incident)).one()
        assert sess.exec(select(Schedule)).one()
        assert sess.get(ContentSeal, "car:keep")


def test_periodic_retention_does_not_block_loop_and_waits_on_shutdown(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(metrics, "purge", slow)
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(metrics.purge_forever(stop, interval=0.01))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        # Keep checking progress with a bounded clock, including on restricted
        # runtimes where the selector's cross-thread wakeup is unavailable.
        for _ in range(2000):
            if task.done():
                break
            await asyncio.sleep(0.001)
        assert task.done()
        await task
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert calls == [calls[0]] and calls[0] != threading.get_ident()


def test_retention_repeats_and_stops_between_runs(monkeypatch):
    calls = []
    def purge():
        calls.append(threading.get_ident())
    monkeypatch.setattr(metrics, "purge", purge)
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(metrics.purge_forever(stop, interval=0.01))
        for _ in range(2000):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.001)
        assert len(calls) >= 2
        stop.set()
        for _ in range(2000):
            if task.done():
                break
            await asyncio.sleep(0.001)
        assert task.done()
        await task
        count = len(calls)
        await asyncio.sleep(0.02)
        assert len(calls) == count
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_catalog_repeat_new_holder_source_and_reactivation(store):
    args = ("car", "repeat", "a" * 64, 5, 1)
    catalog.record_upload(*args, actor="upload")
    with Session(store) as sess:
        row = sess.exec(select(ContentHolder)).one()
        dates = row.attested_at, row.updated_at
    catalog.record_upload(*args, actor="upload")
    with Session(store) as sess:
        row = sess.exec(select(ContentHolder)).one()
        assert (row.attested_at, row.updated_at) == dates
        assert len(sess.exec(select(CatalogEvent)).all()) == 1
    catalog.record_upload(*args, holder="new", actor="upload")
    catalog.record_upload(*args, source_url="https://example.com/mod", actor="upload")
    with Session(store) as sess:
        row = sess.exec(select(ContentHolder).where(ContentHolder.holder == "league")).one()
        row.status = "superseded"
        sess.commit()
    catalog.record_upload(*args, actor="upload")
    with Session(store) as sess:
        assert len(sess.exec(select(CatalogEvent).where(CatalogEvent.action == "upload")).all()) == 4


def test_catalog_concurrent_scan_upload_is_idempotent_without_memory(store):
    args = ("car", "concurrent", "b" * 64, 5, 1)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda actor: catalog.record_upload(*args, actor=actor), ("scan", "upload")))
    catalog._scanned.clear()
    catalog.record_upload(*args, actor="scan")
    with Session(store) as sess:
        assert len(sess.exec(select(ContentHolder)).all()) == 1
        assert len(sess.exec(select(CatalogEvent)).all()) == 1


def test_scan_twice_after_memory_reset(store, monkeypatch, tmp_path):
    from app import content
    folder = tmp_path / "cars" / "scan_me"
    folder.mkdir(parents=True)
    (folder / "data.acd").write_bytes(b"x")
    monkeypatch.setattr(content, "_cars_dir", lambda: folder.parent)
    monkeypatch.setattr(catalog, "_scan_todo", lambda: [("car", "scan_me")])
    for _ in range(2):
        catalog._scanned.clear()
        catalog._scan_lock.acquire()
        catalog._scan_run()
    with Session(store) as sess:
        assert [e.action for e in sess.exec(select(CatalogEvent))] == ["scan"]


def test_async_metrics_reach_queue_not_direct_log(store, monkeypatch):
    # Keep the real enqueue, but intercept the writer to distinguish its thread.
    assert metrics.flush()
    loop_thread = threading.get_ident()
    written = []
    def writer(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        written.append(args)
        return True
    monkeypatch.setattr(metrics, "log", writer)
    sid = create(store)
    with Session(store) as sess:
        srv = sess.get(Server, sid)
        srv.wake = "always"
        sess.commit()
    async def fake_start(*args):
        return {}
    monkeypatch.setattr(server_service, "start", fake_start)
    async def scenario():
        request = SimpleNamespace(url=SimpleNamespace(path="/broken"))
        async def response(_):
            return SimpleNamespace(status_code=503)
        await count_server_errors(request, response)
        assert await schedule.wake(sid)
        inst = supervisor.Instance(999, Path(settings.data_dir),
                                   proc=SimpleNamespace(pid=999, returncode=3))
        monkeypatch.setattr(inst, "_tail", lambda: None)
        await inst._watch()
    asyncio.run(scenario())
    assert metrics.flush()
    assert (0, "http_5xx") in written and (sid, "wake") in written
    assert (999, "server_crash") in written


def test_installer_private_umask_documented():
    root = Path(__file__).resolve().parents[1]
    assert "[Service]\nUMask=0077\n" in (root / "ops/install.sh").read_text()
    readme = (root / "ops/README.md").read_text()
    assert "systemctl edit acm" in readme and "[Service]\nUMask=0077" in readme


def test_repeat_upload_supersedes_a_version_left_active_by_a_restore(store):
    a, b = ("car", "mod", "a" * 64, 5, 1), ("car", "mod", "b" * 64, 6, 1)
    catalog.record_upload(*a, actor="upload")
    catalog.record_upload(*b, actor="upload")   # A superseded
    with Session(store) as sess:   # what /holders/{id}/restore does to A
        row = sess.exec(select(ContentHolder).where(ContentHolder.hash == "a" * 64)).one()
        row.status = "active"
        sess.commit()
    catalog.record_upload(*a, actor="upload")   # A is already active: still must retire B
    with Session(store) as sess:
        status = {h.hash[0]: h.status for h in sess.exec(select(ContentHolder))}
    assert status == {"a": "active", "b": "superseded"}


def test_cancelled_delete_keeps_the_server_lock_until_the_worker_ends(store, monkeypatch):
    sid = create(store)
    release, entered = threading.Event(), threading.Event()
    real_get = servers._get
    def slow_get(sess, server_id):
        entered.set()
        release.wait(5)
        return real_get(sess, server_id)
    monkeypatch.setattr(servers, "_get", slow_get)
    async def scenario():
        with Session(store) as sess:
            task = asyncio.create_task(servers.delete(sid, sess))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.05)
            assert server_service.server_lock(sid).locked()   # the worker is still deleting
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not server_service.server_lock(sid).locked()

    asyncio.run(scenario())
