"""Cars per model (`car_counts`) and fixed setups (`FIXED_SETUP`, the .ini files under the acServer's setups/)."""
import pytest
from sqlmodel import Session
from test_racecontrol import _client
from test_session import FORM, V, _install, _server, api

from app import content, servers
from app.config import settings
from app.db import engine
from app.models import Server
from app.services import server_service

COUNTS = {**FORM, "cars": [], "max_clients": 1, "car_counts": {"bmw": 5, "audi": 2}}
SETUP = "[CAR_0]\nMODEL=bmw\n[FRONT]\nROD_LENGTH=0\n"


def _put(name, data=SETUP, client=api):
    return client.put(f"{V}/content/setups/{name}", content=data if isinstance(data, bytes) else data.encode())


def test_car_counts_build_grouped_open_slots():
    _install()
    sid = _server()
    r = api.post(f"{V}/servers/{sid}/apply", json=COUNTS)
    assert r.status_code == 200, r.text
    assert [e["MODEL"] for e in r.json()["entry_list"]] == ["bmw"] * 5 + ["audi"] * 2
    srv = r.json()["config"]["SERVER"]
    assert srv["MAX_CLIENTS"] == 7 and srv["CARS"] == "bmw;audi"   # the form's `max_clients` and `cars` follow from the counts
    s = api.get(f"{V}/servers/{sid}").json()["session"]
    assert s["car_counts"] == {"bmw": 5, "audi": 2} and s["max_clients"] == 7 and s["cars"] == ["bmw", "audi"]   # the panel reads the same back


@pytest.mark.parametrize("counts", [{}, {"bmw": 0}, {"bmw": -1}, {"bmw": True}, {"bmw": 1.5}, {"bmw": "3"}, {"bmw": 51}, {"bmw": 30, "audi": 30}])
def test_car_counts_are_validated(counts):
    _install()
    sid = _server()
    assert api.post(f"{V}/servers/{sid}/apply", json={**COUNTS, "car_counts": counts}).status_code == 422


def test_zero_counts_are_dropped_and_old_clients_still_work():
    _install()
    sid = _server()
    r = api.post(f"{V}/servers/{sid}/apply", json={**COUNTS, "car_counts": {"bmw": 3, "audi": 0}})
    assert [e["MODEL"] for e in r.json()["entry_list"]] == ["bmw"] * 3 and r.json()["config"]["SERVER"]["CARS"] == "bmw"
    old = api.post(f"{V}/servers/{sid}/apply", json=FORM)   # cars + max_clients, in turns, as before
    assert [e["MODEL"] for e in old.json()["entry_list"]] == ["bmw", "audi", "bmw", "audi", "bmw"]


def test_reserved_entries_win_over_car_counts():
    _install()
    sid = _server()
    form = {**COUNTS, "entries": [{"model": "audi", "driver_name": "Ana"}]}
    r = api.post(f"{V}/servers/{sid}/apply", json=form)
    assert [e["MODEL"] for e in r.json()["entry_list"]] == ["audi"] and api.get(f"{V}/servers/{sid}").json()["session"]["car_counts"] is None


def test_setup_files_upload_list_replace_delete(tmp_path):
    assert _put("mazda_fixed").status_code == 201
    assert {"name": "mazda_fixed", "size": len(SETUP)} in api.get(f"{V}/content/setups").json()
    assert _put("mazda_fixed", SETUP + "[REAR]\n").status_code == 201                       # replacing is fine
    assert (content.setups_dir() / "mazda_fixed.ini").read_text().endswith("[REAR]\n")
    assert not list(content.setups_dir().glob("*.tmp"))
    assert api.delete(f"{V}/content/setups/mazda_fixed").status_code == 200
    assert api.delete(f"{V}/content/setups/mazda_fixed").status_code == 404


@pytest.mark.parametrize("name, data, status", [
    ("..x", SETUP, 400), ("name.ini", SETUP, 400), ("trailing%0A", SETUP, 400), ("a..b", SETUP, 400), (".hidden", SETUP, 400), ("with space", SETUP, 400), ("x" * 81, SETUP, 400),
    ("ok_name", b"", 400), ("ok_name", b"[A]\n\x00\n", 400), ("ok_name", b"\xff\xfe\x00bad", 400), ("ok_name", "x" * (256 * 1024 + 1), 413),
])
def test_setup_uploads_are_checked(name, data, status):
    assert _put(name, data).status_code == status
    assert not list(content.setups_dir().glob("*.tmp")) and not (content.setups_dir() / "ok_name.ini").exists()


