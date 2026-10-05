import io
import time
import zipfile

from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content, supervisor
from app.config import settings
from app.main import app

V = "/api/v1"
api = TestClient(app, headers=ADMIN)


def _touch(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _install():
    """spa (plain), nords (two layouts), mapsonly (only map files), and two cars."""
    t = content._tracks_dir()
    _touch(t / "spa" / "data" / "surfaces.ini")
    for lay in ("gp", "sprint"):
        _touch(t / "nords" / lay / "data" / "surfaces.ini")
    _touch(t / "mapsonly" / "data" / "map.ini")
    _touch(t / "mapsonly" / "map.png")
    c = content._cars_dir()
    _touch(c / "bmw" / "data.acd")
    _touch(c / "bmw" / "skins" / "red" / "livery.png")
    _touch(c / "audi" / "data.acd")
    _touch(c / "leftover" / "readme.txt")  # a folder without data.acd is not a loadable car


def _server():
    return api.post(f"{V}/servers", json={"name": "t"}).json()["id"]


FORM = {"name": "Liga", "password": "pw", "admin_password": "boss", "track": "spa", "cars": ["bmw", "audi"],
        "max_clients": 5, "practice_min": 10, "qualify_min": 5, "race_laps": 3, "restart": False}


def test_listings_mark_what_acserver_can_load():
    _install()
    tracks = {t["track"]: t for t in api.get(f"{V}/content/tracks").json()}
    assert tracks["spa"]["usable"] and tracks["spa"]["base"] and tracks["nords"]["usable"] and not tracks["nords"]["base"]
    assert not tracks["mapsonly"]["usable"]  # map placeholders must not be offered as tracks
    assert [c["config"] for c in tracks["nords"]["configs"]] == ["gp", "sprint"]
    cars = {c["car"]: c for c in api.get(f"{V}/content/cars").json()}
    assert cars["bmw"]["usable"] and not cars["leftover"]["usable"] and cars["bmw"]["skins"] == ["red"]


def test_apply_builds_config_and_entry_list():
    _install()
    sid = _server()
    r = api.post(f"{V}/servers/{sid}/apply", json=FORM)
    assert r.status_code == 200 and r.json()["restarted"] is False
    srv = r.json()["config"]["SERVER"]
    assert (srv["NAME"], srv["PASSWORD"], srv["ADMIN_PASSWORD"], srv["TRACK"], srv["CARS"], srv["MAX_CLIENTS"]) == (
        "Liga", "pw", "boss", "spa", "bmw;audi", 5)
    assert srv["SLEEP_TIME"] == 1 and "WEATHER_0" in r.json()["config"]
    assert r.json()["config"]["RACE"]["LAPS"] == 3 and r.json()["config"]["PRACTICE"]["TIME"] == 10
    entries = r.json()["entry_list"]
    assert [e["MODEL"] for e in entries] == ["bmw", "audi", "bmw", "audi", "bmw"]
    assert entries[0]["SKIN"] == "red" and entries[1]["SKIN"] == ""
    ini = api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    assert "CARS=bmw;audi" in ini and "[RACE]" in ini and "[QUALIFY]" in ini


def test_apply_keeps_admin_password_and_drops_disabled_sessions():
    _install()
    sid = _server()
    api.post(f"{V}/servers/{sid}/apply", json=FORM)
    r = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "admin_password": None, "qualify_min": 0, "race_laps": None})
    cfg = r.json()["config"]
    assert cfg["SERVER"]["ADMIN_PASSWORD"] == "boss" and "QUALIFY" not in cfg and "RACE" not in cfg and "PRACTICE" in cfg


def test_apply_rejects_what_the_server_could_not_run():
    _install()
    sid = _server()
    bad = [
        {"track": "nope"}, {"track": "mapsonly"}, {"track": "spa", "track_config": "gp"},
        {"track": "nords"},  # has layouts but none chosen
        {"track": "nords", "track_config": "oval"}, {"cars": ["ghost"]}, {"cars": ["leftover"]},
        {"practice_min": 0, "qualify_min": 0, "race_laps": 0},  # no session at all
        {"max_clients": 0}, {"name": ""},
    ]
    for patch in bad:
        assert api.post(f"{V}/servers/{sid}/apply", json={**FORM, **patch}).status_code in (400, 422), patch
    assert api.post(f"{V}/servers/{sid}/apply", json={**FORM, "track": "nords", "track_config": "gp"}).status_code == 200
    assert TestClient(app).post(f"{V}/servers/{sid}/apply", json=FORM).status_code == 401


