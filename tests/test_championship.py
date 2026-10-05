import json
from pathlib import Path

from conftest import ADMIN
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app, headers=ADMIN)


def _write_race_result(server_id: int, filename: str, results: list[dict]) -> None:
    d = Path(settings.data_dir) / "instances" / str(server_id) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(json.dumps({"Type": "Race", "TrackName": "spa", "Result": results, "Laps": []}))


def test_championship_standings_across_two_races():
    sid = client.post("/api/v1/servers", json={"name": "ChampServer"}).json()["id"]

    _write_race_result(
        sid,
        "race1.json",
        [
            {"DriverName": "Alice", "DriverGuid": "1", "TotalTime": 600000},
            {"DriverName": "Bob", "DriverGuid": "2", "TotalTime": 605000},
        ],
    )
    _write_race_result(
        sid,
        "race2.json",
        [
            {"DriverName": "Bob", "DriverGuid": "2", "TotalTime": 600000},
            {"DriverName": "Alice", "DriverGuid": "1", "TotalTime": 605000},
        ],
    )

    champ = client.post("/api/v1/championships", json={"name": "OPRL GT3 Cup"}).json()
    cid = champ["id"]
    assert champ["points_system"][0] == 25

    for fname in ("race1.json", "race2.json"):
        r = client.post(f"/api/v1/championships/{cid}/events", json={"server_id": sid, "filename": fname})
        assert r.status_code == 201, r.text

    assert len(client.get(f"/api/v1/championships/{cid}/events").json()) == 2

    standings = client.get(f"/api/v1/championships/{cid}/standings").json()
    assert standings[0]["driver_guid"] in ("1", "2")
    assert standings[0]["points"] == 43  # 25 + 18, tied on points but ranked by wins
    assert standings[0]["wins"] == 1
    assert standings[1]["points"] == 43
    assert {s["driver_guid"] for s in standings} == {"1", "2"}

    assert client.delete(f"/api/v1/championships/{cid}").status_code == 204
    assert client.get(f"/api/v1/championships/{cid}").status_code == 404


def test_add_event_requires_existing_result_file():
    sid = client.post("/api/v1/servers", json={"name": "NoResults"}).json()["id"]
    cid = client.post("/api/v1/championships", json={"name": "Empty Cup"}).json()["id"]
    r = client.post(f"/api/v1/championships/{cid}/events", json={"server_id": sid, "filename": "missing.json"})
    assert r.status_code == 404


def test_edit_points_system_and_remove_a_counted_race():
    sid = client.post("/api/v1/servers", json={"name": "ChampEdit"}).json()["id"]
    _write_race_result(sid, "r.json", [{"DriverName": "Al", "DriverGuid": "7", "TotalTime": 1}])
    cid = client.post("/api/v1/championships", json={"name": "C"}).json()["id"]
    ev = client.post(f"/api/v1/championships/{cid}/events", json={"server_id": sid, "filename": "r.json"}).json()

    r = client.patch(f"/api/v1/championships/{cid}", json={"name": "C2", "points_system": [50, 30]})
    assert r.status_code == 200 and r.json()["name"] == "C2"
    assert client.get(f"/api/v1/championships/{cid}/standings").json()[0]["points"] == 50   # new table applies on read

    assert client.delete(f"/api/v1/championships/{cid}/events/{ev['id']}").status_code == 204
    assert client.get(f"/api/v1/championships/{cid}/standings").json() == []
    assert client.delete(f"/api/v1/championships/{cid}/events/{ev['id']}").status_code == 404
    assert client.patch("/api/v1/championships/9999", json={"name": "x"}).status_code == 404



