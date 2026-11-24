"""The practice of an event begins ON its hour (server 3, 2026-11-22: a player woke the server 9 minutes early and the whole cycle ran 9 minutes ahead of what was announced). A server that opens
before the hour keeps a practice with no end until the hour; at the hour it gets its own length and a RESTART_SESSION (nobody is disconnected: measured on the simulator); a stopped one is loaded again;
an event with no practice is not woken before its hour. Qualifying and the race may come a few minutes late, never early: they follow the practice."""
import asyncio
import time

import pytest

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import schedule
from app.db import engine
from app.live import acsp
from app.main import app
from app.models import Schedule
from app.schemas.servers import UNLIMITED_MIN, SequenceStep, SessionIn
from app.services import server_service
from app.services.server_service import ApplyResult

api = TestClient(app, headers=ADMIN)
_made: list[int] = []   # the schedules this module creates: the database is shared with tests/test_schedule.py, whose ticks would pick up a pending one


@pytest.fixture(autouse=True)
def _clean_up():
    yield
    with Session(engine) as s:
        for sc_id in _made:
            row = s.get(Schedule, sc_id)
            if row:
                s.delete(row)
        s.commit()
    _made.clear()


def _setup(start_in, practice_min=10):
    sid = api.post("/api/v1/servers", json={"name": "OnTime"}).json()["id"]
    api.put(f"/api/v1/servers/{sid}/wake", json={"mode": "window"})
    session = {"name": "x", "track": "spa", "cars": ["bmw"], **({"practice_min": practice_min} if practice_min else {}), "qualify_min": 10, "race_laps": 5}
    eid = api.post("/api/v1/events", json={"title": "On time", "session": session}).json()["id"]
    r = api.post("/api/v1/schedules", json={"event_id": eid, "server_id": sid, "start_at": time.time() + start_in, "reminders": [], "duration_min": 90})
    assert r.status_code == 201, r.text
    _made.append(r.json()["id"])
    return sid, r.json()


def _row(sc_id):
    with Session(engine) as s:
        return s.get(Schedule, sc_id)


def _set(sc_id, **kw):
    with Session(engine) as s:
        row = s.get(Schedule, sc_id)
        for k, v in kw.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


class _Inst:
    running = True

    def __init__(self, client):
        self.acsp = client


def _race_client(sid, session_t0, session_type=1):
    c = acsp.ACSPClient(sid)
    c.session = {"session_type": session_type, "session_index": 0, "name": "Practice", "time_min": UNLIMITED_MIN}
    c.session_t0 = session_t0
    sent = []

    def send(data):
        sent.append(data)
        if data == acsp.encode_restart_session():   # acServer begins the session again: its clock starts now
            c.session_t0 = time.time()
    c.send = send
    return c, sent


def _server_session(sid, **session):
    from app.models import Server
    with Session(engine) as db:
        row = db.get(Server, sid)
        row.session = session
        db.add(row)
        db.commit()


def _fake_apply(monkeypatch, applied):
    async def fake_apply(sess, srv, body, **kw):
        applied.append(body)
        srv.session = {"practice_min": body.practice_min, "sequence": [st.model_dump() for st in body.sequence] if body.sequence else None}   # (the real apply stores what it applied)
        sess.add(srv)
        sess.commit()
        return ApplyResult(restarted=True)

    async def fake_start(sess, server_id):
        applied.append(("start", server_id))
    monkeypatch.setattr(server_service, "apply", fake_apply)
    monkeypatch.setattr(server_service, "start", fake_start)


def test_a_server_woken_before_the_hour_gets_a_practice_with_no_end_and_one_woken_at_the_hour_its_own(monkeypatch):
    sid, sc = _setup(1800)
    t0, applied = sc["start_at"], []
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)
    _fake_apply(monkeypatch, applied)
    assert asyncio.run(schedule.wake(sid, t0 - 540)) is True
    assert applied[0].practice_min == UNLIMITED_MIN and applied[0].qualify_min == 10, "early: the practice waits for the hour, the rest is the event's"
    sid2, sc2 = _setup(300)
    _set(sc2["id"], start_at=time.time() - 2)   # its hour came 2 s ago
    applied2 = []
    _fake_apply(monkeypatch, applied2)
    assert asyncio.run(schedule.wake(sid2, time.time())) is True
    assert applied2[0].practice_min == 10, "at the hour the practice is the event's own"


