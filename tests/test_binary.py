"""The acServer binary upload (app/binary.py): only a real ELF executable gets installed, bare or inside a zip; a Windows .exe, text, a damaged zip and a zip with no acServer are refused with a sentence;
only `acServer` and `system/` leave a zip; nothing is installed while a server runs; admin only; the install is atomic and executable; a failed upload leaves the old program in place."""
import io
import os
import shlex
import stat
import zipfile

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import binary, supervisor
from app.config import settings
from app.main import app

client = TestClient(app, headers=ADMIN)


def elf(machine=3, bits=1, kind=2, payload=b"program"):
    head = b"\x7fELF" + bytes([bits, 1, 1, 0]) + b"\0" * 8 + kind.to_bytes(2, "little") + machine.to_bytes(2, "little")
    return head + b"\0" * 12 + payload


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def server_dir(tmp_path, monkeypatch):
    d = tmp_path / "acserver"
    d.mkdir()
    monkeypatch.setattr(settings, "acserver_cmd", str(d / "acServer"))
    monkeypatch.setattr(supervisor, "any_running", lambda: False)
    return d


def post(data):
    return client.post("/api/v1/binary", content=data, headers={**ADMIN, "Content-Type": "application/octet-stream"})


def test_a_bare_elf_is_installed_executable_and_reported(server_dir):
    assert client.get("/api/v1/binary").json()["installed"] is False
    r = post(elf())
    assert r.status_code == 201 and r.json()["arch"] == "x86 (32-bit)" and r.json()["installed"], r.text
    p = server_dir / "acServer"
    assert p.read_bytes() == elf() and p.stat().st_mode & stat.S_IXUSR and p.stat().st_mode & stat.S_IXOTH, "not executable by the acServer user"
    st = client.get("/api/v1/binary").json()
    assert st["installed"] and st["sha256"] == r.json()["sha256"] and st["size"] == len(elf()) and not list(server_dir.glob(".binary-*")) and not (server_dir / "acServer.new").exists()
    assert post(elf(machine=62, bits=2)).json()["arch"] == "x86-64"


@pytest.mark.parametrize("data,why", [
    (b"MZ" + b"\0" * 100, "Windows program"), (b"hello", "not the acServer"), (b"", "empty"), (elf(machine=40), "not an executable for x86"),
    (elf(kind=1), "not an executable for x86"), (elf(machine=3, bits=2), "not an executable for x86"), (b"\x7fELF" + b"\x01\x02" + b"\0" * 30, "kind of server"),
])
def test_what_is_not_an_executable_is_refused_and_the_old_program_stays(server_dir, data, why):
    (server_dir / "acServer").write_bytes(elf(payload=b"old"))
    r = post(data)
    assert r.status_code == 422 and why in r.json()["detail"], (r.status_code, r.text)
    assert (server_dir / "acServer").read_bytes() == elf(payload=b"old"), "a refused upload replaced the program"


def test_a_zip_gives_only_acserver_and_system(server_dir):
    z = make_zip({"Assetto Corsa Dedicated Server/acServer": elf(), "Assetto Corsa Dedicated Server/acServer.exe": b"MZ", "Assetto Corsa Dedicated Server/system/data/surfaces.ini": "[SURFACE_0]",
                  "Assetto Corsa Dedicated Server/content/cars/x/data.acd": "car", "Assetto Corsa Dedicated Server/cfg/server_cfg.ini": "[SERVER]", "Assetto Corsa Dedicated Server/plugins/p.dll": "dll"})
    r = post(z)
    assert r.status_code == 201 and r.json()["system"] is True, r.text
    assert (server_dir / "acServer").read_bytes() == elf() and (server_dir / "system" / "data" / "surfaces.ini").read_text() == "[SURFACE_0]"
    assert sorted(p.name for p in server_dir.iterdir()) == ["acServer", "system"], "something else came out of the zip"
    # a second install replaces system/ as a whole (no stale files)
    post(make_zip({"acServer": elf(payload=b"v2"), "system/data/other.ini": "x"}))
    assert (server_dir / "acServer").read_bytes() == elf(payload=b"v2") and not (server_dir / "system" / "data" / "surfaces.ini").exists() and (server_dir / "system" / "data" / "other.ini").exists()
    assert sorted(p.name for p in server_dir.iterdir()) == ["acServer", "system"]


def test_bad_zips_are_refused_with_a_sentence(server_dir):
    assert "no file named" in post(make_zip({"readme.txt": "x", "acServer.exe": b"MZ"})).json()["detail"]
    assert "damaged" in post(b"PK\x03\x04garbage").json()["detail"]
    assert "Windows" in post(make_zip({"acServer": b"MZ\x90"})).json()["detail"]
    r = post(make_zip({"acServer": elf(), "system/../../evil": "x"}))
    assert r.status_code == 422 and not (server_dir.parent / "evil").exists() and not (server_dir / "acServer").exists(), "a path in system/ left the folder"
    assert post(make_zip({"acServer": elf(), "system//a": "x"})).status_code == 422
    assert post(make_zip({"acServer": elf(), "system/C:/x": "x"})).status_code == 422
    assert not (server_dir / "acServer").exists()