def test_league_roster_practice_requirement_and_locked_entry_list(monkeypatch):
    import time
    from sqlmodel import Session
    from app import discord
    from app.db import engine
    from app.league import session_for
    from app.models import Activity, Event
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from app.main import app
    api = TestClient(app, headers=ADMIN)
    A, B = "76561198000000001", "76561198000000002"
    lg = api.post("/api/v1/championships", json={"name": "Liga GT", "practice_required": True, "practice_laps": 2, "practice_days": 7,
                                                 "penalties": [{"name": "Contacto", "seconds": 5}, {"name": "Falta grave", "dsq": True}]}).json()
    assert api.patch(f"/api/v1/championships/{lg['id']}", json={"name": "Liga GT", "points_system": [10, 5]}).json()["practice_laps"] == 2   # rules not sent are kept
    for g, n in ((A, "Ana"), (B, "Beto")):
        assert api.post(f"/api/v1/championships/{lg['id']}/members", json={"guid": g, "name": n, "car": "bmw"}).status_code == 200
    now = time.time()
    with Session(engine) as s:   # Ana: 2 valid laps + 1 cut; Beto: 1 valid + 3 that do not count (cut, old, unknown)
        for g, cuts, age in ((A, 0, 60), (A, 0, 120), (A, 2, 30), (B, 0, 90), (B, 1, 90), (B, 0, 9 * 86400), (B, None, 90)):
            s.add(Activity(ts=now - age, server_id=1, kind="lap", guid=g, value=90000, cuts=cuts))
        ev = Event(title="Ronda 1", data={"name": "r1", "track": "spa", "cars": ["bmw", "audi"]}, league_id=lg["id"])
        s.add(ev)
        s.commit()
        said = []
        monkeypatch.setattr(discord, "alert", said.append)
        out = session_for(s, ev, now)
        assert [e.guid for e in out.entries] == [A] and out.locked and not out.pickup and out.entries[0].model == "bmw"
        assert said and "Beto (1/2)" in said[0]
        api.post(f"/api/v1/championships/{lg['id']}/members", json={"guid": B, "name": "Beto", "exempt": True})
        assert sorted(e.guid for e in session_for(s, ev, now).entries) == [A, B]   # an exempt driver is let in
        api.patch(f"/api/v1/championships/{lg['id']}", json={"name": "Liga GT", "practice_required": False})
        api.post(f"/api/v1/championships/{lg['id']}/members", json={"guid": B, "name": "Beto"})
        assert len(session_for(s, ev, now).entries) == 2                           # the checkbox off: everybody


def test_a_league_events_race_result_is_counted_by_its_schedule(monkeypatch):
    import json, os, time
    from pathlib import Path
    from sqlmodel import Session, select
    from app import league
    from app.config import settings
    from app.db import engine
    from app.models import ChampionshipEvent, Event, Schedule
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from app.main import app
    api = TestClient(app, headers=ADMIN)
    sid = api.post("/api/v1/servers", json={"name": "Cuenta"}).json()["id"]
    lg = api.post("/api/v1/championships", json={"name": "Liga cuenta"}).json()
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for name, kind, age in (("race.json", "Race", 60), ("quali.json", "Qualify", 60), ("old.json", "Race", 7200)):
        (d / name).write_text(json.dumps({"Type": kind, "TrackName": "spa", "Result": [], "Laps": []}))
        os.utime(d / name, (now - age, now - age))
    with Session(engine) as s:
        ev = Event(title="R1", data={"name": "r", "track": "spa", "cars": ["a"]}, league_id=lg["id"])
        s.add(ev)
        s.commit()
        sc = Schedule(event_id=ev.id, server_id=sid, start_at=now - 1800, state="running")
        s.add(sc)
        s.commit()
        assert league.count_results(s, sc, ev, now + 60) == 1 and league.count_results(s, sc, ev, now + 60) == 0   # only the race inside the window, once
        rows = {r.filename: r for r in s.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == lg["id"])).all()}
        assert set(rows) == {"race.json", "quali.json"} and rows["race.json"].event_id == ev.id   # the old race is outside the window
        assert (rows["race.json"].session_type, rows["quali.json"].session_type) == ("Race", "Qualify")


def _qualy_result(sid, name, order):
    """A qualifying result with `order` (Steam IDs, best first); written now."""
    import json
    from pathlib import Path
    from app.config import settings
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"Type": "Qualify", "TrackName": "spa", "Result": [
        {"DriverName": g[-2:], "DriverGuid": g, "CarModel": "bmw", "BestLap": 90000 + i, "TotalTime": 0} for i, g in enumerate(order)], "Laps": []}))


