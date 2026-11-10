"""The advantage gained off the track, end to end in the manager: a race of a league (its rule is on by default, 5 s), the cars' positions, the excursion the game of one reports, the banner state, the penalty that
falls due and becomes a time penalty on the result of the race; forgive and sanction-now by the director."""
import json
import os
import time
from pathlib import Path

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import league, racecontrol as adapter, supervisor
from app.config import settings
from app.db import engine
from app.live import racecontrol as live_rc
from app.main import app
from app.models import Event, GiveBackDue, Penalty, Schedule
from test_racecontrol import live, V  # noqa: F401  (the `live` fixture)

api = TestClient(app, headers=ADMIN)
A_GUID, B_GUID = "76561190000000004", "76561190000000003"
RULE = {"seconds": 5, "window_s": 30}


def b_line(t):
    return 5.20 + 0.004 * (t - 1000)


def a_goes_by(t):   # 0.02 behind car 3, off the track from 1005 to 1007: ends 0.01 in front
    return b_line(t) - 0.02 + 0.03 * min(1.0, max(0.0, (t - 1005.0) / 2.0))


def _race(sid, client, monkeypatch):
    supervisor._instances[sid] = type("I", (), {"running": True, "server_id": sid, "acsp": client})()   # (the fixture's instance has no server id: the monitor reads it)
    monkeypatch.setattr(live_rc, "give_back_rule_peek", lambda sid: RULE)
    monkeypatch.setattr(adapter, "give_back_rule_peek", lambda sid: RULE)
    client.session = {"session_index": 2, "session_type": 3}
    client.cars[3] = {"driver_name": "Ana", "driver_guid": B_GUID, "velocity": [0.0, 0.0, 33.0]}    # 119 km/h
    client.cars[4] = {"driver_name": "Beto", "driver_guid": A_GUID, "velocity": [0.0, 0.0, 33.0]}


def _drive(sid, client, t0, t1, a=a_goes_by, report_at=None, saved=None):
    t = t0
    reported = False
    while t <= t1 + 1e-9:
        for car, fn in ((3, b_line), (4, a)):
            p = fn(t)
            client.field.update(car, p - 5.0 if p < 6 else p - 6.0, 5 if p < 6 else 6)
        if report_at is not None and not reported and t >= report_at:
            live_rc.on_excursion(sid, client.session, 4, 2000, 5, a(1005.0) - 5.0, now=t)
            reported = True
        rows = adapter.give_back_tick(t)
        if saved is not None:
            saved += rows
        t = round(t + 0.2, 6)


def test_a_driver_who_goes_off_the_track_and_keeps_the_place_gets_a_time_penalty_in_the_result(live, monkeypatch):
    sid, client, _ = live
    _race(sid, client, monkeypatch)
    rows = []
    _drive(sid, client, 1000.0, 1009.4, report_at=1007.5, saved=rows)
    obs = api.get(f"{V}/servers/{sid}/race-state").json()["obligations"]
    assert len(obs) == 1 and obs[0]["car"] == 4 and obs[0]["creditors"] == [3] and obs[0]["names"] == "Ana" and 0 <= obs[0]["left_s"] <= 30, "the banner of car 4: give the place back to car 3 (the state counts with the real clock, the test with its own: only the shape is checked)"
    _drive(sid, client, 1009.6, 1040.0, saved=rows)
    assert len(rows) == 1 and rows[0]["driver_guid"] == A_GUID and rows[0]["seconds"] == 5 and rows[0]["creditors"] == "Ana", "time is up, still in front: one penalty"
    texts = [n["text"] for n in api.get(f"{V}/servers/{sid}/race-state").json()["notices"]]
    assert "Beto: no devolvió la posición · +5 s" in texts and "PENALIZACIÓN +5 s: ventaja fuera de pista" in texts
    adapter.save_dues(rows)
    # the race ends: counting its result turns the due into a time penalty, once
    lid = api.post(f"{V}/championships", json={"name": f"GB{time.time_ns()}"}).json()["id"]
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (d / "gb.json").write_text(json.dumps({"Type": "Race", "TrackName": "spa", "Result": [{"DriverName": "Beto", "DriverGuid": A_GUID, "CarModel": "x", "BestLap": 1, "TotalTime": 100},
                                                                                         {"DriverName": "Ana", "DriverGuid": B_GUID, "CarModel": "x", "BestLap": 1, "TotalTime": 99}], "Laps": []}))
    os.utime(d / "gb.json", (now + 30, now + 30))
    with Session(engine) as s:
        ev = Event(title="R", data={"name": "r", "track": "spa", "cars": ["a"]}, league_id=lid)
        s.add(ev)
        s.commit()
        sc = Schedule(event_id=ev.id, server_id=sid, start_at=now - 3600, state="running")
        s.add(sc)
        s.commit()
        for due in s.exec(select(GiveBackDue)).all():
            due.ts = now   # (the simulated clock of the test is not the file's)
            s.add(due)
        s.commit()
        league.count_results(s, sc, ev, now + 60)
        pen = s.exec(select(Penalty).where(Penalty.server_id == sid, Penalty.filename == "gb.json")).all()
        assert [(p.driver_guid, p.kind, p.value, p.created_by) for p in pen] == [(A_GUID, "time", 5000, "auto")] and "Ana" in pen[0].reason and pen[0].auto_key.startswith("giveback:")
        assert [x.state for x in s.exec(select(GiveBackDue).where(GiveBackDue.server_id == sid)).all()] == ["applied"]
        league.count_results(s, sc, ev, now + 60)
        assert len(s.exec(select(Penalty).where(Penalty.server_id == sid, Penalty.filename == "gb.json")).all()) == 1, "counting again never doubles it"


