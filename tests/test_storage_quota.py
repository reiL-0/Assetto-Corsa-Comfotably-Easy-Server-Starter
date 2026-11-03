"""The plan's space is enforced on every way of uploading content (app/storage.check_room): a client can neither start nor keep sending past its quota; a download is cut at what is left; the check
counts again after an install; with no quota (the league's own manager) nothing is ever refused."""
import io
import zipfile

import pytest
from conftest import ADMIN
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import content, storage
from app.config import settings
from app.main import app

client = TestClient(app, headers=ADMIN)
MB = 1024 * 1024


@pytest.fixture
def plan(tmp_path, monkeypatch):
    ac = tmp_path / "acserver"
    (ac / "content" / "cars").mkdir(parents=True)
    (ac / "content" / "tracks").mkdir()
    (ac / "setups").mkdir()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(settings, "acserver_cmd", str(ac / "acServer"))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "storage_quota_mb", 5)   # a 5 MB plan
    monkeypatch.setattr(storage, "_cache", None)
    return ac


def fill(ac, mb):
    (ac / "content" / "cars" / "big.bin").write_bytes(b"x" * (mb * MB))
    storage.forget()


def car_zip(name="plan_car", pad=0):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(f"{name}/ui/ui_car.json", f'{{"name": "{name}", "brand": "X"}}')
        zf.writestr(f"{name}/data.acd", b"d" * 100)
        if pad:
            zf.writestr(f"{name}/skins/s/livery.png", b"p" * pad)
    return buf.getvalue()


def test_room_and_check_room_follow_the_plan(plan):
    assert storage.room() == 5 * MB
    storage.check_room(5 * MB)
    with pytest.raises(HTTPException) as e:
        storage.check_room(5 * MB + 1)
    assert e.value.status_code == 413 and "No queda espacio en tu plan" in e.value.detail and "GB" in e.value.detail
    fill(plan, 4)
    assert 0 < storage.room() < 1 * MB + 1024 * 1024
    fill(plan, 6)
    assert storage.room() == 0


def test_no_quota_never_refuses(plan, monkeypatch):
    monkeypatch.setattr(settings, "storage_quota_mb", 0)
    storage.forget()
    assert storage.room() is None
    storage.check_room(10 ** 13)
    assert client.post("/api/v1/content/uploads", json={"kind": "car"}).status_code == 201


def test_a_full_plan_refuses_to_even_start(plan):
    fill(plan, 6)
    r = client.post("/api/v1/content/uploads", json={"kind": "car"})
    assert r.status_code == 413 and "No queda espacio" in r.json()["detail"]
    r = client.post("/api/v1/content/uploads/from-link", json={"url": "https://www.mediafire.com/file/abc/car.zip/file", "kind": "car"})
    assert r.status_code == 413, r.text


def test_a_chunked_upload_is_stopped_where_it_would_pass_the_plan(plan):
    fill(plan, 3)   # ~2 MB left
    uid = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    ok = client.put(f"/api/v1/content/uploads/{uid}?offset=0", content=b"x" * MB, headers={**ADMIN, "Content-Type": "application/octet-stream"})
    assert ok.status_code == 200 and ok.json()["size"] == MB
    over = client.put(f"/api/v1/content/uploads/{uid}?offset={MB}", content=b"x" * (3 * MB), headers={**ADMIN, "Content-Type": "application/octet-stream"})
    assert over.status_code == 413 and "No queda espacio" in over.json()["detail"], over.text
    assert client.get(f"/api/v1/content/uploads/{uid}").json()["size"] == MB, "the refused part was written"


def test_a_direct_upload_over_the_plan_is_refused_and_leaves_no_temporary_file(plan):
    fill(plan, 4)
    scratch = content._scratch()
    before = set(scratch.glob("*.upload"))
    r = client.post("/api/v1/content/cars", files={"file": ("c.zip", car_zip(pad=3 * MB), "application/zip")})
    assert r.status_code == 413 and "No queda espacio" in r.json()["detail"], r.text
    assert set(scratch.glob("*.upload")) == before, "a refused upload left its temporary file"
    assert not (plan / "content" / "cars" / "plan_car").exists()


def test_an_upload_that_fits_installs_and_the_count_moves(plan):
    before = storage.current()["used_bytes"]
    r = client.post("/api/v1/content/cars", files={"file": ("c.zip", car_zip(), "application/zip")})
    assert r.status_code == 201, r.text
    assert storage.current()["used_bytes"] > before, "the space was not counted again after the install"


def test_a_download_is_cut_at_what_the_plan_has_left(plan, monkeypatch):
    fill(plan, 4)
    seen = []
    monkeypatch.setattr(content.download, "fetch", lambda opener, path, limit, progress: seen.append(limit) or (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(content.download, "resolve", lambda url, op: url)
    monkeypatch.setattr(content.download, "opener", lambda: None)
    content._uploads["dl1"] = {"path": content._scratch() / "dl1", "kind": "car", "requested_kind": "car", "state": "downloading", "size": 0, "total": None}
    content._fetch_then_finish("dl1", "https://x.test/a.zip")
    assert seen and 0 < seen[0] < 2 * MB, seen


def test_the_inbox_import_checks_room_too(plan, monkeypatch, tmp_path):
    fill(plan, 4)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "t.zip").write_bytes(b"z" * (3 * MB))
    monkeypatch.setattr(content, "inbox_dir", lambda: inbox)
    r = client.post("/api/v1/content/tracks/import", json={"file": "t.zip"})
    assert r.status_code == 413
