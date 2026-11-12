"""app/records.py: the best valid lap per driver, track layout and car, from any session; served for the track a server is set up with, running or not."""

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import records
from app.db import engine
from app.main import app
from app.models import Server

V = "/api/v1"
G1, G2 = "76561199000000011", "76561199000000012"


def test_only_a_valid_lap_that_beats_the_record_is_kept():
    assert records.submit("csp/3898/../vir", "full course", "gt3", G1, "Ana", 100_000)
    assert not records.submit("vir", "full course", "gt3", G1, "Ana", 100_000)   # not better
    assert not records.submit("vir", "full course", "gt3", G1, "Ana", 90_000, cuts=1)   # cut: not a lap
    assert not records.submit("vir", "full course", "gt3", "", "Ghost", 90_000)   # no Steam ID
    assert not records.submit("vir", "full course", "gt3", G1, "Ana", 999_999_999)   # acServer's "no lap" sentinel
    assert records.submit("vir", "full course", "gt3", G1, "Ana", 98_000)   # the same track (csp/ prefix cleaned), better
    records.submit("vir", "full course", "gt3", G2, "Beto", 97_000)
    records.submit("vir", "full course", "nsx", G2, "Beto", 99_000)
    records.submit("vir", "", "gt3", G2, "Beto", 50_000)   # another layout: its own table
    cars = {c["car_model"]: c["laps"] for c in records.top("vir", "full course")}
    assert [(r["name"], r["best_ms"]) for r in cars["gt3"]] == [("Beto", 97_000), ("Ana", 98_000)]
    assert [r["best_ms"] for r in cars["nsx"]] == [99_000]
    assert [r["best_ms"] for r in records.top("vir", "")[0]["laps"]] == [50_000]


def test_a_stale_process_cache_cannot_lower_a_stored_record():
    records.submit("monza", "", "gt3", G1, "Ana", 80_000)
    records._best.clear()   # as after a restart: the cache forgot, the database did not
    records.submit("monza", "", "gt3", G1, "Ana", 85_000)   # slower than what is stored
    assert records.top("monza", "")[0]["laps"][0]["best_ms"] == 80_000


def test_endpoint_serves_the_set_up_track_of_a_stopped_server():
    with Session(engine) as s:
        s.add(Server(id=950, name="r", base_port=9950, config={"SERVER": {"TRACK": "csp/3898/../vir", "CONFIG_TRACK": "full course"}}))
        s.commit()
    api = TestClient(app)
    records.submit("vir", "full course", "gt3", G1, "Ana", 96_000)
    r = api.get(f"{V}/servers/950/acsm/api/live-timings/records.json").json()
    assert r["Track"] == "vir" and r["TrackConfig"] == "full course"
    assert r["Cars"][0]["laps"][0]["best_ms"] <= 96_000
    assert api.get(f"{V}/servers/951/acsm/api/live-timings/records.json").status_code == 404


def test_submit_says_false_when_the_database_already_had_a_better_one():
    records.submit("imola", "", "gt3", G1, "Ana", 70_000)
    records._best.clear()
    assert not records.submit("imola", "", "gt3", G1, "Ana", 75_000)
