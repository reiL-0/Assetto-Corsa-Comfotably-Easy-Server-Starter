"""The league's preset: skin + fixed setup per roster driver, the league's setup rule per car, its own requirements list, and skins kept as folder names."""
import io
import sqlite3
import time
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session
from test_session import FORM, V, _install, _server, api

from app import announcement, content, db, requirements, wake
from app.db import engine
from app.league import session_for
from app.models import Event, Schedule, Server

A, B = "76561198000000011", "76561198000000012"


def _setup(name, text="[CAR_0]\nMODEL=bmw\n"):
    assert api.put(f"{V}/content/setups/{name}", content=text.encode()).status_code == 201


def _league(**kw):
    return api.post(f"{V}/championships", json={"name": f"Liga {time.time_ns()}", **kw}).json()


def _member(lid, guid, **kw):
    return api.post(f"{V}/championships/{lid}/members", json={"guid": guid, "name": "N" + guid[-2:], **kw})


def test_migration_007_adds_the_preset_columns_and_keeps_every_league_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / "v6.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:6])
    db.init_db()
    con = sqlite3.connect(path)   # a deployed database before 007
    for table, cols in (("championships", ("car_setups", "requirements")), ("league_members", ("skin", "fixed_setup"))):
        for col in cols:
            con.execute(f'ALTER TABLE {table} DROP COLUMN {col}')
    con.execute("INSERT INTO championships (id, name, points_system, practice_required, practice_laps, practice_days, created_at, archived_by) VALUES (1, 'Vieja', '[25]', 0, 5, 7, '2026-01-01', '')")
    con.execute("INSERT INTO league_members (championship_id, guid, name, team, car, exempt, non_racing) VALUES (1, '76561198000000099', 'Ana', '', 'bmw', 0, 0)")
    con.commit()
    con.close()
    monkeypatch.undo()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    con = sqlite3.connect(path)
    assert con.execute("SELECT skin, fixed_setup FROM league_members").fetchall() == [("", "")]
    assert con.execute("SELECT car_setups, requirements FROM championships").fetchall() == [(None, None)]
    assert [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7]
    db.init_db()   # a rerun changes nothing
    con.close()


def test_roster_skin_and_setup_are_validated_and_an_older_panel_does_not_wipe_them():
    _setup("lg_bmw")
    lid = _league()["id"]
    assert _member(lid, A, car="bmw", fixed_setup="ghost").status_code == 400
    for bad in ("a\nb", "../x", "x" * 81):
        assert _member(lid, A, car="bmw", skin=bad).status_code == 422
    assert _member(lid, A, car="bmw", fixed_setup="x.ini").status_code == 422
    r = _member(lid, A, car="bmw", skin="red", fixed_setup="lg_bmw")
    assert r.status_code == 200 and (r.json()["skin"], r.json()["fixed_setup"]) == ("red", "lg_bmw")
    assert _member(lid, A, car="bmw").json()["skin"] == "red"                                    # an older panel sends neither: what is saved stays
    row = {m["guid"]: m for m in api.get(f"{V}/championships/{lid}/members").json()}[A]
    assert (row["skin"], row["fixed_setup"]) == ("red", "lg_bmw")
    cleared = _member(lid, A, car="bmw", skin="", fixed_setup="").json()
    assert (cleared["skin"], cleared["fixed_setup"]) == ("", "")                                   # "" clears