def test_giving_the_place_back_says_so_and_costs_nothing(live, monkeypatch):
    sid, client, _ = live
    _race(sid, client, monkeypatch)
    rows = []
    _drive(sid, client, 1000.0, 1009.4, report_at=1007.5, saved=rows)
    _drive(sid, client, 1009.6, 1030.0, a=lambda t: a_goes_by(t) - (0.022 if t >= 1012 else 0.0), saved=rows)
    assert rows == [] and api.get(f"{V}/servers/{sid}/race-state").json()["obligations"] == []
    assert "POSICIÓN DEVUELTA ✓" in [n["text"] for n in api.get(f"{V}/servers/{sid}/race-state").json()["notices"]]


def test_the_director_can_forgive_or_sanction_an_open_obligation(live, monkeypatch):
    sid, client, _ = live
    _race(sid, client, monkeypatch)
    _drive(sid, client, 1000.0, 1009.4, report_at=1007.5)
    ob = api.get(f"{V}/servers/{sid}/race-state").json()["obligations"][0]["id"]
    assert api.post(f"{V}/servers/{sid}/race-control/give-back/{ob}/forgive").status_code == 204
    assert api.get(f"{V}/servers/{sid}/race-state").json()["obligations"] == [] and api.post(f"{V}/servers/{sid}/race-control/give-back/{ob}/forgive").status_code == 404
    client2_rows = []
    client.field.clear()
    live_rc._states.pop(sid, None)
    _race(sid, client, monkeypatch)
    _drive(sid, client, 1000.0, 1009.4, report_at=1007.5, saved=client2_rows)
    ob = api.get(f"{V}/servers/{sid}/race-state").json()["obligations"][0]["id"]
    before = len(api.get(f"{V}/servers/{sid}/race-state").json()["notices"])
    assert api.post(f"{V}/servers/{sid}/race-control/give-back/{ob}/sanction").status_code == 204
    with Session(engine) as s:
        due = s.exec(select(GiveBackDue).where(GiveBackDue.server_id == sid, GiveBackDue.state == "due")).all()
    assert [(d.driver_guid, d.seconds, d.created_by) for d in due][-1][:2] == (A_GUID, 5)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["obligations"] == [] and len(api.get(f"{V}/servers/{sid}/race-state").json()["notices"]) > before
    assert api.post(f"{V}/servers/{sid}/race-control/give-back/{ob}/sanction").status_code == 404


