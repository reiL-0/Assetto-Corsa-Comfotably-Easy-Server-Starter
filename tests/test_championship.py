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


def test_counting_a_race_makes_cut_notices_for_a_league_with_rules():
    import json, os, time
    from pathlib import Path
    from sqlmodel import Session, select
    from app import league
    from app.config import settings
    from app.db import engine
    from app.models import Event, Notice, Penalty, Schedule
    from conftest import ADMIN
    from fastapi.testclient import TestClient
    from app.main import app
    api = TestClient(app, headers=ADMIN)
    sid = api.post("/api/v1/servers", json={"name": "Auto"}).json()["id"]
    G = "76561190000000009"
    rules = {"cuts_per_step": 2, "step_seconds": 3, "max_seconds": 30}
    ids = [api.post("/api/v1/championships", json={"name": n, **extra}).json()["id"] for n, extra in (("Con reglas", {"auto_rules": rules}), ("Sin reglas", {}))]
    assert api.get(f"/api/v1/championships/{ids[0]}").json()["auto_rules"] == rules and api.get(f"/api/v1/championships/{ids[1]}").json()["auto_rules"] is None
    assert api.patch(f"/api/v1/championships/{ids[0]}", json={"name": "Con reglas", "auto_rules": {"cuts_per_step": 0, "step_seconds": 3, "max_seconds": 30}}).status_code == 422
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (d / "r.json").write_text(json.dumps({"Type": "Race", "TrackName": "spa", "Result": [{"DriverName": "G", "DriverGuid": G, "CarModel": "x", "BestLap": 1, "TotalTime": 100}],
                                          "Laps": [{"DriverName": "G", "DriverGuid": G, "CarId": 0, "LapTime": 90000, "Cuts": 3}]}))
    os.utime(d / "r.json", (now - 60, now - 60))
    got = []
    for lid in ids:
        with Session(engine) as s:
            ev = Event(title="R", data={"name": "r", "track": "spa", "cars": ["a"]}, league_id=lid)
            s.add(ev)
            s.commit()
            sc = Schedule(event_id=ev.id, server_id=sid, start_at=now - 1800, state="running")
            s.add(sc)
            s.commit()
            league.count_results(s, sc, ev, now + 60)
            got.append([(n.kind, n.penalty_kind, n.value) for n in s.exec(select(Notice).where(Notice.server_id == sid))])
            assert not s.exec(select(Penalty).where(Penalty.server_id == sid)).all()   # a warning, never a penalty by itself
    assert got[0] == [("cuts", "time", 3000)]   # 3 cuts = one full step of 2
    assert got[1] == got[0]   # the second league counts the same file: the warning belongs to the file and is not doubled


def test_nascar_scoring_adds_stage_points_to_the_race_points_and_counts_stage_wins():
    G1, G2, G3, G9 = ("7656119000000000" + n for n in "1239")
    sid = client.post("/api/v1/servers", json={"name": "NascarServer"}).json()["id"]
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    cars = [("76561190000000001", "Ann"), ("76561190000000002", "Bob"), ("76561190000000003", "Cat"), ("76561190000000009", "Dir")]
    (d / "2026_10_30_1_00_RACE.json").write_text(json.dumps({"Type": "RACE", "TrackName": "t", "Laps": [], "Result": [
        {"DriverName": n, "DriverGuid": g, "TotalTime": 600000 + i * 1000} for i, (g, n) in enumerate(cars)]}))
    # Bob led stage 1, Ann stage 2; the director (9, non-racing) is in the recorded order but takes no place
    (d / "2026_10_30_1_00_RACE.stages").write_text(json.dumps({"stages": [{"stage": 1, "order": ["76561190000000002", "76561190000000009", "76561190000000001", "76561190000000003"]}, {"stage": 2, "order": ["76561190000000001", "76561190000000002", "76561190000000003"]}]}))
    cid = client.post("/api/v1/championships", json={"name": "Cup"}).json()["id"]
    assert client.put(f"/api/v1/championships/{cid}/rulebook", json={"scoring": {"table": [40, 35, 34, 33], "stage_points": [10, 9, 8]}}).status_code == 200
    assert client.post(f"/api/v1/championships/{cid}/members", json={"guid": "76561190000000009", "name": "Dir", "non_racing": True}).status_code in (200, 201)
    assert client.post(f"/api/v1/championships/{cid}/events", json={"server_id": sid, "filename": "2026_10_30_1_00_RACE.json"}).status_code == 201
    st = {s["driver_guid"]: s for s in client.get(f"/api/v1/championships/{cid}/standings").json()}
    # Ann: P1 40 + stage 1 (second racing car: 9) + stage 2 (first: 10) = 59; Bob: P2 35 + 10 + 9 = 54; Cat: P3 34 + 8 + 8 = 50
    assert (st["76561190000000001"]["points"], st["76561190000000002"]["points"], st["76561190000000003"]["points"]) == (59, 54, 50)
    assert (st["76561190000000001"]["stage_points"], st["76561190000000001"]["stage_wins"], st["76561190000000002"]["stage_wins"]) == (19, 1, 1)
    assert "76561190000000009" not in st, "the director does not score"
    race = client.get(f"/api/v1/championships/{cid}/races").json()[0]
    ann = next(r for r in race["classification"] if r["driver_guid"] == "76561190000000001")
    assert ann["points"] == 40 and ann["stage_points"] == 19 and ann["total_points"] == 59 and [(x["stage"], x["place"]) for x in ann["stages"]] == [(1, 2), (2, 1)]
    # a league without stage points scores the same file as before
    cid2 = client.post("/api/v1/championships", json={"name": "Plain"}).json()["id"]
    client.post(f"/api/v1/championships/{cid2}/events", json={"server_id": sid, "filename": "2026_10_30_1_00_RACE.json"})
    plain = {s["driver_guid"]: s for s in client.get(f"/api/v1/championships/{cid2}/standings").json()}
    assert plain["76561190000000001"]["points"] == 25 and plain["76561190000000001"]["stage_points"] == 0


