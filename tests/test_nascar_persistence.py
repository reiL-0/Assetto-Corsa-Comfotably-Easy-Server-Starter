"""NASCAR is armed on the SERVER, not on a session: armed from the panel in practice or qualifying it runs nothing there but stays armed (race-state `nascar_armed`, saved with the server) through
practice, qualifying and restarts, and the engine starts only when a RACE does, until the session is stopped (the director's stop, the end of an event, the idle stop). The pace car is assigned
whether NASCAR is armed or not."""
import asyncio
import threading

import pytest
from sqlmodel import Session

from app import supervisor
from app.db import engine
from app.live import racecontrol as live_rc
from app.models import Server
from app.services import server_service
from test_racecontrol import api, live, V   # noqa: F401  (the fixture)

PRACTICE, QUALY, RACE = 1, 2, 3
STEAM = "76561190000000001"


@pytest.fixture
def server(live, monkeypatch):
    from app import timeline
    from app.stewards import engine as stewards_engine
    sid, client, sent = live
    monkeypatch.setattr(client, "_record", lambda e: None)
    monkeypatch.setattr(timeline, "on_session_event", lambda *a: None)
    monkeypatch.setattr(stewards_engine, "on_event", lambda *a: None)
    supervisor._instances[sid].nascar_config = None
    return sid, client


def session(client, kind, index):
    client._apply({"type": "new_session", "session_index": index, "session_type": kind})


def stored(sid):
    with Session(engine) as s:
        return (s.get(Server, sid).session or {}).get("nascar")


def state(sid):
    return api.get(f"{V}/servers/{sid}/race-state").json()


def arm(sid, **body):
    return api.post(f"{V}/servers/{sid}/race-control/nascar", json=body)


def test_armed_in_practice_runs_nothing_there_and_stays_armed(server):
    sid, client = server
    session(client, PRACTICE, 0)
    r = arm(sid, total=100)
    assert r.status_code == 200 and r.json()["nascar"] is None, "a NASCAR engine started in practice"
    assert r.json()["nascar_armed"] == {"total": 100, "shares": [30, 45, 25]}
    s = state(sid)
    assert s["nascar"] is None and s["nascar_armed"] == {"total": 100, "shares": [30, 45, 25]}
    assert stored(sid) == {"total": 100, "shares": [30, 45, 25]} and supervisor._instances[sid].nascar_config == stored(sid)
    rc = live_rc.state_of(sid, True, client.session)
    assert rc is None or rc.nascar is None, "the engine exists in a practice session"


def test_it_stays_armed_through_practice_qualifying_and_into_the_race_and_its_restarts(server):
    sid, client = server
    session(client, PRACTICE, 0)
    assert arm(sid, total=100, shares=[20, 50, 30]).status_code == 200
    session(client, QUALY, 1)                                                     # qualifying: still armed, nothing runs
    s = state(sid)
    assert s["nascar"] is None and s["nascar_armed"] == {"total": 100, "shares": [20, 50, 30]}
    session(client, PRACTICE, 2)                                                  # back to practice (a restart of the weekend): the same
    assert state(sid)["nascar_armed"]["total"] == 100 and state(sid)["nascar"] is None
    session(client, RACE, 3)                                                      # the race: the engine starts by itself
    s = state(sid)
    assert s["nascar"]["total"] == 100 and s["nascar"]["stages"] == 3 and s["nascar_armed"] == {"total": 100, "shares": [20, 50, 30]}, s["nascar"]
    session(client, RACE, 4)                                                      # the race is restarted: a fresh engine, still armed
    s = state(sid)
    assert s["nascar"]["total"] == 100 and s["nascar"]["laps"] == 0 and s["nascar_armed"]["total"] == 100
    session(client, QUALY, 5)                                                     # a non-race session after the race: engine gone, arming kept
    s = state(sid)
    assert s["nascar"] is None and s["nascar_armed"]["total"] == 100


