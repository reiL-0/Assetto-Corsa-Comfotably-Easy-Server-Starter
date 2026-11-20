"""Car families: an event names the family, the session's `variant` picks the folder; skins go to every variant; the variant is frozen once scheduled."""
import io
import zipfile

from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content
from app.db import engine
from app.main import app
from app.models import Schedule
from sqlmodel import Session

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
FAM = {"cvcamry": {"name": "Stock Car Camry", "variants": {"oval": "cvcamry_oval", "road": "cvcamry_road"}},
       "cvcamaro": {"name": "Stock Car Camaro", "variants": {"oval": "cvcamaro_oval", "road": "cvcamaro_road"}}}
BASE = {"name": "Liga", "track": "cvspa", "cars": ["cvcamry", "cvcamaro"], "max_clients": 4, "race_laps": 5, "car_setups": {"cvcamry": "cvs"}}


def _install():
    t, c = content._tracks_dir(), content._cars_dir()
    (t / "cvspa" / "data").mkdir(parents=True, exist_ok=True)
    (t / "cvspa" / "data" / "surfaces.ini").write_text("x")
    for car in [d for f in FAM.values() for d in f["variants"].values()]:
        (c / car).mkdir(exist_ok=True)
        (c / car / "data.acd").write_text("x")
    (content.setups_dir() / "cvs.ini").write_text("x")
    assert api.put(f"{V}/content/car-families", json=FAM).status_code == 200


def _run(session):
    eid = api.post(f"{V}/events", json={"title": "t", "session": session}).json()["id"]
    sid = api.post(f"{V}/servers", json={"name": "t"}).json()["id"]
    return eid, api.post(f"{V}/events/{eid}/run", json={"server_id": sid, "restart": False})


def test_variant_picks_the_folder_of_every_family():
    _install()
    eid, r = _run({**BASE, "variant": "oval"})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["SERVER"]["CARS"] == "cvcamry_oval;cvcamaro_oval"
    assert api.get(f"{V}/events/{eid}").json()["session"]["cars"] == ["cvcamry", "cvcamaro"], "the saved event keeps the family ids"
    _, r = _run({**BASE, "variant": "road", "car_counts": {"cvcamry": 2, "cvcamaro": 1}, "cars": []})
    assert r.status_code == 200 and r.json()["config"]["SERVER"]["CARS"] == "cvcamry_road;cvcamaro_road"
    for bad in ("", "nope"):   # a family with no (or an unknown) variant is refused, never run as a missing folder
        assert _run({**BASE, "variant": bad})[1].status_code == 400
    assert _run({**BASE, "cars": ["cvcamry_oval"], "car_setups": {}})[1].status_code == 200, "a plain car folder still goes through"


def test_skin_uploaded_to_a_family_reaches_every_variant():
    _install()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack/blue/ui_skin.json", '{"skinname": "x"}')
    r = api.post(f"{V}/content/cars/cvcamry/skins", files={"file": ("p.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 201, r.text
    for f in ("cvcamry_oval", "cvcamry_road"):
        assert (content._cars_dir() / f / "skins" / "blue" / "ui_skin.json").is_file()
    assert not (content._cars_dir() / "cvcamaro_oval" / "skins" / "blue").exists()
    assert api.get(f"{V}/content/car-families").json()["cvcamry"]["skins"] == ["blue"]


def test_variant_is_frozen_once_scheduled_and_the_file_is_validated():
    _install()
    eid = api.post(f"{V}/events", json={"title": "f", "session": {**BASE, "variant": "oval"}}).json()["id"]
    put = lambda v: api.put(f"{V}/events/{eid}", json={"title": "f", "session": {**BASE, "variant": v}})
    assert put("road").status_code == 200, "free to correct while it is not scheduled"
    with Session(engine) as s:
        s.add(Schedule(event_id=eid, server_id=1, start_at=4102444800))
        s.commit()
    assert put("oval").status_code == 409
    assert put("road").status_code == 200, "the same variant is not a change"
    assert api.put(f"{V}/content/car-families", json={"x": {"variants": {"oval": "not-installed"}}}).status_code == 422
    assert api.put(f"{V}/content/car-families", json={"cvcamry_oval": {"variants": {"oval": "cvcamry_oval"}}}).status_code == 422, "a family id cannot be a folder"
    for bad_id in ("", "a/b", "../x"):
        assert api.put(f"{V}/content/car-families", json={bad_id: {"variants": {"oval": "cvcamry_oval"}}}).status_code == 422, bad_id
    link = content._cars_dir() / "cvlink"
    link.symlink_to(content._cars_dir() / "cvcamry_oval", target_is_directory=True)
    assert api.put(f"{V}/content/car-families", json={"cvl": {"variants": {"oval": "cvlink"}}}).status_code == 422, "a linked folder is not a variant"
    link.unlink()
    assert api.put(f"{V}/content/car-families", json=FAM).status_code == 200


def test_the_wake_preview_lists_the_real_folders():
    """What Content Manager sees while the server sleeps with an event pending: the folders that will run, not a family id."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from app import wake
    _install()
    eid = api.post(f"{V}/events", json={"title": "w", "session": {**BASE, "variant": "road", "cars": ["cvcamry"], "car_setups": {}}}).json()["id"]
    sc = SimpleNamespace(loaded=False, event_id=eid, start_at=4102444800)
    with patch.object(wake.schedule, "open_window", return_value=sc):
        plan = wake._preview(SimpleNamespace(id=1, wake="always"))
    assert plan is not None and plan.cars == ["cvcamry_road"]