def test_league_suspensions_kinds_grid_places_and_non_racing_cars(monkeypatch):
    import time
    from sqlmodel import Session, select
    from app import discord, league
    from app.db import engine
    from app.models import Event, LeagueSuspension, Penalty, Schedule
    from app.results import apply_penalties, parse_result_file
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(discord, "announce", lambda t: None)
    monkeypatch.setattr(discord, "alert", lambda t: None)
    api = TestClient(app, headers=ADMIN)
    G = [f"7656119800000010{i}" for i in range(6)]   # 0 = safety car, 1..5 drivers
    lg = api.post("/api/v1/championships", json={"name": "Liga susp"}).json()
    base = f"/api/v1/championships/{lg['id']}"
    for i, g in enumerate(G):
        assert api.post(base + "/members", json={"guid": g, "name": f"P{i}", "non_racing": i == 0}).status_code == 200
    sus = base + "/suspensions"
    assert api.post(sus, json={"guid": G[0], "kind": "ban", "reason": "no corre"}).status_code == 422          # a non-racing car is never suspended
    assert api.post(sus, json={"guid": G[1], "kind": "time", "reason": "falta días"}).status_code == 422      # needs its amount
    assert api.post(sus, json={"guid": G[1], "kind": "time", "days": 2, "reason": "Choque grave"}).status_code == 201
    assert api.post(sus, json={"guid": G[2], "kind": "races", "races": 2, "reason": "Reincidente"}).status_code == 201
    assert api.post(sus, json={"guid": G[3], "kind": "ban", "reason": "Conducta"}).status_code == 201
    assert api.post(sus, json={"guid": G[4], "kind": "grid", "places": 20, "reason": "Frenó en pista"}).status_code == 201
    # entry list: the three suspended out, the safety car (non racing) and the one with only a grid penalty in
    ev_row = Event(title="R1", data={"name": "r", "track": "spa", "cars": ["bmw"]}, league_id=lg["id"])
    sid = api.post("/api/v1/servers", json={"name": "Susp"}).json()["id"]
    with Session(engine) as s:
        s.add(ev_row)
        s.commit()
        assert sorted(e.guid for e in league.session_for(s, ev_row, time.time()).entries) == sorted([G[0], G[4], G[5]])
        # qualifying: safety car P1, then G[1], G[4] (20 places to lose), G[5] last of the racers
        _qualy_result(sid, "q1.json", [G[0], G[1], G[4], G[5]])
        sc = Schedule(event_id=ev_row.id, server_id=sid, start_at=time.time() - 600, state="running")
        s.add(sc)
        s.commit()
        league.count_results(s, sc, ev_row, time.time() + 60)
        pen = s.exec(select(Penalty).where(Penalty.filename == "q1.json")).all()
        assert [(p.driver_guid, p.kind, p.value) for p in pen] == [(G[4], "grid", 1)]   # 3 racers: only 1 place behind him; the safety car does not count
        x = s.exec(select(LeagueSuspension).where(LeagueSuspension.guid == G[4])).one()
        assert x.places_left == 19 and x.active                                          # the other 19 wait for the next qualifying
        parsed = apply_penalties(parse_result_file(__import__("app.servers", fromlist=["result_path"]).result_path(sid, "q1.json")), pen, frozenset([G[0]]))
        assert parsed["grid"] == [G[1], G[5], G[4]]                                      # without the safety car, G[4] last
        # a counted race uses up the suspensions in races: 2 -> 1
        import json, os
        from pathlib import Path
        from app.config import settings
        rf = Path(settings.data_dir) / "instances" / str(sid) / "results" / "r1.json"
        rf.write_text(json.dumps({"Type": "Race", "TrackName": "spa", "Result": [], "Laps": []}))
        league.count_results(s, sc, ev_row, time.time() + 60)
        assert s.exec(select(LeagueSuspension).where(LeagueSuspension.guid == G[2])).one().races_left == 1
    # qualifying ban: only while the league event is running and the driver has an active qualy suspension
    assert not league.qualy_banned(sid, G[5])
    assert api.post(sus, json={"guid": G[5], "kind": "qualy", "races": 1, "reason": "Antideportivo"}).status_code == 201
    assert league.qualy_banned(sid, G[5]) and not league.qualy_banned(sid, G[1])
    listed = api.get(sus).json()
    assert all(r["active"] for r in listed[:5]) and api.delete(f"{sus}/{listed[0]['id']}").status_code == 204
    assert not league.qualy_banned(sid, G[5])
