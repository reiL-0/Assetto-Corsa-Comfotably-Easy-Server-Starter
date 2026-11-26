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


def test_only_a_zip_with_one_folder_is_accepted_no_rar_and_no_loose_files():
    rar = b"Rar!\x1a\x07\x00" + b"x" * 64
    r = api.post("/api/v1/content/cars", files={"file": ("a.rar", rar, "application/octet-stream")})
    assert r.status_code == 400 and "only .zip" in r.text
    r = api.post("/api/v1/content/cars", files={"file": ("data.acd", b"just a loose file", "application/octet-stream")})
    assert r.status_code == 400 and "only .zip" in r.text
    loose = api.post("/api/v1/content/cars", files={"file": ("l.zip", _zip({"car/data.acd": b"1", "readme.txt": b"loose beside the folder"}), "application/zip")})
    assert loose.status_code == 400 and "exactly one folder" in loose.text
    only_files = api.post("/api/v1/content/cars", files={"file": ("f.zip", _zip({"data.acd": b"1", "ui_car.json": b"{}"}), "application/zip")})
    assert only_files.status_code == 400 and "exactly one folder" in only_files.text


def _rar5(entries: dict[str, bytes]) -> bytes:
    """A minimal RAR5 archive with stored (uncompressed) members, written by hand: no `rar` program is needed to test the RAR path."""
    import zlib

    def vint(n: int) -> bytes:
        out = bytearray()
        while True:
            b, n = n & 0x7F, n >> 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def block(body: bytes, data: bytes = b"") -> bytes:
        head = vint(len(body)) + body
        return zlib.crc32(head).to_bytes(4, "little") + head + data

    out = b"Rar!\x1a\x07\x01\x00" + block(vint(1) + vint(0) + vint(0))                         # main header: no flags
    for name, data in entries.items():
        n = name.encode()
        body = (vint(2) + vint(0x0002) + vint(len(data))                                         # file header with a data area
                + vint(0x0004) + vint(len(data)) + vint(0o644) + zlib.crc32(data).to_bytes(4, "little")   # CRC present, size, attributes
                + vint(0) + vint(1) + vint(len(n)) + n)                                          # stored, unix, name
        out += block(body, data)
    return out + block(vint(5) + vint(0) + vint(0))                                              # end of archive


@pytest.mark.skipif(not shutil.which("bsdtar"), reason="bsdtar is not installed")
def test_a_rar_goes_to_bsdtar_as_a_stream(tmp_path, monkeypatch):
    # vrc_mexico.rar (2026-11-26): opened as a FILE, libarchive also looked for a zip directory at the end, found the one of the rar's last member (a stored .zip)
    # and unpacked that zip's 3 files instead of the track's 175: «no car or track folder found». Fed as a stream it is judged by its first bytes only.
    from pathlib import Path
    real, seen = unpack.subprocess.Popen, []
    monkeypatch.setattr(unpack.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd) or real(cmd, **kw))
    inner = _zip({"Config.ini": b"[SPLIT_AO]", "ExtraSamples.data": b"x"})
    arc, dest = tmp_path / "a.rar", tmp_path / "out"
    arc.write_bytes(_rar5({"vrc_mexico/models.ini": b"[MODEL_0]", "vrc_mexico/ui/ui_track.json": b"{}", "vrc_mexico/extension/car.zip": inner}))
    dest.mkdir()
    unpack._extract_bsdtar(arc, dest, 10**9, 100)
    assert seen[-1][seen[-1].index("-f") + 1] == "-", seen[-1]
    assert (dest / "vrc_mexico" / "models.ini").read_bytes() == b"[MODEL_0]" and (dest / "vrc_mexico" / "extension" / "car.zip").read_bytes() == inner
    assert not (dest / "Config.ini").exists()
    seven = tmp_path / "b.7z"   # (7z must seek to its headers at the end: it keeps being opened as a file)
    seven.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 32)
    with pytest.raises(uploadguard.Rejected):
        unpack._extract_bsdtar(seven, dest, 10**9, 100)
    assert seen[-1][seen[-1].index("-f") + 1] == str(seven)