def test_an_event_with_no_practice_is_not_woken_before_its_hour(monkeypatch):
    sid, sc = _setup(1800, practice_min=None)
    t0, applied = sc["start_at"], []
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)
    _fake_apply(monkeypatch, applied)
    assert asyncio.run(schedule.wake(sid, t0 - 540)) is False and applied == [] and not _row(sc["id"]).loaded, "its qualifying or race would begin early"
    _set(sc["id"], start_at=time.time() - 2)
    assert asyncio.run(schedule.wake(sid, time.time())) is True, "at the hour it opens"


def test_the_practice_helpers_cover_a_plain_event_an_unlimited_one_and_a_sequence():
    plain = SessionIn(name="x", track="spa", cars=["bmw"], practice_min=15, qualify_min=10, race_laps=5)
    assert schedule.has_practice(plain) and schedule.nominal_practice_min(plain) == 15
    assert schedule.early_practice(plain).practice_min == UNLIMITED_MIN
    assert plain.practice_min == 15, "the event itself is not touched"
    unlimited = plain.model_copy(update={"practice_min": UNLIMITED_MIN})
    assert schedule.has_practice(unlimited) and schedule.nominal_practice_min(unlimited) == 0, "an unlimited practice never advances: nothing to put right"
    none = plain.model_copy(update={"practice_min": None})
    assert not schedule.has_practice(none) and schedule.nominal_practice_min(none) == 0
    steps = [SequenceStep(kind="practice", name="P", time_min=20), SequenceStep(kind="qualify", name="Q", time_min=10), SequenceStep(kind="practice", name="P2", time_min=5), SequenceStep(kind="race", name="R", laps=5)]
    seq = plain.model_copy(update={"sequence": steps})
    assert schedule.nominal_practice_min(seq) == 20
    late = plain.model_copy(update={"sequence": [SequenceStep(kind="qualify", name="Q", time_min=10), *steps[:1], steps[2], steps[3]]})
    assert not schedule.has_practice(late) and schedule.nominal_practice_min(late) == 0, "a practice that is not the first session is no use to wait in: the ones before it would begin early"
    early = schedule.early_practice(seq)
    assert [st.time_min for st in early.sequence] == [schedule.EARLY_STEP_MIN, 10, 5, 0], "only the OPENING practice waits for the hour"


def test_at_the_hour_the_practice_that_began_early_is_started_again_with_its_own_length_and_nobody_is_disconnected(monkeypatch):
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)   # the hour has just come; the server was woken 9 minutes ago
    _server_session(sid, practice_min=UNLIMITED_MIN, qualify_min=10, race_laps=5)   # with the early configuration
    c, sent = _race_client(sid, now - 540)
    calls = []
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c))

    async def fake_apply(sess, srv, body, **kw):
        calls.append(("apply", body.restart, body.practice_min))
        srv.session = {"practice_min": body.practice_min}   # (the real apply stores what it applied)
        sess.add(srv)
        sess.commit()
        return ApplyResult(restarted=False)
    monkeypatch.setattr(server_service, "apply", fake_apply)
    monkeypatch.setattr(c, "send", lambda d: (calls.append(("send", d)), sent.append(d), setattr(c, "session_t0", time.time()) if d == acsp.encode_restart_session() else None)[0])
    asyncio.run(schedule.realign_tick(now))
    assert calls == [("apply", False, 10), ("send", acsp.encode_set_session_info(0, "Practice", 1, 0, 10, 0)), ("send", acsp.encode_restart_session())], calls   # its own config FIRST: the new session is anchored with it
    assert c.session_t0 >= now - 0.5 - 1, "acServer began the practice again"
    n = len(calls)
    asyncio.run(schedule.realign_tick(now + 1))
    asyncio.run(schedule.realign_tick(now + 2))
    assert len(calls) == n, "once: the practice clock already starts at the hour, nothing more is done"
    assert _row(sc["id"]).loaded and _row(sc["id"]).state == "pending", "the realign changes nothing else: the tick does the rest"


def test_the_realign_works_when_the_tick_has_already_moved_the_schedule_to_running(monkeypatch):
    """The tick and the realign are two tasks: whichever comes first, the practice is put right (a first version only looked at `pending`)."""
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, state="running", start_at=now - 0.5)
    c, sent = _race_client(sid, now - 540)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c))
    _fake_apply(monkeypatch, [])
    asyncio.run(schedule.realign_tick(now))
    assert sent[:2] == [acsp.encode_set_session_info(0, "Practice", 1, 0, 10, 0), acsp.encode_restart_session()]


