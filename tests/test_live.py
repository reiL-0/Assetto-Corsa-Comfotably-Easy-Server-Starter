import io
import zipfile

from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content, supervisor
from app.live import acsp
from app.live.board import LiveBoard
from app.main import app

V = "/api/v1"
SESSION = {
    "type": "new_session", "server_name": "OPR test", "track": "magione", "track_config": "", "name": "Practice",
    "session_type": 1, "time_min": 10, "laps": 0, "ambient_temp": 17, "road_temp": 22, "elapsed_ms": 1000,
}


def _conn(car, name, guid):
    return {"type": "new_connection", "car_id": car, "driver_name": name, "driver_guid": guid,
            "car_model": "bmw_m3_e30", "car_skin": "red"}


def _board():
    b = LiveBoard()
    for e in (SESSION, _conn(0, "Ana", "111"), _conn(1, "Beto", "222")):
        b.apply(e, now=100.0)
    return b


def test_leaderboard_has_what_the_site_reads():
    b = _board()
    b.apply({"type": "car_update", "car_id": 0, "pos": [1.0, 2.0, 3.0], "velocity": [10.0, 0.0, 0.0],
             "gear": 3, "rpm": 5000, "spline_pos": 0.5})
    b.apply({"type": "lap_completed", "car_id": 0, "laptime_ms": 91000, "cuts": 0, "grip_level": 1.0,
             "leaderboard": [{"car_id": 0, "laptime_ms": 91000, "laps": 1, "has_completed": False},
                             {"car_id": 1, "laptime_ms": 4294967295, "laps": 0, "has_completed": False}]})
    lb = b.leaderboard(now=102.0)
    assert (lb["Track"], lb["Name"], lb["Type"], lb["Time"]) == ("magione", "Practice", 1, 10)
    assert lb["ElapsedMilliseconds"] == 3000  # 1000 at the session start + 2 s since it arrived
    ana, beto = lb["ConnectedDrivers"]
    assert ana["CarInfo"]["DriverName"] == "Ana" and ana["CarInfo"]["DriverGUID"] == "111"
    car = ana["Cars"]["bmw_m3_e30"]
    assert car["BestLap"] == 91000 * 1_000_000 and car["LastLap"] == 91000 * 1_000_000 and car["NumLaps"] == 1
    assert car["TopSpeedBestLap"] == 36  # 10 m/s
    assert ana["LastPos"] == {"X": 1.0, "Y": 2.0, "Z": 3.0} and ana["NormalisedSplinePos"] == 0.5
    assert beto["Cars"]["bmw_m3_e30"]["BestLap"] == 0  # the server's "no lap yet" sentinel is not a time


def test_disconnected_stay_listed_until_the_next_session():
    b = _board()
    b.apply({"type": "connection_closed", "car_id": 1, "driver_name": "Beto", "driver_guid": "222",
             "car_model": "bmw_m3_e30", "car_skin": "red"})
    lb = b.leaderboard()
    assert [e["CarInfo"]["DriverName"] for e in lb["DisconnectedDrivers"]] == ["Beto"]
    b.apply(SESSION)  # new session: only whoever is connected carries over, with a clean table
    assert b.leaderboard()["DisconnectedDrivers"] == []


def test_race_positions_by_laps_then_time():
    b = LiveBoard()
    for e in ({**SESSION, "session_type": 3}, _conn(0, "Ana", "111"), _conn(1, "Beto", "222")):
        b.apply(e)
    for car, ms in ((0, 95000), (1, 90000), (1, 90000), (0, 95000)):
        b.apply({"type": "lap_completed", "car_id": car, "laptime_ms": ms, "cuts": 0, "grip_level": 1.0, "leaderboard": []})
    pos = {e["CarInfo"]["DriverName"]: e["Position"] for e in b.leaderboard()["ConnectedDrivers"]}
    assert pos == {"Beto": 1, "Ana": 2}  # same laps: less total time wins


class _Inst:
    running = True

    def __init__(self, client):
        self.acsp = client