def test_a_drivers_setup_beats_the_leagues_rule_which_beats_the_events():
    _setup("own_one"); _setup("league_rule"); _setup("event_rule")
    lid = _league(car_setups={"bmw": "league_rule"})["id"]
    _member(lid, A, car="bmw", skin="red", fixed_setup="own_one")
    _member(lid, B, car="bmw", skin="blue")
    _member(lid, "76561198000000013", car="ferrari", skin="rosso")   # a car the event does not run: the first one, and its skin does not follow
    with Session(engine) as s:
        ev = Event(title="R1", league_id=lid, data={"name": "r1", "track": "spa", "cars": ["bmw", "audi"], "car_setups": {"bmw": "event_rule", "audi": "event_rule"}})
        s.add(ev); s.commit()
        got = {e.guid: e for e in session_for(s, ev, time.time()).entries}
        assert (got[A].model, got[A].skin, got[A].fixed_setup) == ("bmw", "red", "own_one")
        assert (got[B].skin, got[B].fixed_setup) == ("blue", "league_rule")
        assert (got["76561198000000013"].model, got["76561198000000013"].skin, got["76561198000000013"].fixed_setup) == ("bmw", "", "league_rule")
        api.patch(f"{V}/championships/{lid}", json={"name": "x", "car_setups": None})   # no league rule: the event's
        assert {e.guid: e.fixed_setup for e in session_for(s, ev, time.time()).entries}[B] == "event_rule"


def test_league_setup_rule_is_validated_and_protects_the_setup_from_deletion():
    _setup("rule_x"); _setup("pinned")
    lid = _league()["id"]
    assert api.patch(f"{V}/championships/{lid}", json={"name": "x", "car_setups": {"bmw": "ghost"}}).status_code == 400
    assert api.patch(f"{V}/championships/{lid}", json={"name": "x", "car_setups": {"bmw": "a\nb"}}).status_code == 422
    assert api.patch(f"{V}/championships/{lid}", json={"name": "x", "car_setups": {"..": "rule_x"}}).status_code == 422
    assert api.patch(f"{V}/championships/{lid}", json={"name": "x", "car_setups": {"bmw": "rule_x"}}).json()["car_setups"] == {"bmw": "rule_x"}
    _member(lid, A, car="bmw", fixed_setup="pinned")
    r = api.delete(f"{V}/content/setups/rule_x")
    assert r.status_code == 409 and "league" in r.text
    r = api.delete(f"{V}/content/setups/pinned")
    assert r.status_code == 409 and "driver" in r.text
    api.patch(f"{V}/championships/{lid}", json={"name": "x", "car_setups": None})
    _member(lid, A, car="bmw", fixed_setup="")
    assert api.delete(f"{V}/content/setups/rule_x").status_code == 200 and api.delete(f"{V}/content/setups/pinned").status_code == 200


def test_requirements_per_league_fall_back_to_the_global_list():
    glob = {"csp_build": 3000, "items": [{"name": "Global", "url": "https://x.test/g"}]}
    own = {"csp_build": 0, "items": [{"name": "Skins OPR", "url": "https://drive.google.com/drive/folders/1"}]}
    api.put(f"{V}/requirements", json=glob)
    try:
        lid = _league()["id"]
        assert api.get(f"{V}/championships/{lid}/requirements").json() == {"own": False, **glob}
        assert api.put(f"{V}/championships/{lid}/requirements", json=own).json() == {"own": True, **own}
        assert requirements.current(lid) == own and requirements.current() == glob and requirements.current(None) == glob
        assert api.put(f"{V}/championships/{lid}/requirements", json={"csp_build": 0, "items": []}).json()["items"] == []
        assert requirements.lines(requirements.current(lid)) == []                                   # an empty own list is still its own, not the global one
        vars_ = announcement.variables("T", "S", {"track": "spa", "cars": ["a"]}, 1.0, {"yes": 0, "maybe": 0, "no": 0}, league_id=lid)
        assert vars_["requisitos"] == ""
        api.put(f"{V}/championships/{lid}/requirements", json=own)
        assert "Skins OPR" in announcement.variables("T", "S", {"track": "spa", "cars": ["a"]}, 1.0, {"yes": 0, "maybe": 0, "no": 0}, league_id=lid)["requisitos"]
        assert "Global" in announcement.variables("T", "S", {"track": "spa", "cars": ["a"]}, 1.0, {"yes": 0, "maybe": 0, "no": 0})["requisitos"]
        assert api.delete(f"{V}/championships/{lid}/requirements").json() == {"own": False, **glob}
        assert api.get(f"{V}/championships/999999/requirements").status_code == 404
    finally:
        api.put(f"{V}/requirements", json={"csp_build": 0, "items": []})