def test_what_changed_while_the_realign_waited_for_the_lock_is_looked_at_again(monkeypatch):
    """A steward can move the session while the realign waits for the server's lock: the commands go only if the practice is still the one that needs it."""
    import contextlib
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)
    c, sent = _race_client(sid, now - 540)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c))
    _fake_apply(monkeypatch, [])

    @contextlib.asynccontextmanager
    async def steward_moves_on(_id):
        c.session = {**c.session, "session_type": 2}   # qualifying began meanwhile
        yield
    monkeypatch.setattr(server_service, "server_lock", steward_moves_on)
    asyncio.run(schedule.realign_tick(now))
    assert sent == []


def test_nothing_is_reset_when_the_practice_already_began_at_the_hour_or_the_cycle_is_past_it_or_the_hour_is_old(monkeypatch):
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)
    c, sent = _race_client(sid, now - 0.4)   # not woken early: the practice began at the hour
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c))
    asyncio.run(schedule.realign_tick(now))
    assert sent == []
    c2, sent2 = _race_client(sid, now - 540, session_type=2)   # the cycle is already in qualifying: a practice cannot be gone back to
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c2))
    asyncio.run(schedule.realign_tick(now))
    assert sent2 == []
    c3, sent3 = _race_client(sid, now - 700)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c3))
    asyncio.run(schedule.realign_tick(now + schedule.REALIGN_WINDOW + 5))
    assert sent3 == [], "the manager was down at the hour: a practice running for minutes is not reset now"


def test_a_stopped_server_that_still_holds_the_early_configuration_is_loaded_again_at_the_hour_whatever_starts_it(monkeypatch):
    """It was woken early (practice with no end), stopped (idle) and the hour came: the tick or a wake would start it with that practice. It is applied again with the event's own."""
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)
    _server_session(sid, practice_min=UNLIMITED_MIN, qualify_min=10, race_laps=5)   # what the early opening stored
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)
    applied = []
    _fake_apply(monkeypatch, applied)
    monkeypatch.setattr(schedule.discord, "announce", lambda *a, **k: None)
    asyncio.run(schedule.tick(now + 1))
    assert applied and applied[0].practice_min == 10 and applied[0].restart is True and _row(sc["id"]).state == "running"
    assert asyncio.run(schedule.wake(sid, time.time())) is True, "(and a wake finds it loaded: no second apply)"
    assert len([a for a in applied if not isinstance(a, tuple)]) == 1


def test_a_server_that_holds_the_events_own_configuration_is_not_applied_again_and_a_running_one_is_left_to_the_realign(monkeypatch):
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)
    _server_session(sid, practice_min=10, qualify_min=10, race_laps=5)   # already the event's own: nothing is stale
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)
    applied = []
    _fake_apply(monkeypatch, applied)
    monkeypatch.setattr(schedule.discord, "announce", lambda *a, **k: None)
    asyncio.run(schedule.tick(now + 1))
    assert applied == [], "a restart of the event in the middle of it is not what a stopped server needs"
    sid2, sc2 = _setup(300)
    _set(sc2["id"], loaded=True, start_at=time.time() - 0.5)
    _server_session(sid2, practice_min=UNLIMITED_MIN, qualify_min=10, race_laps=5)
    c, _ = _race_client(sid2, time.time() - 540)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c))
    asyncio.run(schedule.tick(time.time() + 1))
    assert applied == [], "a RUNNING server with the early configuration is the realign's: applying with restart would kick everybody"


def test_an_event_with_an_unlimited_or_no_practice_is_left_alone_at_the_hour(monkeypatch):
    sid, sc = _setup(300, practice_min=None)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: None)
    asyncio.run(schedule.realign_tick(now))
    assert _row(sc["id"]).loaded, "no practice was ever shortened: nothing to put back"


def test_the_loop_sleeps_only_until_the_next_hour_when_it_is_near():
    sid, sc = _setup(7)
    w = schedule.next_wait(time.time())
    assert 0.2 <= w <= 7.1, f"the next event begins in 7 s: the loop must not sleep {schedule.TICK} s ({w})"


def test_the_announced_hours_count_the_results_screen_between_sessions():
    """2026-11-22, server 2: practice 10 min + qualifying 12 min announced 04:35 and 04:47; the real ones were 04:37:46 and 04:51:43 because acServer shows the results (120 s there) after each session."""
    from app import announcement
    start = 1_000_000.0
    session = {"track": "spa", "cars": ["bmw"], "practice_min": 15, "qualify_min": 15, "race_laps": 10}
    text = announcement.render(announcement.DEFAULT, announcement.variables("T", "S", session, start, {"yes": 0, "maybe": 0, "no": 0}, result_s=120))["content"]
    assert f"Práctica: 15 min · <t:{int(start)}:t>" in text
    assert f"Clasificación: 15 min · <t:{int(start) + 900 + 120}:t>" in text, "after the practice and its results screen"
    assert f"10 vueltas · <t:{int(start) + 1800 + 240}:t>" in text, "after two results screens"
    assert f"<t:{int(start) + 1800}:t>" not in text, "never the old hour: it is BEFORE the real one"