def test_no_rule_no_qualifying_no_judge(live, monkeypatch):
    sid, client, _ = live
    _race(sid, client, monkeypatch)
    monkeypatch.setattr(adapter, "give_back_rule_peek", lambda s: None)
    _drive(sid, client, 1000.0, 1015.0, report_at=1007.5)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["obligations"] == [], "a server whose league has the rule off (or no league event): nothing"
    live_rc._states.pop(sid, None)
    monkeypatch.setattr(adapter, "give_back_rule_peek", lambda s: RULE)
    client.session = {"session_index": 1, "session_type": 2}
    _drive(sid, client, 1000.0, 1015.0, report_at=1007.5)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["obligations"] == [], "qualifying: no places to give back"


def test_a_slot_that_changes_hands_owes_nothing_and_a_failed_save_is_tried_again(live, monkeypatch):
    sid, client, _ = live
    _race(sid, client, monkeypatch)
    _drive(sid, client, 1000.0, 1009.4, report_at=1007.5)
    assert len(api.get(f"{V}/servers/{sid}/race-state").json()["obligations"]) == 1
    client.cars[4] = {"driver_name": "Otro", "driver_guid": "76561190000000099", "velocity": [0.0, 0.0, 33.0]}   # another driver takes the slot meanwhile
    _drive(sid, client, 1009.6, 1010.4)
    assert api.get(f"{V}/servers/{sid}/race-state").json()["obligations"] == [], "the new driver owes nothing"
    assert [d for d in adapter._unsaved_dues] == []
    live_rc._states.pop(sid, None)
    client.cars[4] = {"driver_name": "Beto", "driver_guid": A_GUID, "velocity": [0.0, 0.0, 33.0]}
    client.field.clear()
    _race(sid, client, monkeypatch)
    calls = []

    def failing(rows):
        calls.append(len(rows))
        raise RuntimeError("database is locked")
    monkeypatch.setattr(adapter, "save_dues", failing)
    t, saved = 1000.0, []
    while t <= 1041.0:   # the monitor's own steps: tick, keep what is due, try to write it
        for car, fn in ((3, b_line), (4, a_goes_by)):
            p = fn(t)
            client.field.update(car, p - 5.0, 5)
        if abs(t - 1007.6) < 0.1:
            live_rc.on_excursion(sid, client.session, 4, 2000, 5, a_goes_by(1005.0) - 5.0, now=t)
        adapter._unsaved_dues.extend(adapter.give_back_tick(t))
        if adapter._unsaved_dues:
            batch = list(adapter._unsaved_dues)
            try:
                adapter.save_dues(batch)
                del adapter._unsaved_dues[:len(batch)]
            except RuntimeError:
                pass
        t = round(t + 0.2, 6)
    assert len(adapter._unsaved_dues) == 1 and calls, "the write failed: the penalty is kept, not lost"
    monkeypatch.setattr(adapter, "save_dues", lambda rows: saved.extend(rows))
    batch = list(adapter._unsaved_dues)
    adapter.save_dues(batch)
    del adapter._unsaved_dues[:len(batch)]
    assert [r["driver_guid"] for r in saved] == [A_GUID] and adapter._unsaved_dues == []