def test_the_lobby_page_shows_the_requirements_of_the_league_loaded_on_that_server():
    own = {"csp_build": 0, "items": [{"name": "Pack liga", "url": "https://x.test/liga"}]}
    api.put(f"{V}/requirements", json={"csp_build": 0, "items": [{"name": "Global", "url": "https://x.test/g"}]})
    try:
        lid = _league()["id"]
        api.put(f"{V}/championships/{lid}/requirements", json=own)
        sid = _server()
        srv = Server(id=sid, name="L", base_port=9700, config={"SERVER": {"TRACK": "spa"}}, entry_list=[], welcome="Hola")
        assert "Global" in wake.details(srv, None)["description"] and wake._league_of(sid) is None
        with Session(engine) as s:
            ev = Event(title="E", league_id=lid, data={"name": "e", "track": "spa", "cars": ["a"]})
            s.add(ev); s.commit()
            sc = Schedule(event_id=ev.id, server_id=sid, start_at=time.time(), loaded=True, state="pending")
            s.add(sc); s.commit()
            try:
                d = wake.details(srv, None)["description"]
                assert wake._league_of(sid) == lid and "Pack liga" in d and "Global" not in d
            finally:   # the scheduler would pick it up in the other tests
                s.delete(sc); s.delete(ev); s.commit()
    finally:
        api.put(f"{V}/requirements", json={"csp_build": 0, "items": []})


# ---- skins: only the folder name and ui_skin.json stay on the server -------------------------------------------------------------------

def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, b in files.items():
            zf.writestr(n, b)
    return buf.getvalue()


CAR, CAR2 = "skin_car_one", "skin_car_two"   # their own cars: the other tests count the skins of bmw / audi


def _cars():
    for c in (CAR, CAR2):
        (content._cars_dir() / c).mkdir(parents=True, exist_ok=True)
        (content._cars_dir() / c / "data.acd").write_text("x")


def _skins(files, car=None):
    r = api.post(f"{V}/content/cars/{car or CAR}/skins", files={"file": ("p.zip", _zip(files))})
    return r


def _skin_dir(car, skin):
    return content._cars_dir() / car / "skins" / skin


def test_a_skin_upload_keeps_only_its_folder_and_ui_skin_json():
    _cars()
    r = _skins({"blue/ui_skin.json": b"{}", "blue/body.dds": b"x" * 5000, "blue/preview.jpg": b"j"})
    assert r.status_code == 201, r.text
    assert r.json()["skins"] == [f"{CAR}/blue"] and r.json()["skin"] == "blue" and r.json()["car"] == CAR
    assert sorted(p.name for p in _skin_dir(CAR, "blue").iterdir()) == ["ui_skin.json"]


