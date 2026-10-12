"""Archiving a league (app/archive.py): the final standings and the per-race tables stay, the fine detail (laps, cuts, result files, penalty rows) goes; the
standings read the same before and after; a second archive and every write to an archived league answer 409; a result another running league counts is left alone;
a file that cannot be deleted is reported, not fatal; two archivers at once: one wins."""
import json
import threading
from pathlib import Path

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import archive
from app.config import settings
from app.db import engine
from app.main import app
from app.models import Championship, ChampionshipArchive, ChampionshipEvent, Penalty

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
A, B, C = "76561190000000011", "76561190000000012", "76561190000000013"


def _result(sid, name, kind="Race", order=(("A", A, 600000), ("B", B, 605000), ("C", C, 610000))):
    """A result with laps and CUTS: exactly what the archive must not keep."""
    laps = [{"DriverName": n, "DriverGuid": g, "CarId": i, "LapTime": 120000 + k, "Cuts": 1 if k == 3 else 0} for i, (n, g, _) in enumerate(order) for k in range(5)]
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"Type": kind, "TrackName": "spa", "TrackConfig": "", "Name": kind,
                                      "Result": [{"DriverName": n, "DriverGuid": g, "CarModel": "bmw", "BestLap": 119000 + i, "TotalTime": t} for i, (n, g, t) in enumerate(order)],
                                      "Laps": laps}))
    return d / name


def _league(name, files, sid):
    cid = api.post(f"{V}/championships", json={"name": name}).json()["id"]
    for f in files:
        assert api.post(f"{V}/championships/{cid}/events", json={"server_id": sid, "filename": f}).status_code == 201
    return cid


def _setup(tag):
    sid = api.post(f"{V}/servers", json={"name": f"arch-{tag}"}).json()["id"]
    paths = [_result(sid, "race1.json"), _result(sid, "race2.json", order=(("B", B, 600000), ("A", A, 603000), ("C", C, 610000))), _result(sid, "qualy1.json", "Qualify")]
    cid = _league(f"Liga {tag}", [p.name for p in paths], sid)
    # a steward's decision with its reason, on race 1: 5 s for contact
    assert api.post(f"{V}/servers/{sid}/results/race1.json/penalties", json={"driver_guid": A, "kind": "time", "value": 8, "reason": "Contacto en la curva 3"}).status_code in (200, 201)
    return sid, cid, paths


def test_archive_keeps_the_tables_and_drops_the_fine_detail():
    sid, cid, paths = _setup("keep")
    before = api.get(f"{V}/championships/{cid}/standings").json()
    pv = api.get(f"{V}/championships/{cid}/archive/preview").json()
    assert pv["races"] == 3 and pv["files"] == 3 and pv["penalty_rows"] == 1 and pv["bytes"] > 0 and pv["standings"] == before
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "otra cosa"}).status_code == 422
    assert all(p.exists() for p in paths), "a wrong confirmation must delete nothing"
    r = api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga keep"})
    assert r.status_code == 200 and r.json()["files_deleted"] == 3 and r.json()["files_not_deleted"] == [], r.text
    assert not any(p.exists() for p in paths), "the result files (every lap, every cut) must be gone"
    # the standings read exactly as before, from the archive
    assert api.get(f"{V}/championships/{cid}/standings").json() == before
    evs = api.get(f"{V}/championships/{cid}/events").json()
    assert sorted(e["filename"] for e in evs) == ["qualy1.json", "race1.json", "race2.json"] and {e["session_type"] for e in evs} <= {"Race", "Qualify"}
    arch = api.get(f"{V}/championships/{cid}/archive").json()
    assert arch["archived_by"] == "root"
    text = json.dumps(arch["data"])
    assert "cuts" not in text.lower() and "lap_time_ms" not in text and "LapTime" not in text, "no cuts and no lap times"
    assert all(isinstance(e["laps"], int) for r in arch["data"]["races"] for e in r["classification"]) and not any("laps" in r for r in arch["data"]["races"]), "only a lap COUNT"
    race1 = next(r for r in arch["data"]["races"] if r["filename"] == "race1.json")
    pen = race1["penalties"]
    assert len(pen) == 1 and (pen[0]["driver_guid"], pen[0]["kind"], pen[0]["reason"], pen[0]["created_by"]) == (A, "time", "Contacto en la curva 3", "root") and pen[0]["value"] > 0, pen
    assert [e["driver_guid"] for e in race1["classification"]] == [B, A, C], "the order after the penalty is what is kept"
    assert race1["classification"][0]["points"] > race1["classification"][1]["points"] and race1["classification"][0]["best_lap_ms"] is not None
    assert all("laps_list" not in e and "cuts" not in e for e in race1["classification"]), "no per-lap data"
    # the penalty rows of those files went with them; the roster / suspensions are not touched
    with Session(engine) as s:
        assert s.exec(select(Penalty).where(Penalty.server_id == sid)).all() == []
        assert s.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == cid)).all() == []
        assert s.get(Championship, cid).archived_at is not None and s.get(ChampionshipArchive, cid) is not None


