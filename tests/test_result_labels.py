"""The readable names of the result files (app/results.py result_label / result_details, GET servers/{id}/results?details=1): the file name stays the id."""
import json
from pathlib import Path

from conftest import ADMIN
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.main import app
from app.models import ChampionshipEvent, Event
from app.results import result_label, result_start

client = TestClient(app, headers=ADMIN)


def _put(sid, name, body):
    d = Path(settings.data_dir) / "instances" / str(sid) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body if isinstance(body, str) else json.dumps(body))


def test_labels_by_case():
    assert result_label("2026_11_11_5_13_RACE.json", "OPR 1", "Spa", "Liga GT", "GT3 Fecha 3")== "GT3 Fecha 3 · Liga GT · Race"
    assert result_label("2026_11_11_5_13_RACE.json", "OPR 1", "Spa", "Liga GT", None)== "Liga GT · Spa · Race"
    assert result_label("2026_11_11_4_40_QUALIFY.json", "OPR 1", "Monza", None, None)== "OPR 1 · Monza · Quali"
    assert result_label("2026_11_11_4_25_PRACTICE.json", "OPR 1", None, None, None)== "OPR 1 · pista desconocida · Practice"
    assert result_label("race1.json", "OPR 1", "Spa", None, None) == "race1.json", "not a dated result file: shown as it is"
    assert ":" not in result_label("2026_11_11_5_13_RACE.json", "S", "T", None, None), "no date in the label: the browser adds it from started_at"


def test_start_reads_unpadded_hours_and_rejects_nonsense():
    assert result_start("2026_11_11_5_13_RACE.json") > result_start("2026_11_11_5_12_RACE.json")
    assert result_start("2026_13_40_99_99_RACE.json") is None and result_start("x.json") is None


def test_details_are_newest_first_by_date_not_by_text_and_a_broken_file_does_not_break_the_list():
    sid = client.post("/api/v1/servers", json={"name": "Labels"}).json()["id"]
    _put(sid, "2026_11_11_10_0_RACE.json", {"Type": "Race", "TrackName": "rt_spa", "TrackConfig": "gp"})
    _put(sid, "2026_11_11_5_13_RACE.json", {"Type": "Race", "TrackName": "monza"})   # text order puts this AFTER the 10:00 one; by date it is before
    _put(sid, "2026_11_11_4_40_QUALIFY.json", "")   # empty: no track, still listed
    _put(sid, "2026_11_11_3_0_PRACTICE.json", {"Type": "Practice", "TrackName": 42, "TrackConfig": ["x"]})   # wrong types: no track, no 500 for the whole list
    names = [r["filename"] for r in client.get(f"/api/v1/servers/{sid}/results?details=1").json()]
    assert names == ["2026_11_11_10_0_RACE.json", "2026_11_11_5_13_RACE.json", "2026_11_11_4_40_QUALIFY.json", "2026_11_11_3_0_PRACTICE.json"]
    rows = {r["filename"]: r for r in client.get(f"/api/v1/servers/{sid}/results?details=1").json()}
    assert rows["2026_11_11_10_0_RACE.json"]["label"]== "Labels · rt spa (gp) · Race" and "pista desconocida" in rows["2026_11_11_4_40_QUALIFY.json"]["label"] and "pista desconocida" in rows["2026_11_11_3_0_PRACTICE.json"]["label"]
    assert client.get(f"/api/v1/servers/{sid}/results").json() == sorted(names), "without ?details the list is the file names, as before"


def test_a_counted_result_shows_its_league_and_event_title():
    sid = client.post("/api/v1/servers", json={"name": "Labels2"}).json()["id"]
    _put(sid, "2026_11_11_5_13_RACE.json", {"Type": "Race", "TrackName": "spa"})
    cid = client.post("/api/v1/championships", json={"name": "Liga GT"}).json()["id"]
    with Session(engine) as s:
        ev = Event(title="GT3 Fecha 3", data={"name": "r", "track": "spa", "cars": ["a"]})
        s.add(ev)
        s.commit()
        s.add(ChampionshipEvent(championship_id=cid, server_id=sid, filename="2026_11_11_5_13_RACE.json", event_id=ev.id, session_type="Race"))
        s.commit()
    r = client.get(f"/api/v1/servers/{sid}/results?details=1").json()[0]
    assert r["label"]== "GT3 Fecha 3 · Liga GT · Race" and r["started_at"].endswith("+00:00") and r["league"] == "Liga GT" and r["title"] == "GT3 Fecha 3"
