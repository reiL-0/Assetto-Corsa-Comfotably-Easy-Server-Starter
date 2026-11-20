"""What an uploaded track tells about each of its layouts, and the AI lines given later to a layout that is already installed (POST /content/tracks/{track}/layouts/{layout}/ai)."""
import io
import zipfile

from conftest import ADMIN
from fastapi.testclient import TestClient
from test_pitentry import _track

from app import pitentry
from app.config import settings
from app.main import app

client = TestClient(app, headers=ADMIN)
API = "/api/v1/content/tracks"


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
    return buf.getvalue()


def _lines(lay):
    return {"fast_lane": ("fast_lane.ai", (lay / "ai" / "fast_lane.ai").read_bytes()), "pit_lane": ("pit_lane.ai", (lay / "ai" / "pit_lane.ai").read_bytes())}


def test_the_upload_says_what_each_layout_brought_and_the_ai_can_be_added_later(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    lay = _track(tmp_path / "src", "gp")
    files = {"rep_t/models_gp.ini": b"[M]", "rep_t/gp/data/surfaces.ini": b"[S]", "rep_t/gp/ai/fast_lane.ai": (lay / "ai" / "fast_lane.ai").read_bytes(),
             "rep_t/gp/ai/pit_lane.ai": (lay / "ai" / "pit_lane.ai").read_bytes(), "rep_t/short/data/surfaces.ini": b"[S]", "rep_t/short/ai/pit_lane.ai": b"junk"}
    r = client.post(API, files={"file": ("r.zip", _zip(files), "application/zip")})
    assert r.status_code == 201, r.text
    got = {x["layout"]: x for x in r.json()["layouts"]}
    assert got["gp"]["ai"] == "ok" and 0.6 <= got["gp"]["pit_entry"] <= 0.64 and got["gp"]["models_ini"] is True
    assert got["short"]["ai"] == "missing" and got["short"]["pit_entry"] is None and got["short"]["models_ini"] is False
    assert pitentry.read("rep_t", "short") is None
    # the lines of the layout that came without them, given later (no new upload of the track)
    ok = client.post(f"{API}/rep_t/layouts/short/ai", files=_lines(lay))
    assert ok.status_code == 200 and ok.json()["ai"] == "ok", ok.text
    assert pitentry.read("rep_t", "short") == ok.json()["pit_entry"] and (pitentry.lines_path("rep_t", "short") / "pit_lane.ai").is_file()
    kept = pitentry.read("rep_t", "short")
    # a bad pair is a 400 and keeps what was there; a layout that is not installed is a 404
    bad = client.post(f"{API}/rep_t/layouts/short/ai", files={"fast_lane": ("f.ai", b"nope"), "pit_lane": ("p.ai", b"nope")})
    assert bad.status_code == 400 and pitentry.read("rep_t", "short") == kept
    assert client.post(f"{API}/rep_t/layouts/ghost/ai", files=_lines(lay)).status_code == 404
    assert client.post(f"{API}/rep_t/layouts/.x/ai", files=_lines(lay)).status_code == 400
    assert client.post(f"{API}/rep_t/layouts/a..b/ai", files=_lines(lay)).status_code == 400   # (a name that has no place to be kept is refused, not answered 200)