def test_leaderboard_route_is_public_and_409_when_stopped():
    api = TestClient(app)  # no token on purpose: the site's proxy and the telemetry backend send none
    url = f"{V}/servers/71/acsm/api/live-timings/leaderboard.json"
    assert api.get(url).status_code == 409
    c = acsp.ACSPClient(71)
    c.board = _board()
    supervisor._instances[71] = _Inst(c)
    try:
        r = api.get(url)
        assert r.status_code == 200 and r.json()["ConnectedDrivers"][0]["CarInfo"]["DriverName"] == "Ana"
    finally:
        supervisor._instances.pop(71)


def test_map_files_come_from_the_servers_content_and_cannot_escape_it():
    tracks = content._tracks_dir()
    (tracks / "magione" / "data").mkdir(parents=True, exist_ok=True)
    (tracks / "magione" / "map.png").write_bytes(b"\x89PNG-fake")
    (tracks / "magione" / "data" / "map.ini").write_text("[PARAMETERS]\nSCALE_FACTOR=1\n")
    (tracks / "spa" / "2022" / "data").mkdir(parents=True, exist_ok=True)
    (tracks / "spa" / "2022" / "map.png").write_bytes(b"\x89PNG-fake2")
    api = TestClient(app)
    base = f"{V}/servers/1/acsm/content/tracks"
    assert api.get(f"{base}/magione/map.png").content == b"\x89PNG-fake"
    assert "SCALE_FACTOR" in api.get(f"{base}/magione/data/map.ini").text
    assert api.get(f"{base}/spa/2022/map.png").status_code == 200
    assert api.get(f"{base}/nope/map.png").status_code == 404
    assert api.get(f"{base}/magione/data/map.png").status_code == 404
    # a decoy one level above tracks/: no spelling of "go up" may ever deliver it
    (tracks.parent / "map.png").write_bytes(b"OUTSIDE-TRACKS")
    for evil in ("..%2f..%2fetc/map.png", "..%2fmagione/map.png", "magione/..%2f../map.png", "..%5c..%5cmap.png"):
        r = api.get(f"{base}/{evil}")
        assert b"OUTSIDE-TRACKS" not in r.content and r.headers["content-type"] != "image/png", evil
    assert api.get(f"{base}/..%2fmagione/map.png").status_code == 404


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_track_upload_lands_in_the_servers_content_and_rejects_bad_archives():
    api = TestClient(app, headers=ADMIN)
    ok = _zip({"mytrack/data/map.ini": b"[PARAMETERS]", "mytrack/ui/outline.png": b"png"})
    r = api.post(f"{V}/content/tracks", files={"file": ("mytrack.zip", ok)})
    assert r.status_code == 201 and r.json() == {"track": "mytrack"}
    assert (content._tracks_dir() / "mytrack" / "data" / "map.ini").exists()
    two_roots = _zip({"a/x": b"1", "b/y": b"2"})
    assert api.post(f"{V}/content/tracks", files={"file": ("t.zip", two_roots)}).status_code == 400
    evil = _zip({"mytrack/../../evil": b"x"})
    assert api.post(f"{V}/content/tracks", files={"file": ("t.zip", evil)}).status_code == 400
    assert api.post(f"{V}/content/tracks", files={"file": ("t.txt", b"not an archive")}).status_code == 400


def test_track_import_from_the_inbox():
    api = TestClient(app, headers=ADMIN)
    assert api.post(f"{V}/content/tracks/import", json={"file": "missing.zip"}).status_code == 404
    assert api.post(f"{V}/content/tracks/import", json={"file": "../x.zip"}).status_code == 400
    (content.inbox_dir() / "big.zip").write_bytes(_zip({"inboxtrack/data/surfaces.ini": b"x"}))
    r = api.post(f"{V}/content/tracks/import", json={"file": "big.zip"})
    assert r.status_code == 201 and r.json() == {"track": "inboxtrack"}
    assert (content._tracks_dir() / "inboxtrack" / "data" / "surfaces.ini").exists()
    assert TestClient(app).post(f"{V}/content/tracks/import", json={"file": "big.zip"}).status_code == 401  # admin only