def test_the_stage_order_of_a_race_without_its_own_file_comes_from_the_journal_written_before_it(tmp_path):
    from app.results import STAGES_PENDING, read_stages
    race = tmp_path / "2026_10_30_2_00_RACE.json"
    race.write_text("{}")
    (tmp_path / STAGES_PENDING).write_text(json.dumps({"at": race.stat().st_mtime - 5, "stages": [{"stage": 1, "order": ["1", "2"]}]}))
    assert read_stages(race) == [{"stage": 1, "order": ["1", "2"]}]
    later = tmp_path / "2026_10_30_3_00_RACE.json"   # another race written after the first one took the journal: not its own
    later.write_text("{}")
    import os
    os.utime(later, (race.stat().st_mtime + 60, race.stat().st_mtime + 60))
    assert read_stages(later) == []
    assert read_stages(tmp_path / "2026_10_30_9_00_RACE.json") == []


def test_a_journal_whose_race_never_heard_the_session_end_is_given_to_that_race_before_it_is_overwritten_and_never_to_the_next_one(tmp_path):
    import os
    from app.results import STAGES_PENDING, STAGES_SUFFIX, claim_orphan, pending_is_unclaimed, read_stages
    first = tmp_path / "2026_10_30_2_00_RACE.json"
    first.write_text("{}")
    at = first.stat().st_mtime - 5
    (tmp_path / STAGES_PENDING).write_text(json.dumps({"at": at, "stages": [{"stage": 1, "order": ["1"]}]}))
    assert pending_is_unclaimed(tmp_path, first.name), "the race that wrote it may claim it"
    second = tmp_path / "2026_10_30_4_00_RACE.json"   # the next race: the first one's result already exists after the journal
    second.write_text("{}")
    os.utime(second, (at + 7200, at + 7200))
    assert not pending_is_unclaimed(tmp_path, second.name), "a journal older than another race result is not this race's"
    claim_orphan(tmp_path)   # the next race is about to write its own journal
    assert not (tmp_path / STAGES_PENDING).exists() and read_stages(first) == [{"stage": 1, "order": ["1"]}]
    assert (first.with_suffix(STAGES_SUFFIX)).is_file() and read_stages(second) == []


def test_a_late_session_end_of_one_race_does_not_take_the_journal_of_the_next_race(tmp_path):
    import os
    from app.results import STAGES_PENDING, pending_is_unclaimed
    a = tmp_path / "2026_10_30_2_00_RACE.json"
    a.write_text("{}")
    (tmp_path / STAGES_PENDING).write_text(json.dumps({"at": a.stat().st_mtime + 3600, "stages": []}))   # B's journal, written an hour after A's result
    assert not pending_is_unclaimed(tmp_path, a.name)