def test_the_results_screen_comes_from_the_servers_config_and_a_bad_value_is_acservers_default():
    from types import SimpleNamespace
    assert schedule._result_s(SimpleNamespace(config={"SERVER": {"RESULT_SCREEN_TIME": "120"}})) == 120
    assert schedule._result_s(SimpleNamespace(config={"SERVER": {"RESULT_SCREEN_TIME": 30}})) == 30
    assert schedule._result_s(SimpleNamespace(config={})) == 10 and schedule._result_s(SimpleNamespace(config=None)) == 10
    assert schedule._result_s(SimpleNamespace(config={"SERVER": {"RESULT_SCREEN_TIME": "x"}})) == 10
    assert schedule._result_s(SimpleNamespace(config={"SERVER": {"RESULT_SCREEN_TIME": -5}})) == 0


def test_the_published_announcement_uses_the_servers_results_screen():
    """The message that is posted (and edited when it changes) is built by _rsvp_payload from the server's own config."""
    from app.models import Event, Server
    sid, sc = _setup(3600)
    with Session(engine) as s:
        row, srv = s.get(Schedule, sc["id"]), s.get(Server, sid)
        ev = s.get(Event, row.event_id)
        srv.config = {**(srv.config or {}), "SERVER": {**((srv.config or {}).get("SERVER") or {}), "RESULT_SCREEN_TIME": 120}}
        text = schedule._rsvp_payload(s, row, ev, srv, {"yes": 0, "maybe": 0, "no": 0})["content"]
        start = int(row.start_at)
        assert f"Clasificación: 10 min · <t:{start + 600 + 120}:t>" in text, text
        s.rollback()


def test_the_events_own_configuration_is_stored_only_while_the_stored_one_is_the_early_one_and_is_tried_again_if_it_failed(monkeypatch):
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)
    c, sent = _race_client(sid, now - 540)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c))
    calls = []

    async def failing_apply(sess, srv, body, **kw):
        calls.append(body.restart)
        raise server_service.ServerError(500, "disk full")
    monkeypatch.setattr(server_service, "apply", failing_apply)
    _server_session(sid, practice_min=UNLIMITED_MIN, qualify_min=10, race_laps=5)
    asyncio.run(schedule.realign_tick(now))
    assert calls == [False] and len(sent) >= 2, "the apply failed: the practice was put right anyway"
    asyncio.run(schedule.realign_tick(now + 1))
    assert calls == [False, False], "the practice began at the hour but the stored configuration is still the early one: tried again"
    applied = []
    _fake_apply(monkeypatch, applied)
    asyncio.run(schedule.realign_tick(now + 2))
    asyncio.run(schedule.realign_tick(now + 3))
    assert len(applied) == 1, "stored once, then nothing more"
    # an administrator's own apply is not undone
    sid2, sc2 = _setup(300)
    _set(sc2["id"], loaded=True, start_at=time.time() - 0.5)
    _server_session(sid2, practice_min=25, qualify_min=10, race_laps=5)
    c2, sent2 = _race_client(sid2, time.time() - 540)
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c2))
    applied2 = []
    _fake_apply(monkeypatch, applied2)
    asyncio.run(schedule.realign_tick(time.time()))
    assert applied2 == [], "the stored configuration is not the early one: it is the administrator's"


def test_the_practice_of_the_hour_can_be_kept_across_an_idle_stop(monkeypatch):
    sid, sc = _setup(300)
    now = time.time()
    _set(sc["id"], loaded=True, start_at=now - 0.5)
    _server_session(sid, practice_min=UNLIMITED_MIN, qualify_min=10, race_laps=5)
    c, sent = _race_client(sid, now - 540)
    c.keep_off = False
    monkeypatch.setattr(schedule.supervisor, "get", lambda _id: _Inst(c))

    async def apply_that_turns_keeping_off(sess, srv, body, **kw):
        c.keep_off = True   # what the real apply does
        return ApplyResult(restarted=False)
    monkeypatch.setattr(server_service, "apply", apply_that_turns_keeping_off)
    asyncio.run(schedule.realign_tick(now))
    assert sent and c.keep_off is False, "the new practice is kept across an idle stop like any other"
