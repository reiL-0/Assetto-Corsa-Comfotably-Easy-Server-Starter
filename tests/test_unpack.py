import io
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import unpack, uploadguard
from app.config import settings
from app.main import app

api = TestClient(app, headers=ADMIN)


def _zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in entries.items():
            zf.writestr(n, b)
        if symlink:
            zi = zipfile.ZipInfo(symlink)
            zi.external_attr = (0o120777 << 16)   # a symbolic link
            zf.writestr(zi, "/etc/passwd")
    return buf.getvalue()


LAST: dict = {}


def _run(tmp_path, data: bytes, **kw):
    """Unpacks `data` into a fresh folder under tmp_path (kept in LAST["dest"] even when it raises)."""
    base = tempfile.mkdtemp(dir=tmp_path)
    arc, dest = os.path.join(base, "a.zip"), os.path.join(base, "out")
    open(arc, "wb").write(data)
    os.mkdir(dest)
    LAST["dest"] = dest
    from pathlib import Path
    return unpack.run_sandboxed(Path(arc), Path(dest), **kw), Path(dest)


def test_a_normal_archive_unpacks_in_the_child(tmp_path):
    res, dest = _run(tmp_path, _zip({"car/data.acd": b"x" * 100, "car/ui/ui_car.json": b"{}"}))
    assert res["files"] == 2 and (dest / "car" / "data.acd").read_bytes() == b"x" * 100


def test_the_byte_counter_stops_a_bomb_whatever_the_headers_say(tmp_path):
    # three 20 MB files of zeros: each is under the header-ratio check (50 MB), together they pass the limit set here
    data = _zip({f"c/z{i}": b"\0" * (20 * 1024**2) for i in range(3)})
    with pytest.raises(uploadguard.Rejected, match="more than the allowed size"):
        _run(tmp_path, data, max_total=50 * 1024**2)
    written = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(LAST["dest"]) for f in fs)
    assert written <= 50 * 1024**2 + 1024**2                                                           # it stopped writing at the limit
    with pytest.raises(uploadguard.Rejected, match="too many"):
        _run(tmp_path, _zip({f"c/{i}": b"1" for i in range(20)}), max_files=5)


def test_symlinks_and_unsafe_names_are_refused_in_the_child(tmp_path):
    with pytest.raises(uploadguard.Rejected, match="symbolic link"):
        _run(tmp_path, _zip({"c/a": b"1"}, symlink="c/link"))
    with pytest.raises(uploadguard.Rejected, match="unsafe"):
        _run(tmp_path, _zip({"../evil": b"1"}))


def _sleeper(monkeypatch, seconds: float):
    monkeypatch.setattr(unpack, "_command", lambda *a, **k: [sys.executable, "-c", f"import time; time.sleep({seconds})"])


def test_a_child_that_takes_too_long_is_killed(tmp_path, monkeypatch):
    _sleeper(monkeypatch, 30)
    t0 = time.time()
    with pytest.raises(uploadguard.Rejected, match="took longer than 1 s"):
        _run(tmp_path, _zip({"c/a": b"1"}), timeout=1)
    assert time.time() - t0 < 10


def test_a_child_killed_by_a_limit_without_answering_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(unpack, "_command", lambda *a, **k: [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"])
    with pytest.raises(uploadguard.Rejected, match="within the allowed resources"):
        _run(tmp_path, _zip({"c/a": b"1"}))


def test_only_one_unpacking_runs_at_a_time(tmp_path, monkeypatch):
    _sleeper(monkeypatch, 1)
    d1, d2 = tmp_path / "1", tmp_path / "2"
    d1.mkdir()
    d2.mkdir()
    errors, t0 = [], time.time()

    def go(d):
        try:
            unpack.run_sandboxed(tmp_path / "none.zip", d, timeout=10)
        except uploadguard.Rejected as e:   # the sleeper prints no JSON: that is fine, we only time the queue
            errors.append(str(e))
    ts = [threading.Thread(target=go, args=(d,)) for d in (d1, d2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert time.time() - t0 >= 1.9 and len(errors) == 2


def test_not_enough_free_disk_is_refused_before_unpacking(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 99, 1024**2))
    with pytest.raises(OSError, match="free disk"):
        _run(tmp_path, _zip({"c/a": b"1"}))


def test_the_upload_endpoint_reports_a_stopped_unpacking_as_a_400(monkeypatch):
    _sleeper(monkeypatch, 30)
    monkeypatch.setattr(settings, "unpack_timeout", 1)
    r = api.post("/api/v1/content/cars", files={"file": ("slow.zip", _zip({"slow/data.acd": b"1"}), "application/zip")})
    assert r.status_code == 400 and "took longer" in r.text


@pytest.mark.skipif(not shutil.which("bsdtar"), reason="needs bsdtar")
def test_a_non_rar_with_a_rar_name_is_not_trusted(tmp_path):
    arc = tmp_path / "x.rar"
    arc.write_bytes(b"Rar!\x1a\x07\x00" + os.urandom(64))
    dest = tmp_path / "o"
    dest.mkdir()
    with pytest.raises(uploadguard.Rejected):
        unpack.run_sandboxed(arc, dest)