def test_a_distance_change_keeps_the_armed_shares_and_disarming_forgets_it(server):
    sid, client = server
    session(client, PRACTICE, 0)
    arm(sid, total=70, shares=[20, 50, 30])
    r = api.patch(f"{V}/servers/{sid}/race-control/nascar", json={"total": 120})
    assert r.json()["nascar_armed"] == {"total": 120, "shares": [20, 50, 30]} and stored(sid) == {"total": 120, "shares": [20, 50, 30]}
    r = arm(sid, total=0)
    assert r.status_code == 200 and r.json()["nascar_armed"] is None and stored(sid) is None and supervisor._instances[sid].nascar_config is None
    session(client, RACE, 1)
    assert state(sid)["nascar"] is None, "a disarmed server armed itself for the race"


def test_what_cannot_be_armed_is_refused_in_practice_as_it_would_be_in_a_race(server):
    sid, client = server
    session(client, PRACTICE, 0)
    assert arm(sid, total=100, stages=[50, 100]).status_code == 422                 # explicit stages are a live-race tool
    assert arm(sid, total=10, shares=[0, 1]).status_code == 422
    assert arm(sid, total=10, shares=[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]).status_code == 422
    assert state(sid)["nascar_armed"] is None and stored(sid) is None, "a refused arming was remembered"


def test_armed_in_a_race_it_starts_now_and_is_remembered_for_the_next_one(server):
    sid, client = server
    session(client, RACE, 0)
    r = arm(sid, total=90)
    assert r.json()["nascar"]["total"] == 90 and r.json()["nascar_armed"]["total"] == 90 and stored(sid)["total"] == 90
    session(client, RACE, 1)
    assert state(sid)["nascar"]["total"] == 90


def test_the_pace_car_is_assigned_with_or_without_nascar(server):
    sid, client = server
    session(client, PRACTICE, 0)
    assert state(sid)["nascar_armed"] is None
    r = api.post(f"{V}/servers/{sid}/race-control/pace-car", json={"steam_id": STEAM, "kmh": 80})
    assert r.status_code == 200, r.text
    s = state(sid)
    assert s["pace_steam_id"] == STEAM and s["nascar"] is None and s["nascar_armed"] is None, "the pace car needed NASCAR"
    session(client, RACE, 1)    # and the director's choice goes on into the race, still with no NASCAR
    s = state(sid)
    assert s["pace_steam_id"] == STEAM and s["nascar"] is None


def test_stopping_the_session_clears_the_arming_whichever_way_it_is_stopped(server, monkeypatch):
    sid, client = server
    inst = supervisor._instances[sid]
    calls = []

    async def fake_stop(server_id, reason="manual", only=None):
        calls.append(reason)
        return True
    monkeypatch.setattr(supervisor, "stop", fake_stop)
    for how in ("director", "event_end"):
        session(client, PRACTICE, 0)
        arm(sid, total=100)
        assert stored(sid) and state(sid)["nascar_armed"]
        if how == "director":
            asyncio.run(server_service.stop(sid))
        else:
            asyncio.run(server_service.stop_instance(sid, inst, "event_end"))
        assert stored(sid) is None, f"the arming survived the stop ({how})"
    assert calls == ["manual", "event_end"]


def test_a_stop_that_stopped_nothing_leaves_the_new_sessions_arming(server, monkeypatch):
    """An automatic stop aimed at an instance that an apply has already replaced stops nothing: the new session keeps what it has."""
    sid, client = server
    inst = supervisor._instances[sid]
    session(client, PRACTICE, 0)
    arm(sid, total=100)

    async def replaced(server_id, reason="manual", only=None):
        return False
    monkeypatch.setattr(supervisor, "stop", replaced)
    asyncio.run(server_service.stop_instance(sid, inst, "event_end"))
    asyncio.run(server_service.stop(sid))
    assert stored(sid) == {"total": 100, "shares": [30, 45, 25]}, "a stop that stopped nothing cleared the arming"


def test_a_newer_session_saved_while_the_stop_waited_keeps_its_arming(server, monkeypatch):
    sid, client = server
    session(client, PRACTICE, 0)
    arm(sid, total=100)

    async def slow_stop(server_id, reason="manual", only=None):
        with Session(engine) as s:   # an apply saves the NEXT session (another arming) while the old process is being stopped
            row = s.get(Server, sid)
            row.session = {**(row.session or {}), "nascar": {"total": 40, "shares": None}}
            s.add(row)
            s.commit()
        return True
    monkeypatch.setattr(supervisor, "stop", slow_stop)
    asyncio.run(server_service.stop(sid))
    assert stored(sid) == {"total": 40, "shares": None}, "the stop wiped the arming of the session that came after"