def test_a_second_archive_and_every_write_to_an_archived_league_are_409():
    sid, cid, paths = _setup("closed")
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga closed"}).status_code == 200
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga closed"}).status_code == 409
    assert api.get(f"{V}/championships/{cid}/archive/preview").status_code == 409
    for method, url, body in (("patch", f"{V}/championships/{cid}", {"name": "x"}), ("delete", f"{V}/championships/{cid}", None),
                              ("post", f"{V}/championships/{cid}/events", {"server_id": sid, "filename": "race1.json"}),
                              ("delete", f"{V}/championships/{cid}/events/-1", None),
                              ("post", f"{V}/championships/{cid}/members", {"guid": A, "name": "A"}), ("delete", f"{V}/championships/{cid}/members/{A}", None),
                              ("post", f"{V}/championships/{cid}/suspensions", {"guid": A, "kind": "ban", "reason": "motivo"}), ("delete", f"{V}/championships/{cid}/suspensions/1", None)):
        r = getattr(api, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 409, (method, url, r.status_code, r.text)
    # no event can be tied to an archived league
    r = api.post(f"{V}/events", json={"title": "e", "session": {"name": "n", "track": "spa", "cars": ["bmw"]}, "league_id": cid})
    assert r.status_code == 409, r.text
    # and a running league still takes changes
    live = api.post(f"{V}/championships", json={"name": "viva"}).json()["id"]
    assert api.patch(f"{V}/championships/{live}", json={"name": "viva 2"}).status_code == 200


def test_a_result_another_running_league_counts_stays():
    sid, cid, paths = _setup("shared")
    other = _league("Otra liga", ["race1.json"], sid)   # counts the same file
    r = api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga shared"})
    assert r.status_code == 200 and r.json()["files_deleted"] == 2, r.text
    assert paths[0].exists() and not paths[1].exists() and not paths[2].exists(), "the shared race file must stay for the league that still runs"
    with Session(engine) as s:
        assert len(s.exec(select(Penalty).where(Penalty.server_id == sid, Penalty.filename == "race1.json")).all()) == 1, "its penalty stays too"
    assert api.get(f"{V}/championships/{other}/standings").json(), "the other league still scores from that file"


def test_a_file_that_cannot_be_deleted_is_reported_not_fatal(monkeypatch):
    sid, cid, paths = _setup("stuck")
    real = Path.unlink

    def stuck(self, missing_ok=False):
        if self.name == "race2.json":
            raise PermissionError("busy")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", stuck)
    r = api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga stuck"})
    assert r.status_code == 200 and r.json()["files_not_deleted"] == [f"{sid}/race2.json"] and r.json()["files_deleted"] == 2, r.text
    assert api.get(f"{V}/championships/{cid}/archive").status_code == 200, "the league is archived all the same"


def test_a_change_while_archiving_makes_it_try_again_and_two_archivers_get_one_winner(monkeypatch):
    sid, cid, paths = _setup("race")
    # a penalty arrives between the snapshot and the claim: the snapshot is stale -> 409 and NOTHING is archived or deleted
    real_snapshot = archive.snapshot

    def snapshot_then_change(sess, c):
        out = real_snapshot(sess, c)
        assert api.post(f"{V}/servers/{sid}/results/race2.json/penalties", json={"driver_guid": B, "kind": "time", "value": 3, "reason": "tarde"}).status_code in (200, 201)
        return out
    monkeypatch.setattr(archive, "snapshot", snapshot_then_change)
    r = api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga race"})
    assert r.status_code == 409 and "changed" in r.text and all(p.exists() for p in paths), r.text
    with Session(engine) as s:
        assert s.get(Championship, cid).archived_at is None
    monkeypatch.setattr(archive, "snapshot", real_snapshot)
    # two archivers at the same moment: exactly one succeeds
    codes, gate = [], threading.Barrier(2)

    def go():
        client = TestClient(app, headers=ADMIN)
        gate.wait()
        codes.append(client.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga race"}).status_code)
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 409], codes


def test_an_archived_league_counts_no_more_results():
    from app.league import count_results
    from app.models import Event, Schedule
    sid, cid, paths = _setup("count")
    with Session(engine) as s:
        ev = Event(title="e", data={"name": "n", "track": "spa", "cars": ["bmw"]}, league_id=cid)
        s.add(ev)
        s.commit()
        sc = Schedule(event_id=ev.id, server_id=sid, start_at=0.0)
        s.add(sc)
        s.commit()
        ev_id, sc_id = ev.id, sc.id
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga count"}).status_code == 200
    _result(sid, "late_race.json")   # a new result of the same server, written after the archive
    with Session(engine) as s:
        assert count_results(s, s.get(Schedule, sc_id), s.get(Event, ev_id), 1e12) == 0
        assert s.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == cid)).all() == [], "an archived league must not collect results again"