def _chunks(data: bytes, n: int):
    size = -(-len(data) // n)
    return [data[i : i + size] for i in range(0, len(data), size)]


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in entries.items():
            zf.writestr(k, v)
    return buf.getvalue()


def _wait(uid):
    for _ in range(100):
        st = api.get(f"{V}/content/uploads/{uid}").json()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("upload never finished")


def test_chunked_upload_resumes_unpacks_and_reports_errors():
    data = _zip({"bigtrack/data/surfaces.ini": b"x" * 5000, "bigtrack/map.png": b"png"})
    uid = api.post(f"{V}/content/uploads", json={"kind": "track"}).json()["id"]
    parts = _chunks(data, 3)
    off = 0
    for i, part in enumerate(parts):
        if i == 1:  # a client that lost track of its position is told where to resume
            r = api.put(f"{V}/content/uploads/{uid}", params={"offset": off + 7}, content=part)
            assert r.status_code == 409 and str(off) in r.json()["detail"]
        r = api.put(f"{V}/content/uploads/{uid}", params={"offset": off}, content=part)
        assert r.status_code == 200
        off = r.json()["size"]
    assert api.post(f"{V}/content/uploads/{uid}/complete").status_code == 202
    st = _wait(uid)
    assert st["state"] == "done" and st["result"] == "bigtrack"
    assert (content._tracks_dir() / "bigtrack" / "data" / "surfaces.ini").is_file()
    assert not any(content._scratch().glob("upload-*"))  # the assembled file is cleaned up

    bad = api.post(f"{V}/content/uploads", json={"kind": "car"}).json()["id"]
    api.put(f"{V}/content/uploads/{bad}", params={"offset": 0}, content=b"this is not an archive")
    api.post(f"{V}/content/uploads/{bad}/complete")
    st = _wait(bad)
    assert st["state"] == "error" and "not a zip or rar" in st["error"]


def test_upload_endpoints_are_admin_only_and_validated():
    anon = TestClient(app)
    assert anon.post(f"{V}/content/uploads", json={"kind": "track"}).status_code == 401
    assert api.post(f"{V}/content/uploads", json={"kind": "skin"}).status_code == 422
    assert api.get(f"{V}/content/uploads/deadbeef").status_code == 404
    uid = api.post(f"{V}/content/uploads", json={"kind": "track"}).json()["id"]
    assert api.post(f"{V}/content/uploads/{uid}/complete").status_code == 409  # nothing uploaded yet


def test_apply_can_restart_the_server(tmp_path, monkeypatch):
    fake = tmp_path / "acServer"  # the binary's folder is where content/ lives
    fake.write_text("#!/bin/sh\nexec sleep 60\n")
    fake.chmod(0o755)
    monkeypatch.setattr(settings, "acserver_cmd", str(fake))
    _install()  # content now lives next to the fake binary, not in the shared test dir
    with TestClient(app, headers=ADMIN) as one_loop:  # the server process belongs to one event loop for the whole test
        sid = one_loop.post(f"{V}/servers", json={"name": "t"}).json()["id"]
        try:
            first = one_loop.post(f"{V}/servers/{sid}/apply", json={**FORM, "restart": True}).json()
            assert first["restarted"] is True and supervisor.get(sid).running
            pid = supervisor.get(sid).proc.pid
            again = one_loop.post(f"{V}/servers/{sid}/apply", json={**FORM, "restart": True}).json()
            assert again["restarted"] and supervisor.get(sid).proc.pid != pid  # old process replaced
        finally:
            one_loop.post(f"{V}/servers/{sid}/stop")


G1, G2, G3 = "76561199003234525", "76561198418379726", "76561198000000001"
ENTRIES = [
    {"model": "bmw", "driver_name": "Ana", "team": "Rojo", "guid": G1, "ballast": 20, "restrictor": 5},
    {"model": "audi", "driver_name": "Beto", "team": "Azul", "guid": f"{G2};{G3}", "spectator": False},
    {"model": "bmw"},  # an open slot
]


def test_entry_list_with_drivers_ballast_and_locking():
    _install()
    sid = _server()
    form = {**FORM, "cars": [], "max_clients": 30, "entries": ENTRIES, "locked": True, "pickup": False}
    r = api.post(f"{V}/servers/{sid}/apply", json=form)
    assert r.status_code == 200
    srv = r.json()["config"]["SERVER"]
    assert srv["MAX_CLIENTS"] == 3 and srv["CARS"] == "bmw;audi"  # the list sets slots and cars, not the form's numbers
    assert srv["LOCKED_ENTRY_LIST"] == 1 and srv["PICKUP_MODE_ENABLED"] == 0
    e = r.json()["entry_list"]
    assert (e[0]["DRIVERNAME"], e[0]["TEAM"], e[0]["GUID"], e[0]["BALLAST"], e[0]["RESTRICTOR"]) == ("Ana", "Rojo", G1, 20, 5)
    assert e[0]["SKIN"] == "red" and e[1]["GUID"] == f"{G2};{G3}" and e[2]["GUID"] == ""
    ini = api.get(f"{V}/servers/{sid}/entry_list.ini").text
    assert f"GUID={G1}" in ini and "BALLAST=20" in ini and "RESTRICTOR=5" in ini and "TEAM=Rojo" in ini
    # plain anonymous slots still work and pickup defaults on
    r = api.post(f"{V}/servers/{sid}/apply", json=FORM)
    assert r.json()["config"]["SERVER"]["PICKUP_MODE_ENABLED"] == 1 and r.json()["config"]["SERVER"]["LOCKED_ENTRY_LIST"] == 0


def test_entry_list_is_validated():
    _install()
    sid = _server()

    def post(**patch):
        return api.post(f"{V}/servers/{sid}/apply", json={**FORM, "cars": [], "entries": ENTRIES, **patch})

    assert post(entries=[ENTRIES[0], {**ENTRIES[0], "driver_name": "otra"}]).status_code == 400  # same Steam ID twice
    assert post(entries=[ENTRIES[0], {**ENTRIES[1], "guid": f"{G3};{G1}"}]).status_code == 400  # ...inside a shared one
    assert post(entries=[{"model": "bmw"}], locked=True).status_code == 400  # locked but nobody could join
    assert post(entries=[{"model": "ghost", "guid": G1}]).status_code == 400  # car not installed
    for bad in ({"guid": "123"}, {"guid": "abcdefghijklmnopq"}, {"ballast": 301}, {"restrictor": 101}, {"ballast": -1}):
        assert post(entries=[{**ENTRIES[0], **bad}]).status_code == 422, bad
    assert post(entries=[]).status_code == 422  # neither cars nor entries
    assert post(entries=ENTRIES * 20).status_code == 422  # 60 slots


def test_saved_event_keeps_the_entry_list():
    _install()
    body = {"title": "Liga rojo", "session": {**FORM, "cars": [], "entries": ENTRIES, "locked": True}}
    eid = api.post(f"{V}/events", json=body).json()["id"]
    got = api.get(f"{V}/events/{eid}").json()["session"]
    assert got["locked"] is True and got["entries"][0]["guid"] == G1 and got["entries"][0]["ballast"] == 20
    sid = _server()
    r = api.post(f"{V}/events/{eid}/run", json={"server_id": sid, "restart": False})
    assert r.status_code == 200 and r.json()["entry_list"][0]["DRIVERNAME"] == "Ana"


def test_a_timed_race_is_written_as_time_not_laps_and_both_are_refused():
    _install()
    sid = _server()
    r = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "practice_min": 0, "qualify_min": 0, "race_laps": 0, "race_min": 90, "restart": False})
    assert r.status_code == 200, r.text
    race = r.json()["config"]["RACE"]
    assert race["LAPS"] == 0 and race["TIME"] == 90
    ini = api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    assert "[RACE]" in ini and "TIME=90" in ini and "LAPS=0" in ini
    both = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "race_laps": 5, "race_min": 90, "restart": False})
    assert both.status_code == 422
    laps = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "race_laps": 5, "race_min": 0, "restart": False}).json()
    assert laps["config"]["RACE"]["LAPS"] == 5 and "TIME" not in laps["config"]["RACE"]