def test_the_system_folder_is_capped(server_dir, monkeypatch):
    monkeypatch.setattr(binary, "MAX_SYSTEM_FILES", 3)
    assert post(make_zip({"acServer": elf(), **{f"system/f{i}": "x" for i in range(4)}})).status_code == 422
    monkeypatch.setattr(binary, "MAX_SYSTEM_FILES", 400); monkeypatch.setattr(binary, "MAX_SYSTEM_BYTES", 10)
    assert post(make_zip({"acServer": elf(), "system/big": "x" * 11})).status_code == 422


def test_too_big_an_upload_is_cut_off_with_advice(server_dir, monkeypatch):
    monkeypatch.setattr(binary, "MAX_UPLOAD", 1000)
    r = post(b"x" * 2000)
    assert r.status_code == 413 and "zip only" in r.json()["detail"] and not list(server_dir.iterdir())


def test_nothing_is_installed_while_a_server_runs(server_dir, monkeypatch):
    (server_dir / "acServer").write_bytes(elf(payload=b"old"))
    monkeypatch.setattr(supervisor, "any_running", lambda: True)
    r = post(elf(payload=b"new"))
    assert r.status_code == 409 and "stop every server" in r.json()["detail"] and (server_dir / "acServer").read_bytes() == elf(payload=b"old")


def test_admin_only_and_needs_a_configured_path(server_dir, monkeypatch):
    anon = TestClient(app)
    assert anon.get("/api/v1/binary").status_code in (401, 403) and anon.post("/api/v1/binary", content=elf()).status_code in (401, 403)
    monkeypatch.setattr(settings, "acserver_cmd", "")
    assert post(elf()).status_code == 409 and client.get("/api/v1/binary").json() == {"installed": False, "configured": False}


def test_the_program_runs_in_the_instance_that_starts_it(server_dir):
    """What a start needs from the file: an executable name that acserver_cmd resolves to (the supervisor reads settings.acserver_cmd, which is this path)."""
    post(elf())
    assert os.access(shlex.split(settings.acserver_cmd)[0], os.X_OK)


def test_status_never_shows_the_servers_absolute_path(server_dir):
    post(elf())
    assert str(server_dir) not in client.get("/api/v1/binary").text and str(server_dir) not in post(elf()).text


def test_a_failed_swap_leaves_the_old_program_and_the_old_system_folder(server_dir, monkeypatch):
    post(make_zip({"acServer": elf(payload=b"v1"), "system/data/old.ini": "old"}))
    real = os.replace

    def flaky(src, dst, *a, **kw):
        if str(dst) == str(server_dir / "acServer"):
            raise OSError("disk full")   # the program cannot be put in place AFTER system/ was swapped
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(binary.os, "replace", flaky)
    r = TestClient(app, headers=ADMIN, raise_server_exceptions=False).post("/api/v1/binary", content=make_zip({"acServer": elf(payload=b"v2"), "system/data/new.ini": "new"}),
                                                                            headers={**ADMIN, "Content-Type": "application/octet-stream"})
    assert r.status_code == 500
    monkeypatch.setattr(binary.os, "replace", real)
    assert (server_dir / "acServer").read_bytes() == elf(payload=b"v1") and (server_dir / "system" / "data" / "old.ini").exists() and not (server_dir / "system" / "data" / "new.ini").exists(), "program and resources no longer match"
    assert sorted(p.name for p in server_dir.iterdir()) == ["acServer", "system"], "a temporary file was left behind"
    assert not supervisor.program_lock.locked(), "the install lock was not released"


def test_two_installs_at_once_never_corrupt_the_program(server_dir):
    import threading
    results = []

    def go(tag):
        results.append(post(elf(payload=tag * 4000)).status_code)
    ts = [threading.Thread(target=go, args=(bytes([65 + i]),)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert set(results) <= {201, 409} and 201 in results, results
    data = (server_dir / "acServer").read_bytes()
    assert data[:4] == b"\x7fELF" and len(data) == len(elf(payload=b"A" * 4000)) and len(set(data[32:])) == 1, "the program is a mix of two uploads"
    assert sorted(p.name for p in server_dir.iterdir()) == ["acServer"] and not supervisor.program_lock.locked()


def test_a_server_cannot_start_while_the_program_is_being_installed(server_dir):
    import asyncio
    assert supervisor.program_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="being installed"):
            asyncio.run(supervisor.start(987, server_dir))
    finally:
        supervisor.program_lock.release()