def test_the_claim_itself_stops_a_second_archiver_even_if_it_got_past_the_first_look(monkeypatch):
    """Two archivers can both pass the early «already archived?» look; only the conditional UPDATE decides. Simulate that by blinding the early look."""
    sid, cid, paths = _setup("claim")
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga claim"}).status_code == 200
    monkeypatch.setattr(archive, "get_open", lambda sess, championship_id: sess.get(Championship, championship_id))
    r = api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga claim"})
    assert r.status_code == 409, r.text
    with Session(engine) as s:
        assert len(s.exec(select(ChampionshipArchive).where(ChampionshipArchive.championship_id == cid)).all()) == 1


def test_events_of_an_archived_league_cannot_run_and_a_copy_does_not_join_it():
    sid, cid, paths = _setup("events")
    ev = api.post(f"{V}/events", json={"title": "Fecha 1", "session": {"name": "n", "track": "spa", "cars": ["bmw"]}, "league_id": cid})
    assert ev.status_code in (200, 201), ev.text
    eid = ev.json()["id"]
    assert api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga events"}).status_code == 200
    r = api.post(f"{V}/events/{eid}/run", json={"server_id": sid, "restart": False})
    assert r.status_code == 409 and "archived" in r.text, r.text
    copy = api.post(f"{V}/events/{eid}/duplicate")
    assert copy.status_code in (200, 201) and copy.json()["league_id"] is None, "a copy must not be tied to an archived league"


