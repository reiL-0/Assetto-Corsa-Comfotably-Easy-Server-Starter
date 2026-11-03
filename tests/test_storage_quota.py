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


def test_the_inbox_import_refuses_when_the_plan_is_already_full(plan, monkeypatch, tmp_path):
    fill(plan, 6)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "t.zip").write_bytes(b"z" * MB)
    monkeypatch.setattr(content, "inbox_dir", lambda: inbox)
    assert client.post("/api/v1/content/tracks/import", json={"file": "t.zip"}).status_code == 413


def put(uid, offset, data):
    return client.put(f"/api/v1/content/uploads/{uid}?offset={offset}", content=data, headers={**ADMIN, "Content-Type": "application/octet-stream"})


def test_uploads_running_side_by_side_cannot_jointly_pass_the_plan(plan):
    a = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    b = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    assert put(a, 0, b"x" * (3 * MB)).status_code == 200   # 3 MB of a 5 MB plan: fits alone
    r = put(b, 0, b"x" * (3 * MB))                          # ...and so would this one alone, but not next to the bytes the first has already written
    assert r.status_code == 413 and "No queda espacio" in r.json()["detail"], "two uploads at once passed the quota between them"
    assert put(b, 0, b"x" * MB).status_code == 200          # what is left (2 MB) still fits


def test_a_finished_or_failed_upload_gives_its_room_back(plan):
    a = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    b = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    assert put(a, 0, b"not an archive" * (200 * 1024)).status_code == 200   # ~2.8 MB
    assert put(b, 0, b"x" * (3 * MB)).status_code == 413
    client.post(f"/api/v1/content/uploads/{a}/complete")   # it fails (not an archive) and its file is deleted
    import time as _t
    for _ in range(100):
        if client.get(f"/api/v1/content/uploads/{a}").json()["state"] in ("done", "error"):
            break
        _t.sleep(0.05)
    assert put(b, 0, b"x" * (3 * MB)).status_code == 200, "a failed upload kept its room"
    r = client.post("/api/v1/content/cars", files={"file": ("c.zip", b"junk", "application/zip")})
    assert r.status_code in (400, 422) and not list(content._scratch().glob("*.upload")), "a refused direct upload left its file"


def test_an_abandoned_upload_is_cleaned_after_hours_and_counts_until_then(plan):
    import os as _os
    a = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    assert put(a, 0, b"x" * (4 * MB)).status_code == 200
    b = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    assert put(b, 0, b"x" * (3 * MB)).status_code == 413, "abandoned bytes in the unpacking area were not counted"
    for f in content._scratch().iterdir():
        _os.utime(f, (1, 1))   # untouched since 1970
    assert put(b, 0, b"x" * (3 * MB)).status_code == 200, "the abandoned upload was never cleaned"
    assert not [f for f in content._scratch().iterdir() if f.stat().st_size == 4 * MB]


def test_the_unpacking_area_is_counted_once(plan):
    uid = client.post("/api/v1/content/uploads", json={"kind": "car"}).json()["id"]
    assert put(uid, 0, b"x" * (2 * MB)).status_code == 200
    storage.forget()   # the cache is taken again while the upload is half done
    assert storage.current()["parts"]["data"] < MB, "the unpacking area is counted in the installed content"
    assert put(uid, 2 * MB, b"x" * (2 * MB)).status_code == 200, "an upload that fits was refused because its own bytes were counted twice"
    assert put(uid, 4 * MB, b"x" * (2 * MB)).status_code == 413


def test_a_small_archive_that_expands_into_kept_files_cannot_pass_the_plan(plan):
    big = car_zip(pad=0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("fat_car/ui/ui_car.json", '{"name": "F", "brand": "X"}')
        zf.writestr("fat_car/data.acd", b"\0" * (7 * MB))   # kept by the server pack; compresses to a few KB
    assert len(buf.getvalue()) < 100 * 1024 and len(big) < 5 * MB
    r = client.post("/api/v1/content/cars", files={"file": ("f.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 413 and "No queda espacio" in r.json()["detail"], r.text
    assert not (plan / "content" / "cars" / "fat_car").exists(), "it was installed past the plan"


def test_an_inbox_archive_is_not_counted_twice(plan, monkeypatch, tmp_path):
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "t.zip").write_bytes(b"z" * int(3.5 * MB))   # already on the data disk, so already in the count
    monkeypatch.setattr(content, "inbox_dir", lambda: inbox)
    storage.forget()
    r = client.post("/api/v1/content/tracks/import", json={"file": "t.zip"})
    assert r.status_code != 413, "the inbox archive was counted a second time as if it were being uploaded"


def test_replacing_a_folder_counts_only_the_difference(plan):
    fill(plan, 0)
    d = plan / "content" / "cars" / "same_car"
    d.mkdir()
    (d / "data.acd").write_bytes(b"x" * (4 * MB))
    storage.forget()
    storage.check_install(4 * MB + 100 * 1024, replacing=d)    # 4 MB already there: only 100 KB more
    with pytest.raises(HTTPException):
        storage.check_install(6 * MB, replacing=d)             # 2 MB more would pass the 5 MB plan
    with pytest.raises(HTTPException):
        storage.check_install(2 * MB, replacing=None)          # a new folder next to the 4 MB: 6 MB


def test_a_download_stops_when_the_plan_fills_up_while_it_runs(plan, monkeypatch):
    def fake_fetch(opener, path, limit, progress):
        path.write_bytes(b"x" * (6 * MB))   # other uploads (or this one) filled the plan meanwhile
        progress(6 * MB, None)
        raise AssertionError("the download went on after the plan was full")
    monkeypatch.setattr(content.download, "fetch", fake_fetch)
    monkeypatch.setattr(content.download, "resolve", lambda url, op: url)
    monkeypatch.setattr(content.download, "opener", lambda: None)
    p = content._scratch() / "dl2"
    content._uploads["dl2"] = {"path": p, "kind": "car", "requested_kind": "car", "state": "downloading", "size": 0, "total": None}
    content._fetch_then_finish("dl2", "https://x.test/a.zip")
    u = content._uploads["dl2"]
    assert u["state"] == "error" and "No queda espacio" in u["error"], u
    assert not p.exists(), "the partial download was left behind"