def test_setups_are_admin_only():
    operator = _client("steward")
    assert operator.get(f"{V}/content/setups").status_code == 403
    assert _put("sneaky", client=operator).status_code == 403
    assert operator.delete(f"{V}/content/setups/x").status_code == 403
    assert not (content.setups_dir() / "sneaky.ini").exists()


def test_apply_writes_fixed_setup_only_where_asked_and_checks_it_exists():
    _install()
    sid = _server()
    r = api.post(f"{V}/servers/{sid}/apply", json={**COUNTS, "car_setups": {"bmw": "ghost"}})
    assert r.status_code == 400 and "ghost" in r.text
    assert _put("bmw_fixed").status_code == 201
    r = api.post(f"{V}/servers/{sid}/apply", json={**COUNTS, "car_setups": {"bmw": "bmw_fixed"}})
    assert r.status_code == 200, r.text
    assert [e.get("FIXED_SETUP") for e in r.json()["entry_list"]] == ["bmw_fixed.ini"] * 5 + [None] * 2   # acServer opens setups/<value>: the file name, with «.ini»
    ini = api.get(f"{V}/servers/{sid}/entry_list.ini").text
    assert ini.count("FIXED_SETUP=bmw_fixed.ini") == 5 and ini.count("FIXED_SETUP") == 5
    entries = [{"model": "audi", "driver_name": "Ana", "fixed_setup": "bmw_fixed"}, {"model": "bmw", "driver_name": "Beto"}]
    r = api.post(f"{V}/servers/{sid}/apply", json={**FORM, "cars": [], "entries": entries})
    assert [e.get("FIXED_SETUP") for e in r.json()["entry_list"]] == ["bmw_fixed.ini", None]


def test_a_setup_in_use_cannot_be_deleted():
    _install()
    sid = _server()
    assert _put("busy").status_code == 201
    assert api.post(f"{V}/servers/{sid}/apply", json={**FORM, "car_setups": {"bmw": "busy"}}).status_code == 200
    r = api.delete(f"{V}/content/setups/busy")
    assert r.status_code == 409 and "server" in r.text
    ev = api.post(f"{V}/events", json={"title": "Con setup", "session": {**FORM, "car_setups": {"audi": "busy"}}})
    assert ev.status_code in (200, 201), ev.text
    assert api.post(f"{V}/servers/{sid}/apply", json=FORM).status_code == 200   # the server lets go of it…
    assert "event" in api.delete(f"{V}/content/setups/busy").text                 # …the saved event still holds it
    api.delete(f"{V}/events/{ev.json()['id']}")
    assert api.delete(f"{V}/content/setups/busy").status_code == 200


def test_every_instance_links_the_shared_setups_folder(tmp_path, monkeypatch):
    bin_dir = tmp_path / "ac"
    for d in ("content", "system"):
        (bin_dir / d).mkdir(parents=True)
    monkeypatch.setattr(settings, "acserver_cmd", str(bin_dir / "acServer"))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    sid = _server()
    with Session(engine) as sess:
        d = server_service.write_instance(sess.get(Server, sid))
    assert (d / "setups").is_symlink() and (d / "setups").resolve() == (bin_dir / "setups").resolve() and (bin_dir / "setups").is_dir()


@pytest.mark.parametrize("bad", ["setup\n", "a\nFIXED_SETUP=b", "x.ini", ".x", "a b", ""])
def test_setup_names_cannot_carry_newlines_or_ini_keys(bad):
    _install()
    sid = _server()
    entry = {"model": "bmw", "driver_name": "Ana", "fixed_setup": bad}
    assert api.post(f"{V}/servers/{sid}/apply", json={**FORM, "cars": [], "entries": [entry]}).status_code == (200 if bad == "" else 422)
    if bad:
        assert api.post(f"{V}/servers/{sid}/apply", json={**FORM, "car_setups": {"bmw": bad}}).status_code == 422