def test_a_league_edit_waits_for_an_archive_in_progress_and_the_results_counter_does_not_block():
    from app import leaguelock
    from app.league import count_results
    from app.models import Event, Schedule
    sid, cid, paths = _setup("lock")
    with Session(engine) as s:
        ev = Event(title="e", data={"name": "n", "track": "spa", "cars": ["bmw"]}, league_id=cid)
        s.add(ev)
        s.commit()
        sc = Schedule(event_id=ev.id, server_id=sid, start_at=0.0)
        s.add(sc)
        s.commit()
        ev_id, sc_id = ev.id, sc.id
    done = []
    with leaguelock.lock:   # an archive (or another edit) holds the league
        t = threading.Thread(target=lambda: done.append(TestClient(app, headers=ADMIN).patch(f"{V}/championships/{cid}", json={"name": "Liga lock"}).status_code))
        t.start()
        t.join(0.5)
        assert t.is_alive() and not done, "a league edit must wait while the league lock is held"
        result = {}
        worker = threading.Thread(target=lambda: result.update(n=count_results(Session(engine), Session(engine).get(Schedule, sc_id), Session(engine).get(Event, ev_id), 1e12)))
        worker.start()   # another thread: the lock is held by this one, so the counter must give up at once instead of waiting
        worker.join(2)
        assert not worker.is_alive() and result["n"] == 0, "the scheduler's counter must not wait for the lock"
    t.join(5)
    assert done == [200]
    # saving an event is serialized with archiving too (it checks the league and then writes)
    body = {"title": "otra", "session": {"name": "n", "track": "spa", "cars": ["bmw"]}}
    finished = []
    with leaguelock.lock:
        t2 = threading.Thread(target=lambda: finished.append(TestClient(app, headers=ADMIN).post(f"{V}/events", json=body).status_code))
        t2.start()
        t2.join(0.5)
        assert t2.is_alive() and not finished, "saving an event must wait while the league lock is held"
    t2.join(5)
    assert finished == [201]


def test_a_file_another_league_takes_up_during_the_archive_is_kept(monkeypatch):
    sid, cid, paths = _setup("takeup")
    other = api.post(f"{V}/championships", json={"name": "Tardía"}).json()["id"]
    real_snapshot = archive.snapshot

    def snapshot_then_other_league_takes_race1(sess, c):
        out = real_snapshot(sess, c)
        with Session(engine) as other_session:   # straight in the database: the API's own league writers wait for the archive (the league lock), which is the point
            other_session.add(ChampionshipEvent(championship_id=other, server_id=sid, filename="race1.json", session_type="Race"))
            other_session.commit()
        return out
    monkeypatch.setattr(archive, "snapshot", snapshot_then_other_league_takes_race1)
    r = api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga takeup"})
    assert r.status_code == 200, r.text
    assert paths[0].exists(), "race1.json is counted by another running league now: it must stay"
    assert not paths[1].exists() and not paths[2].exists()


def test_a_file_another_league_takes_up_after_the_commit_but_before_it_is_deleted_is_kept(monkeypatch):
    sid, cid, paths = _setup("lastlook")
    other = api.post(f"{V}/championships", json={"name": "Muy tardía"}).json()["id"]
    real, state = archive.result_path, {"done": False}

    def result_path_then_other_league_takes_race2(server_id, filename):
        if not state["done"]:
            with Session(engine) as s:
                if s.get(Championship, cid).archived_at is not None:   # the archive is COMMITTED (a fresh connection sees it): the deletion loop has started
                    state["done"] = True
                    s.add(ChampionshipEvent(championship_id=other, server_id=sid, filename="race2.json", session_type="Race"))
                    s.commit()
        return real(server_id, filename)
    monkeypatch.setattr(archive, "result_path", result_path_then_other_league_takes_race2)
    r = api.post(f"{V}/championships/{cid}/archive", json={"confirm": "Liga lastlook"})
    assert r.status_code == 200, r.text
    assert state["done"], "the hook never fired"
    assert paths[1].exists(), "race2.json was taken up by a running league after the commit: it must not be deleted"
    assert not paths[0].exists() and not paths[2].exists()
