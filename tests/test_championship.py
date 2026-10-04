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

