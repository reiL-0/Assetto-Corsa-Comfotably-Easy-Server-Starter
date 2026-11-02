"""The storage card's numbers (app/storage.py): every part is counted from the real files, a link is never followed, the quota is only shown, the walk is cached, admin only."""
import os

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content, storage
from app.config import settings
from app.main import app

client = TestClient(app, headers=ADMIN)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    ac = tmp_path / "acserver"
    (ac / "content" / "cars" / "c1").mkdir(parents=True)
    (ac / "content" / "tracks" / "t1").mkdir(parents=True)
    (ac / "setups").mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (ac / "content" / "cars" / "c1" / "a.kn5").write_bytes(b"x" * 1000)
    (ac / "content" / "cars" / "c1" / "b.ini").write_bytes(b"x" * 24)
    (ac / "content" / "tracks" / "t1" / "t.kn5").write_bytes(b"x" * 5000)
    (ac / "setups" / "s.ini").write_bytes(b"x" * 10)
    (data / "acmanager.db").write_bytes(b"x" * 300)
    (ac / "acServer").write_bytes(b"x" * 700)
    monkeypatch.setattr(settings, "acserver_cmd", str(ac / "acServer"))
    monkeypatch.setattr(settings, "data_dir", str(data))
    monkeypatch.setattr(settings, "storage_quota_mb", 0)
    monkeypatch.setattr(storage, "_cache", None)
    return ac, data


def test_every_part_is_counted_from_the_files(tree):
    r = client.get("/api/v1/storage")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["parts"] == {"cars": 1024, "tracks": 5000, "setups": 10, "data": 300, "program": 700}, d["parts"]
    assert d["used_bytes"] == 1024 + 5000 + 10 + 300 + 700 and d["quota_bytes"] is None
    assert d["counted_at"] > 0 and set(d) == {"used_bytes", "quota_bytes", "parts", "counted_at"}, "the machine's disk figures reach the client"
    assert "disk" not in r.text and "free" not in r.text


def test_a_symbolic_link_is_never_followed(tree, tmp_path):
    ac, _ = tree
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge").write_bytes(b"x" * 99999)
    os.symlink(outside, ac / "content" / "cars" / "linked")
    os.symlink(outside / "huge", ac / "content" / "cars" / "c1" / "alias")
    assert client.get("/api/v1/storage").json()["parts"]["cars"] == 1024, "a link led the count outside the content folder"


def test_the_quota_is_shown_and_nothing_else(tree, monkeypatch):
    monkeypatch.setattr(settings, "storage_quota_mb", 40 * 1024)
    assert client.get("/api/v1/storage").json()["quota_bytes"] == 40 * 1024 * 1024 * 1024


def test_the_walk_is_cached_and_one_at_a_time(tree, monkeypatch):
    calls = []
    real = storage.measure
    monkeypatch.setattr(storage, "measure", lambda: calls.append(1) or real())
    first = client.get("/api/v1/storage").json()
    (tree[0] / "content" / "cars" / "c1" / "new.kn5").write_bytes(b"x" * 500)
    assert client.get("/api/v1/storage").json() == first and len(calls) == 1, "the disk was walked again inside the cache period"
    monkeypatch.setattr(storage, "CACHE_SECONDS", -1)
    assert client.get("/api/v1/storage").json()["parts"]["cars"] == 1524 and len(calls) == 2


def test_a_missing_folder_counts_zero_and_never_fails(tree):
    import shutil
    shutil.rmtree(tree[0] / "content" / "tracks")
    (tree[0] / "acServer").unlink()
    d = client.get("/api/v1/storage").json()
    assert d["parts"]["tracks"] == 0 and d["parts"]["program"] == 0 and d["parts"]["cars"] == 1024


def test_admin_only(tree):
    anon = TestClient(app)
    assert anon.get("/api/v1/storage").status_code in (401, 403)
