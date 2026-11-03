"""NASCAR is armed on the SERVER, not on a session: armed from the panel in practice or qualifying it runs nothing there but stays armed (race-state `nascar_armed`, saved with the server) through
practice, qualifying and restarts, and the engine starts only when a RACE does, until the session is stopped (the director's stop, the end of an event, the idle stop). The pace car is assigned
whether NASCAR is armed or not."""
import asyncio

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


def test_the_idle_stop_and_every_stop_notify_and_an_apply_restart_does_not():
    import inspect
    assert "notify_stopped" in inspect.getsource(supervisor.Instance._idle_watch), "the idle stop does not clear the session's arming"
    assert "notify_stopped" not in inspect.getsource(server_service.apply), "an apply with restart (which writes the new session first) would wipe it"


def test_a_stop_hook_that_fails_never_keeps_a_stop_from_finishing(monkeypatch):
    def boom(server_id):
        raise RuntimeError("db down")
    monkeypatch.setattr(supervisor, "stopped_hooks", [boom, lambda sid: seen.append(sid)])
    seen = []
    supervisor.notify_stopped(5)
    assert seen == [5]
