import hashlib
from pathlib import Path

import pytest
from conftest import ADMIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import binaries, servers
from app.config import settings
from app.db import engine
from app.main import app

api = TestClient(app, headers=ADMIN)
V = "/api/v1"


def _install(tmp_path: Path, name: str, linux: bytes, windows: bytes | None = None) -> Path:
    d = tmp_path / name
    (d / "system").mkdir(parents=True)
    (d / "acServer").write_bytes(linux)
    if windows is not None:
        (d / "acServer.exe").write_bytes(windows)
    return d


def test_register_installed_hashes_both_platforms_and_refuses_duplicates(tmp_path, monkeypatch):
    d = _install(tmp_path, "v1", b"linux-build-1", b"windows-build-1")
    monkeypatch.setattr(settings, "acserver_cmd", str(d / "acServer"))
    r = api.post(f"{V}/binaries/register-installed", json={"version": "9.1"})
    assert r.status_code == 201, r.text
    rows = {x["platform"]: x for x in r.json()}
    assert rows["linux"]["sha256"] == hashlib.sha256(b"linux-build-1").hexdigest() and rows["linux"]["dir"] == str(d)
    assert rows["windows"]["sha256"] == hashlib.sha256(b"windows-build-1").hexdigest() and rows["windows"]["dir"] == ""
    assert not rows["linux"]["verified"]                                                   # hashed from our own folder, not from a clean install
    assert api.post(f"{V}/binaries/register-installed", json={"version": "9.1"}).status_code == 409


def test_verify_hashes_an_upload_and_says_if_it_is_a_known_build(tmp_path):
    d = _install(tmp_path, "v2", b"known-linux", b"known-windows")
    api.post(f"{V}/binaries", json={"version": "9.2", "platform": "windows", "sha256": hashlib.sha256(b"known-windows").hexdigest()})
    ok = api.post(f"{V}/binaries/verify", files={"file": ("acServer.exe", b"known-windows", "application/octet-stream")}).json()
    assert ok["known"] and ok["matches"][0]["version"] == "9.2" and ok["size"] == len(b"known-windows")
    bad = api.post(f"{V}/binaries/verify", files={"file": ("acServer.exe", b"modified-by-someone", "application/octet-stream")}).json()
    assert bad["known"] is False and bad["matches"] == [] and bad["sha256"] == hashlib.sha256(b"modified-by-someone").hexdigest()
    assert api.post(f"{V}/binaries", json={"version": "x", "platform": "linux", "sha256": "nothex"}).status_code == 422
    assert api.post(f"{V}/binaries", json={"version": "x", "platform": "linux", "sha256": "0" * 64, "dir": str(tmp_path / "nope")}).status_code == 400
    assert d.exists()


def test_a_server_runs_the_version_it_was_given_and_its_system_folder_follows(tmp_path, monkeypatch):
    v1, v2 = _install(tmp_path, "a", b"A-linux"), _install(tmp_path, "b", b"B-linux", b"B-win")
    monkeypatch.setattr(settings, "acserver_cmd", str(v1 / "acServer"))
    b2 = api.post(f"{V}/binaries", json={"version": "B", "platform": "linux", "sha256": hashlib.sha256(b"B-linux").hexdigest(), "dir": str(v2)}).json()
    win = api.post(f"{V}/binaries", json={"version": "B", "platform": "windows", "sha256": hashlib.sha256(b"B-win").hexdigest()}).json()
    sid = api.post(f"{V}/servers", json={"name": "Versions"}).json()["id"]
    assert api.get(f"{V}/servers/{sid}").json()["binary_id"] is None
    assert api.put(f"{V}/servers/{sid}/binary", json={"binary_id": win["id"]}).status_code == 409      # a Windows build cannot run here
    assert api.put(f"{V}/servers/{sid}/binary", json={"binary_id": 99999}).status_code == 409
    assert api.put(f"{V}/servers/{sid}/binary", json={"binary_id": b2["id"]}).json()["binary_id"] == b2["id"]
    with Session(engine) as sess:
        s = sess.get(servers.Server, sid)
        assert binaries.command_for(sess, s.acserver_binary_id).strip("'") == str(v2 / "acServer")
        inst = servers._write_instance(s, binaries.dir_for(sess, s.acserver_binary_id))
        assert (inst / "system").resolve() == (v2 / "system").resolve()
        s.acserver_binary_id = None                                                                    # back to the global install
        assert binaries.command_for(sess, None) == str(v1 / "acServer")
        inst = servers._write_instance(s, settings.acserver_dir())
        assert (inst / "system").resolve() == (v1 / "system").resolve()
    with pytest.raises(HTTPException):
        with Session(engine) as sess:
            binaries.dir_for(sess, win["id"])