def test_a_race_that_begins_while_arming_gets_its_engine(server, monkeypatch):
    sid, client = server
    session(client, PRACTICE, 0)
    from app import racecontrol
    real = racecontrol._remember_nascar
    flips = []

    def remember_then_race_starts(sess, server_id, inst, remembered):
        real(sess, server_id, inst, remembered)
        if not flips:
            flips.append(1)
            client.session = {**client.session, "session_type": RACE, "session_index": 1}   # the race began between the check and here
    monkeypatch.setattr(racecontrol, "_remember_nascar", remember_then_race_starts)
    r = arm(sid, total=100)
    assert r.status_code == 200 and r.json()["nascar"] is not None and r.json()["nascar"]["total"] == 100, "the race that began meanwhile was left without its engine"


def test_two_armings_at_once_leave_the_database_and_the_memory_equal(server):
    import threading
    sid, client = server
    session(client, PRACTICE, 0)
    for _ in range(12):
        ts = [threading.Thread(target=lambda t=t: arm(sid, total=t)) for t in (50, 60, 70, 80, 90, 110)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert stored(sid) == supervisor._instances[sid].nascar_config, "the database and the instance disagree about the arming"


class SpyLock:
    def __init__(self):
        self.entered, self._l = 0, threading.Lock()

    def __enter__(self):
        self.entered += 1
        self._l.acquire()
        return self

    def __exit__(self, *a):
        self._l.release()
        return False


def test_arming_and_disarming_take_the_arming_lock(server, monkeypatch):
    from app import racecontrol
    sid, client = server
    spy = SpyLock()
    monkeypatch.setattr(racecontrol, "_arm_lock", spy)
    session(client, PRACTICE, 0)
    arm(sid, total=100)
    assert spy.entered == 1, "two armings can interleave their database and memory writes"
    racecontrol.disarm_when_stopped(sid, {"total": 100, "shares": [30, 45, 25]})
    assert spy.entered == 2


def test_the_idle_stop_and_every_stop_notify_and_an_apply_restart_does_not():
    import inspect
    assert "notify_stopped" in inspect.getsource(supervisor.Instance._idle_watch), "the idle stop does not clear the session's arming"
    assert "notify_stopped" not in inspect.getsource(server_service.apply), "an apply with restart (which writes the new session first) would wipe it"


def test_a_stop_hook_that_fails_never_keeps_a_stop_from_finishing(monkeypatch):
    def boom(server_id, was):
        raise RuntimeError("db down")
    monkeypatch.setattr(supervisor, "stopped_hooks", [boom, lambda sid, was: seen.append((sid, was))])
    seen = []
    supervisor.notify_stopped(5, {"total": 9})
    assert seen == [(5, {"total": 9})]


@pytest.mark.parametrize("stopped,kept", [(True, False), (False, True)])
def test_the_idle_stop_clears_the_arming_only_when_it_stopped_the_instance(server, monkeypatch, stopped, kept):
    from types import SimpleNamespace

    from app.config import settings
    sid, client = server
    session(client, PRACTICE, 0)
    arm(sid, total=100)
    was = supervisor._instances[sid].nascar_config

    async def fake_stop(server_id, reason="manual", only=None):
        assert reason == "idle"
        return stopped
    monkeypatch.setattr(supervisor, "stop", fake_stop)
    monkeypatch.setattr(supervisor, "IDLE_POLL", 0.01)
    monkeypatch.setattr(settings, "idle_stop_seconds", 0.02)
    me = SimpleNamespace(running=True, acsp=SimpleNamespace(cars={}), server_id=sid, nascar_config=was)
    asyncio.run(asyncio.wait_for(supervisor.Instance._idle_watch(me), timeout=5))
    assert (stored(sid) is not None) is kept, ("the idle stop", stopped, stored(sid))