def test_welcome_message_is_written_next_to_the_config_and_removed_when_empty():
    from sqlmodel import Session

    from app import servers as srvmod
    from app.db import engine
    from app.models import Server
    _install()
    sid = _server()
    r = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "welcome": "Práctica libre.\nSin contacto.", "restart": False})
    assert r.status_code == 200 and r.json()["welcome"] == "Práctica libre.\nSin contacto."
    ini = api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    assert "WELCOME_MESSAGE=cfg/welcome.txt" in ini
    with Session(engine) as s:
        d = srvmod._write_instance(s.get(Server, sid))
    assert (d / "cfg" / "welcome.txt").read_text() == "Práctica libre.\nSin contacto."
    r = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "welcome": "", "restart": False})
    assert "WELCOME_MESSAGE" not in api.get(f"{V}/servers/{sid}/server_cfg.ini").text
    with Session(engine) as s:
        d = srvmod._write_instance(s.get(Server, sid))
    assert not (d / "cfg" / "welcome.txt").exists()



def test_apply_stores_the_session_and_only_a_session_change_resets_the_clock():
    from sqlmodel import Session

    from app.db import engine
    from app.models import Server
    _install()
    sid = _server()
    r = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "restart": False}).json()
    assert r["session"]["track"] == "spa" and r["session"]["cars"] == ["bmw", "audi"] and "admin_password" not in r["session"]
    assert api.get(f"{V}/servers/{sid}").json()["session"]["name"] == "Liga"
    with Session(engine) as db:
        srv = db.get(Server, sid)
        srv.anchor_index, srv.anchor_at = 1, 1000.0
        db.add(srv)
        db.commit()
    api.post(f"{V}/servers/{sid}/apply", json={**FORM, "name": "Liga 2", "restart": False})   # a rename (autosave): the clock stays
    with Session(engine) as db:
        assert db.get(Server, sid).anchor_index == 1
    api.post(f"{V}/servers/{sid}/apply", json={**FORM, "practice_min": 20, "restart": False})  # a different set-up: the clock starts over
    with Session(engine) as db:
        assert db.get(Server, sid).anchor_index is None
