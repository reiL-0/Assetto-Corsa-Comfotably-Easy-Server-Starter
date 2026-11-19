"""A grid penalty a steward puts on a league RACE carries to the next race of the league (app/league.py sync_grid_carry): it appears on the qualifying before that race and moves the grid."""

import json
import os
import time
from pathlib import Path

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import discord, league
from app.config import settings
from app.db import engine
from app.main import app
from app.models import ChampionshipEvent, Event, Penalty, Schedule
from app.results import apply_penalties, parse_result_file
from app.servers import result_path

api = TestClient(app, headers=ADMIN)
G = [f"7656119800000020{i}" for i in range(5)]


def _file(sid, name, kind, order, age):
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"Type": kind, "TrackName": "spa", "Result": [
        {"DriverName": g[-2:], "DriverGuid": g, "CarModel": "bmw", "BestLap": 90000 + i, "TotalTime": 100000 + i} for i, g in enumerate(order)], "Laps": []}))
    os.utime(d / name, (time.time() - age, time.time() - age))


def _setup(monkeypatch):
    monkeypatch.setattr(discord, "announce", lambda t: None)
    monkeypatch.setattr(discord, "alert", lambda t: None)
    sid = api.post("/api/v1/servers", json={"name": "Carry"}).json()["id"]
    lg = api.post("/api/v1/championships", json={"name": "Liga carry"}).json()
    for i, g in enumerate(G):
        api.post(f"/api/v1/championships/{lg['id']}/members", json={"guid": g, "name": f"P{i}"})
    return sid, lg


def _count(sid, lg, start_age):
    """Count the results of a league event whose schedule began `start_age` seconds ago."""
    with Session(engine) as s:
        ev = Event(title="E", data={"name": "r", "track": "spa", "cars": ["bmw"]}, league_id=lg["id"])
        s.add(ev)
        s.commit()
        sc = Schedule(event_id=ev.id, server_id=sid, start_at=time.time() - start_age, state="running")
        s.add(sc)
        s.commit()
        league.count_results(s, sc, ev, time.time() + 60)


def _penalise(sid, fname, guid, value=2):
    r = api.post(f"/api/v1/servers/{sid}/results/{fname}/penalties", json={"driver_guid": guid, "kind": "grid", "value": value, "reason": "Choque"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _carried(sid, fname):
    with Session(engine) as s:
        return [(p.driver_guid, p.value, p.dismissed) for p in s.exec(select(Penalty).where(Penalty.server_id == sid, Penalty.filename == fname, Penalty.auto_key.like("carry:%")))]


def test_a_grid_penalty_on_a_league_race_reaches_the_qualifying_of_the_next_race_and_moves_the_grid(monkeypatch):
    sid, lg = _setup(monkeypatch)
    _file(sid, "r1.json", "Race", G, 5000)
    _count(sid, lg, 6000)                                    # week 1: the race is counted
    _penalise(sid, "r1.json", G[0], 2)                       # the steward: P1 loses 2 places on the next grid
    _file(sid, "q2.json", "Qualify", G, 100)                 # week 2: the qualifying
    _count(sid, lg, 200)
    assert _carried(sid, "q2.json") == [(G[0], 2, False)]
    with Session(engine) as s:
        pens = list(s.exec(select(Penalty).where(Penalty.filename == "q2.json")))
        assert apply_penalties(parse_result_file(result_path(sid, "q2.json")), pens)["grid"] == [G[1], G[2], G[0], G[3], G[4]]
        _count(sid, lg, 200)                                 # counting again changes nothing (idempotent)
    assert len(_carried(sid, "q2.json")) == 1


def test_the_first_race_of_a_double_header_does_not_carry_only_the_last_one_does(monkeypatch):
    sid, lg = _setup(monkeypatch)
    _file(sid, "r1.json", "Race", G, 5000)
    _file(sid, "r2.json", "Race", G[::-1], 4000)
    _count(sid, lg, 6000)                                    # race 1 and race 2 of one night
    _penalise(sid, "r1.json", G[1], 3)                       # race 1: already passed to race 2 by grid_from_race
    _penalise(sid, "r2.json", G[2], 1)                       # race 2: the last one of the night
    _file(sid, "q3.json", "Qualify", G, 100)
    _count(sid, lg, 200)
    assert _carried(sid, "q3.json") == [(G[2], 1, False)]


def test_a_penalty_put_or_taken_after_the_qualifying_follows_until_the_next_race_is_counted(monkeypatch):
    sid, lg = _setup(monkeypatch)
    _file(sid, "r1.json", "Race", G, 5000)
    _count(sid, lg, 6000)
    _file(sid, "q2.json", "Qualify", G, 100)
    _count(sid, lg, 200)
    assert _carried(sid, "q2.json") == []
    pid = _penalise(sid, "r1.json", G[0], 2)                 # late: the qualifying was counted already, the race has not
    assert _carried(sid, "q2.json") == [(G[0], 2, False)]
    assert api.delete(f"/api/v1/servers/{sid}/results/r1.json/penalties/{pid}").status_code == 204
    assert _carried(sid, "q2.json") == []                    # the original is gone: so is what it carried
    _penalise(sid, "r1.json", G[0], 2)
    _file(sid, "r2.json", "Race", G, 50)
    _count(sid, lg, 200)                                     # the next race is counted: over
    _penalise(sid, "r1.json", G[3], 4)
    assert _carried(sid, "q2.json") == [(G[0], 2, False)]    # nothing moves any more, and the late one went nowhere
    with Session(engine) as s:
        pid = s.exec(select(Penalty).where(Penalty.server_id == sid, Penalty.filename == "r1.json", Penalty.driver_guid == G[0])).one().id
    assert api.delete(f"/api/v1/servers/{sid}/results/r1.json/penalties/{pid}").status_code == 204
    assert _carried(sid, "q2.json") == [], "deleting the original takes its copy away even after the next race was counted"


def test_a_steward_who_withdraws_the_carried_penalty_is_not_overruled_and_an_absent_driver_keeps_nothing(monkeypatch):
    sid, lg = _setup(monkeypatch)
    _file(sid, "r1.json", "Race", G, 5000)
    _count(sid, lg, 6000)
    _penalise(sid, "r1.json", G[0], 2)
    _penalise(sid, "r1.json", G[4], 2)
    _file(sid, "q2.json", "Qualify", G[:4], 100)             # G[4] did not qualify: his places lapse
    _count(sid, lg, 200)
    assert _carried(sid, "q2.json") == [(G[0], 2, False)]
    with Session(engine) as s:
        cid = s.exec(select(Penalty).where(Penalty.server_id == sid, Penalty.filename == "q2.json", Penalty.auto_key.like("carry:%"))).one().id
    assert api.delete(f"/api/v1/servers/{sid}/results/q2.json/penalties/{cid}").status_code == 204   # withdrawn: the row stays, dismissed
    _count(sid, lg, 200)
    assert _carried(sid, "q2.json") == [(G[0], 2, True)]


def test_a_knockout_carries_it_once_not_once_per_stage(monkeypatch):
    sid, lg = _setup(monkeypatch)
    _file(sid, "r1.json", "Race", G, 5000)
    _count(sid, lg, 6000)
    _penalise(sid, "r1.json", G[0], 2)
    for k, name in enumerate(("kq1.json", "kq2.json", "kq3.json")):
        _file(sid, name, "Qualify", G[: 5 - k], 300 - 10 * k)
    _count(sid, lg, 400)
    assert sum(len(_carried(sid, n)) for n in ("kq1.json", "kq2.json", "kq3.json")) == 1