def test_a_sanction_that_cannot_be_written_leaves_the_obligation_open(live, monkeypatch):
    sid, client, _ = live
    _race(sid, client, monkeypatch)
    _drive(sid, client, 1000.0, 1009.4, report_at=1007.5)
    ob = api.get(f"{V}/servers/{sid}/race-state").json()["obligations"][0]["id"]
    monkeypatch.setattr(adapter, "save_dues", lambda rows: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert api.post(f"{V}/servers/{sid}/race-control/give-back/{ob}/sanction").status_code == 503
    assert [o["id"] for o in api.get(f"{V}/servers/{sid}/race-state").json()["obligations"]] == [ob], "still open: the director can try again"
    client.cars[4].pop("driver_guid")
    monkeypatch.undo()
    _race(sid, client, monkeypatch)
    client.cars[4].pop("driver_guid")
    assert api.post(f"{V}/servers/{sid}/race-control/give-back/{ob}/sanction").status_code in (404, 409), "no Steam ID: no penalty can be written"


def test_the_dues_of_a_race_never_wait_for_the_next_one_and_never_cross_schedules():
    from app.stewards.auto import give_back_penalties
    from app.models import Championship
    sid = api.post(f"{V}/servers", json={"name": "gb-window"}).json()["id"]
    lid = api.post(f"{V}/championships", json={"name": f"W{time.time_ns()}"}).json()["id"]
    G1, G2 = "76561190000000011", "76561190000000012"
    parsed = {"type": "Race", "classification": [{"driver_guid": G1, "driver_name": "Uno"}], "laps": []}
    with Session(engine) as s:
        for guid, ts in ((G1, 500.0), (G1, 1500.0), (G2, 1500.0), (G1, 2500.0)):   # before the schedule, in it, a driver who is not in the result, after the race
            s.add(GiveBackDue(server_id=sid, driver_guid=guid, ts=ts, seconds=5))
        s.commit()
        c = s.get(Championship, lid)
        made = give_back_penalties(s, c, sid, "r1.json", parsed, 2000.0, 1000.0)
        s.commit()
        assert made == 1
        states = {(d.driver_guid, d.ts): d.state for d in s.exec(select(GiveBackDue).where(GiveBackDue.server_id == sid)).all()}
        assert states == {(G1, 500.0): "due", (G1, 1500.0): "applied", (G2, 1500.0): "expired", (G1, 2500.0): "due"}, states
        assert give_back_penalties(s, c, sid, "r2.json", parsed, 3000.0, 2000.0) == 1, "the one after the first race goes to the next, and nothing older does"


def test_the_cached_rule_is_never_an_old_leagues_and_the_monitor_knows_when_to_read_it_again():
    sid = api.post(f"{V}/servers", json={"name": "gb-cache"}).json()["id"]
    league._EFFECTIVE.clear()
    assert league.give_back_rule_cached(sid) is None and league.give_back_rule_stale(sid) is True, "never read"
    league._EFFECTIVE[sid] = (time.time(), {"give_back": {"seconds": 5, "window_s": 30}})
    assert league.give_back_rule_cached(sid) == {"seconds": 5, "window_s": 30} and league.give_back_rule_stale(sid) is False
    league._EFFECTIVE[sid] = (time.time() - league.CUT_RULE_TTL - 1, {"give_back": {"seconds": 5, "window_s": 30}})
    assert league.give_back_rule_stale(sid) is True and league.give_back_rule_cached(sid) is not None, "due to be read again, still usable for a while"
    league._EFFECTIVE[sid] = (time.time() - 3 * league.CUT_RULE_TTL - 1, {"give_back": {"seconds": 5, "window_s": 30}})
    assert league.give_back_rule_cached(sid) is None, "a rule that old is not used: another league's"


def test_a_late_due_never_goes_to_a_later_race():
    from app.stewards.auto import give_back_penalties
    from app.models import Championship
    sid = api.post(f"{V}/servers", json={"name": "gb-late"}).json()["id"]
    lid = api.post(f"{V}/championships", json={"name": f"L{time.time_ns()}"}).json()["id"]
    G1 = "76561190000000021"
    parsed = {"type": "Race", "classification": [{"driver_guid": G1, "driver_name": "Uno"}], "laps": []}
    with Session(engine) as s:
        s.add(GiveBackDue(server_id=sid, driver_guid=G1, ts=1800.0, seconds=5))   # fell due in race 1 (ended at 2000) but was written after race 1 was counted
        s.commit()
        c = s.get(Championship, lid)
        assert give_back_penalties(s, c, sid, "race2.json", parsed, 4000.0, 2000.0) == 0, "race 2 began at 2000: a due of 1800 is race 1's, not its"


def test_a_name_with_a_quote_or_a_backslash_cannot_break_what_the_hub_reads(live, monkeypatch):
    sid, client, _ = live
    _race(sid, client, monkeypatch)
    client.cars[3]["driver_name"] = 'Ana "Rayo" \\ Largo Largo Largo Largo'
    _drive(sid, client, 1000.0, 1009.4, report_at=1007.5)
    obs = api.get(f"{V}/servers/{sid}/race-state").json()["obligations"]
    assert obs[0]["names"] == "Ana 'Rayo' / Largo Largo"[:24] and '"' not in obs[0]["names"] and "\\" not in obs[0]["names"] and len(obs[0]["names"]) <= 24
    raw = api.get(f"{V}/servers/{sid}/race-state").text
    assert '\\"' not in raw.split('"obligations"')[1].split('"rev"')[0], "no escaped quote inside the obligations"
