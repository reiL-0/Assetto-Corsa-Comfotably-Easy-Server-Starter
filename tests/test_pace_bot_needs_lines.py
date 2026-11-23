"""A session cannot name the pace bot on a layout that has no AI lines kept: the apply is refused (409) and says to install the track again; the catalogue tells the panel which layouts have them;
a scheduled event (held) goes without a pace car instead of failing to start."""
import shutil

from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content, pitentry
from app.config import settings
from app.main import app
from app.schemas.servers import SessionIn
from app.services import server_service as ss

V = "/api/v1"
api = TestClient(app, headers=ADMIN)
BASE = {"name": "Bot", "track": "botspa", "cars": ["botbmw"], "max_clients": 4, "race_laps": 3, "practice_min": 5, "restart": False}


def _setup():
    t, c = content._tracks_dir(), content._cars_dir()
    (t / "botspa" / "data").mkdir(parents=True, exist_ok=True)
    (t / "botspa" / "data" / "surfaces.ini").write_text("x")
    (c / "botbmw").mkdir(exist_ok=True)
    (c / "botbmw" / "data.acd").write_text("x")
    return api.post(f"{V}/servers", json={"name": "t"}).json()["id"]


def _no_lines(track, config=""):
    shutil.rmtree(pitentry.lines_path(track, config), ignore_errors=True)


def _keep_lines(track, config=""):
    d = pitentry.lines_path(track, config)
    d.mkdir(parents=True, exist_ok=True)
    for f in pitentry.LINES:
        (d / f).write_bytes(b"x")


def test_the_bot_is_refused_without_lines_and_allowed_with_them():
    sid = _setup()
    _no_lines("botspa")
    r = api.post(f"{V}/servers/{sid}/apply", json={**BASE, "pace_source": "bot"})
    assert r.status_code == 409 and "install the track again" in r.json()["detail"], r.text
    assert api.post(f"{V}/servers/{sid}/apply", json={**BASE, "pace_source": "real"}).status_code == 200, "a real driver or nobody never needs the lines"
    assert api.post(f"{V}/servers/{sid}/apply", json={**BASE, "pace_source": "off"}).status_code == 200
    _keep_lines("botspa")
    r = api.post(f"{V}/servers/{sid}/apply", json={**BASE, "pace_source": "bot"})
    assert r.status_code == 200, r.text


def test_the_catalogue_says_which_layouts_have_lines():
    _setup()
    _no_lines("botspa")
    tr = {t["track"]: t for t in content.list_tracks()}["botspa"]
    assert tr["base_ai_lines"] is False
    _keep_lines("botspa")
    assert {t["track"]: t for t in content.list_tracks()}["botspa"]["base_ai_lines"] is True
    (pitentry.lines_path("botspa", "") / "pit_lane.ai").unlink()
    assert {t["track"]: t for t in content.list_tracks()}["botspa"]["base_ai_lines"] is False, "one line alone is no pair"


def test_a_scheduled_session_goes_without_pace_car_instead_of_failing():
    body = SessionIn(**{**BASE, "track": "nolines", "pace_source": "bot"})
    assert ss.check_pace_lines(body, held=True).pace_source == "off"
    try:
        ss.check_pace_lines(body, held=False)
        assert False, "an admin's apply must be refused"
    except ss.ServerError as e:
        assert e.status == 409


def test_a_layout_counts_on_its_own_lines_not_the_tracks():
    sid = _setup()
    _no_lines("botspa"); _no_lines("botspa", "gp")
    (content._tracks_dir() / "botspa" / "gp" / "data").mkdir(parents=True, exist_ok=True)
    (content._tracks_dir() / "botspa" / "gp" / "data" / "surfaces.ini").write_text("x")
    _keep_lines("botspa", "gp")   # only the layout has them
    tr = {t["track"]: t for t in content.list_tracks()}["botspa"]
    assert tr["base_ai_lines"] is False and {c["config"]: c["ai_lines"] for c in tr["configs"] if c["config"]} == {"gp": True}
    ok = api.post(f"{V}/servers/{sid}/apply", json={**BASE, "track_config": "gp", "pace_source": "bot"})
    assert ok.status_code == 200, ok.text
    assert api.post(f"{V}/servers/{sid}/apply", json={**BASE, "track_config": "", "pace_source": "bot"}).status_code in (400, 409), "the base has no lines (or needs a layout): never accepted"


def test_a_raw_config_edit_cannot_move_a_bot_session_to_a_track_without_lines():
    sid = _setup()
    _keep_lines("botspa")
    assert api.post(f"{V}/servers/{sid}/apply", json={**BASE, "pace_source": "bot"}).status_code == 200
    srv = api.get(f"{V}/servers/{sid}").json()
    other = {**srv["config"], "SERVER": {**srv["config"]["SERVER"], "TRACK": "nolinesatall", "CONFIG_TRACK": ""}}
    r = api.patch(f"{V}/servers/{sid}", json={"name": srv["name"], "config": other, "entry_list": srv["entry_list"]})
    assert r.status_code == 409 and "install the track again" in r.json()["detail"], r.text
    same = {**srv["config"]}
    assert api.patch(f"{V}/servers/{sid}", json={"name": srv["name"], "config": same, "entry_list": srv["entry_list"]}).status_code == 200


def test_a_scheduled_apply_stores_the_session_without_the_bot():
    import asyncio
    from sqlmodel import Session
    from app.db import engine
    from app.models import Server
    sid = _setup()
    _no_lines("botspa")
    with Session(engine) as sess:
        srv = sess.get(Server, sid)
        body = SessionIn(**{**BASE, "pace_source": "bot"})
        asyncio.run(ss.apply(sess, srv, body, held=True))
        sess.refresh(srv)
        assert srv.session["pace_source"] == "off", srv.session.get("pace_source")