@pytest.mark.parametrize("files, expect", [
    ({f"{CAR}/skins/gold/ui_skin.json": b"{}", f"{CAR}/skins/gold/a.dds": b"x", f"{CAR2}/skins/white/ui_skin.json": b"{}"}, [f"{CAR}/gold", f"{CAR2}/white"]),   # a pack with several cars
    ({f"assettocorsa/content/cars/{CAR}/skins/pink/ui_skin.json": b"{}", "README.txt": b"hi"}, [f"{CAR}/pink"]),                                       # a mod's full path
    ({"skins/a/ui_skin.json": b"{}", "skins/b/body.dds": b"x"}, [f"{CAR}/a", f"{CAR}/b"]),                                                              # a skin without json is still a name
    ({"one/ui_skin.json": b"{}", "two/ui_skin.json": b"{}", "__MACOSX/x/ui_skin.json": b"{}"}, [f"{CAR}/one", f"{CAR}/two"]),
    ({"nombre con espacio/ui_skin.json": b"{}", "ñandú/ui_skin.json": b"{}"}, [f"{CAR}/nombre con espacio", f"{CAR}/ñandú"]),
    ({"skinA/body.dds": b"x", "skinB/body.dds": b"x"}, [f"{CAR}/skinA", f"{CAR}/skinB"]),                                                              # no json anywhere: the top folders
])
def test_skin_packs_are_found_whatever_their_layout(files, expect):
    _cars()
    r = _skins(files)
    assert r.status_code == 201, r.text
    assert sorted(r.json()["skins"]) == expect
    for item in expect:
        car, skin = item.split("/", 1)
        assert _skin_dir(car, skin).is_dir() and not [p for p in _skin_dir(car, skin).iterdir() if p.name != "ui_skin.json"]


@pytest.mark.parametrize("files, why", [
    ({"README.txt": b"hi"}, "no skin folders"),
    ({"skins/a/ui_skin.json": b"{}", "other/skins/a/ui_skin.json": b"{}", f"{CAR}/skins/a/ui_skin.json": b"{}"}, "twice"),
])
def test_unclear_or_unsafe_skin_packs_are_refused(files, why):
    _cars()
    r = _skins(files)
    assert r.status_code == 400 and why in r.json()["detail"], r.text


def test_a_pack_that_names_a_car_we_do_not_have_is_refused_unless_the_uploader_names_one():
    _cars()
    pack = {"x/skins/a/ui_skin.json": b"{}"}
    uid = api.post(f"{V}/content/uploads", json={"kind": "skin"}).json()["id"]   # no car given, and the archive's «x» is not one of ours
    api.put(f"{V}/content/uploads/{uid}?offset=0", content=_zip(pack))
    api.post(f"{V}/content/uploads/{uid}/complete")
    for _ in range(100):
        st = api.get(f"{V}/content/uploads/{uid}").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["state"] == "error" and "car not installed: x" in st["error"], st
    assert not (content._cars_dir() / "x").exists()
    assert _skins(pack).json()["skins"] == [f"{CAR}/a"]   # the uploader said bmw: «x» was only the pack's folder


def test_skins_for_an_unnamed_or_unknown_car_are_refused_and_nothing_is_created():
    _cars()
    assert api.post(f"{V}/content/cars/ghost/skins", files={"file": ("p.zip", _zip({"a/ui_skin.json": b"{}"}))}).status_code == 400
    assert not (content._cars_dir() / "ghost").exists()
    assert api.post(f"{V}/content/cars/a%0Ab/skins", files={"file": ("p.zip", _zip({"a/ui_skin.json": b"{}"}))}).status_code in (400, 404)


def test_skins_can_come_through_the_chunked_upload_and_the_entry_list_refuses_unsafe_skin_names(monkeypatch):
    _cars()
    body = _zip({"neon/ui_skin.json": b"{}", "neon/big.dds": b"x" * 3000})
    uid = api.post(f"{V}/content/uploads", json={"kind": "skin", "car": CAR}).json()["id"]
    assert api.put(f"{V}/content/uploads/{uid}?offset=0", content=body).status_code == 200
    assert api.post(f"{V}/content/uploads/{uid}/complete").status_code == 202
    for _ in range(100):
        st = api.get(f"{V}/content/uploads/{uid}").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["state"] == "done" and st["kind"] == "skin" and st["result"] == f"{CAR}/neon", st
    assert [p.name for p in _skin_dir(CAR, "neon").iterdir()] == ["ui_skin.json"]
    sid = _server()
    bad = {"model": "bmw", "driver_name": "Ana", "skin": "red\nFIXED_SETUP=x"}
    assert api.post(f"{V}/servers/{sid}/apply", json={**FORM, "cars": [], "entries": [bad]}).status_code == 422
