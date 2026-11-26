"""Lenient uploads (settings.upload_permissive): any archive through bsdtar, the car/track folder anywhere inside, the kind detected. Strict mode stays zip-only."""
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest
from conftest import ADMIN
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import content, unpack, uploadguard
from app.config import settings
from app.main import app

pytestmark = pytest.mark.skipif(not shutil.which("bsdtar"), reason="bsdtar (libarchive) is needed to build and open these archives")
client = TestClient(app, headers=ADMIN)

TRACK = {"models_oval.ini": b"m", "oval/data/surfaces.ini": b"s", "ui/oval/ui_track.json": b"{}", "big.kn5": b"k" * 5000}
CAR = {"data.acd": b"acd", "ui/ui_car.json": b"{}", "skins/red/body.dds": b"t"}


@pytest.fixture
def permissive(monkeypatch):
    monkeypatch.setattr(settings, "upload_permissive", True)


def _make(tmp_path, files: dict[str, bytes], fmt: str) -> Path:
    """An archive of `files` built by bsdtar itself («7zip», «gnutar» gz, «zip»)."""
    src = Path(tempfile.mkdtemp(dir=tmp_path))
    for name, data in files.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(data)
    out = Path(tempfile.mkdtemp(dir=tmp_path)) / "mod.bin"
    flags = {"7zip": ["--format", "7zip"], "tgz": ["-z"], "zip": ["--format", "zip"], "tar": ["--format", "ustar"]}[fmt]
    subprocess.run(["bsdtar", *flags, "-cf", str(out), "-C", str(src), "."], check=True)
    return out


def _wrapped(files, *prefix):
    return {"/".join(prefix + (k,)): v for k, v in files.items()}


@pytest.mark.parametrize("fmt", ["7zip", "tgz", "zip", "tar"])
def test_a_mod_with_wrapper_folders_and_a_readme_installs_as_a_track(permissive, tmp_path, fmt):
    files = _wrapped(TRACK, "assettocorsa", "content", "tracks", f"mod_{fmt}") | {"README.pdf": b"pdf"}
    res = content._extract(_make(tmp_path, files, fmt), content._tracks_dir(), auto=True)
    assert str(res) == f"mod_{fmt}" and res.kind == "track"
    d = content._tracks_dir() / f"mod_{fmt}"
    assert (d / "oval/data/surfaces.ini").is_file() and not (d / "big.kn5").exists()   # cut down to the server pack as always


def test_the_kind_comes_from_the_folder_not_from_where_it_was_sent(permissive, tmp_path):
    res = content._extract(_make(tmp_path, _wrapped(CAR, "x", "wrapped_car"), "7zip"), content._tracks_dir(), auto=True)
    assert res.kind == "car" and (content._cars_dir() / "wrapped_car" / "data.acd").is_file()
    assert not (content._tracks_dir() / "wrapped_car").exists()


@pytest.mark.parametrize("files, why", [
    ({"README.txt": b"hi"}, "no car or track folder"),
    (TRACK, "loose at the root"),   # no folder to name it by, and its layout folder must not be taken for a track
])
def test_unclear_archives_are_refused(permissive, tmp_path, files, why):
    with pytest.raises(HTTPException) as e:
        content._extract(_make(tmp_path, files, "tgz"), content._tracks_dir(), auto=True)
    assert e.value.status_code == 400 and why in e.value.detail


def test_strict_mode_is_still_zip_only_with_one_root_folder(tmp_path):
    with pytest.raises(HTTPException) as e:
        content._extract(_make(tmp_path, _wrapped(TRACK, "t"), "7zip"), content._tracks_dir())
    assert e.value.status_code == 400 and "only .zip" in e.value.detail
    wrapped = _make(tmp_path, _wrapped(TRACK, "a", "t") | {"README.pdf": b"x"}, "zip")
    with pytest.raises(HTTPException) as e:
        content._extract(wrapped, content._tracks_dir())
    assert "exactly one folder" in e.value.detail


def test_unknown_formats_are_not_opened(permissive, tmp_path):
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"\0" * 32768 + b"\x01CD001\x01" + b"\0" * 4096)
    with pytest.raises(HTTPException) as e:
        content._extract(iso, content._tracks_dir(), auto=True)
    assert e.value.status_code == 400 and "unrecognised archive" in e.value.detail


def _tar(tmp_path, add) -> Path:
    out = tmp_path / f"{os.urandom(3).hex()}.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        info = tarfile.TarInfo("t/models.ini")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"m"))
        add(tf)
    return out


def _symlink(tf):
    i = tarfile.TarInfo("t/evil")
    i.type, i.linkname = tarfile.SYMTYPE, "/etc/passwd"
    tf.addfile(i)


def _hardlink(tf):
    i = tarfile.TarInfo("t/again.ini")
    i.type, i.linkname = tarfile.LNKTYPE, "t/models.ini"
    tf.addfile(i)


def _traversal(tf):
    i = tarfile.TarInfo("../outside.txt")
    i.size = 1
    tf.addfile(i, io.BytesIO(b"x"))


@pytest.mark.parametrize("add", [_symlink, _hardlink, _traversal])
def test_links_and_escaping_names_never_get_installed(permissive, tmp_path, add):
    outside = tmp_path / "outside.txt"
    try:
        content._extract(_tar(tmp_path, add), content._tracks_dir(), auto=True)
    except HTTPException as e:
        assert e.status_code == 400
    assert not outside.exists() and not (content._tracks_dir() / "t").exists()


def test_a_bomb_is_stopped_while_it_unpacks(permissive, tmp_path):
    arc = _make(tmp_path, {"t/models.ini": b"m", "t/zeros.bin": b"\0" * (8 * 1024**2)}, "tgz")   # a few KB packed
    dest = Path(tempfile.mkdtemp(dir=tmp_path))
    with pytest.raises(uploadguard.Rejected, match="more files or bytes"):
        unpack.run_sandboxed(arc, dest, max_total=1024**2, permissive=True)


def test_http_kind_is_optional_only_when_permissive(monkeypatch):
    assert client.post("/api/v1/content/uploads", json={}).status_code == 422
    monkeypatch.setattr(settings, "upload_permissive", True)
    assert client.post("/api/v1/content/uploads", json={}).status_code == 201


def test_check_tree_rejects_special_files(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(uploadguard.Rejected, match="unsafe"):
        uploadguard.check_tree(tmp_path)


# A pack of several cars and tracks (the owner's decision 2026-11-26): each one is installed with all the checks of a single upload; one that cannot be is reported and the rest go in.
def test_a_pack_installs_every_car_and_track_it_holds(permissive, tmp_path):
    files = _wrapped(TRACK, "pack", "tracks", "pk_track_a") | _wrapped(TRACK, "pack", "tracks", "pk_track_b") | _wrapped(CAR, "pack", "cars", "pk_car") | {"pack/README.txt": b"hi"}
    res = content._extract(_make(tmp_path, files, "7zip"), content._tracks_dir(), auto=True)
    assert sorted(i["name"] for i in res.items) == ["pk_car", "pk_track_a", "pk_track_b"] and res.failed == [] and res.kind == "" and res.note == "", (res.items, res.failed)
    assert {i["name"]: i["kind"] for i in res.items} == {"pk_car": "car", "pk_track_a": "track", "pk_track_b": "track"}
    assert (content._cars_dir() / "pk_car" / "data.acd").is_file() and (content._tracks_dir() / "pk_track_b" / "oval/data/surfaces.ini").is_file()
    assert content._installed(res, "track")["items"] == res.items and "pack" in content._installed(res, "track"), "the answer of the plain upload route says it was a mixed pack"


def test_what_cannot_be_installed_is_reported_and_the_rest_goes_in(permissive, tmp_path):
    both = CAR | {"models.ini": b"m", "data/surfaces.ini": b"s"}   # looks like a car AND a track: unclear on its own
    files = _wrapped(CAR, "a", "pk_dup") | _wrapped(CAR, "b", "pk_dup") | _wrapped(both, "c", "pk_both") | _wrapped(TRACK, "d", "pk_good")
    res = content._extract(_make(tmp_path, files, "tgz"), content._tracks_dir(), auto=True)
    assert sorted(i["name"] for i in res.items) == ["pk_dup", "pk_good"], res.items
    why = {f["name"]: f["error"] for f in res.failed}
    assert set(why) == {"pk_dup", "pk_both"} and "two folders with this name" in why["pk_dup"] and "looks like both" in why["pk_both"], why
    assert res.note == "2 of 4 not installed" and not (content._cars_dir() / "pk_both").exists() and not (content._tracks_dir() / "pk_both").exists()


def test_a_pack_where_nothing_can_be_installed_is_an_error(permissive, tmp_path):
    both = CAR | {"models.ini": b"m", "data/surfaces.ini": b"s"}
    with pytest.raises(HTTPException) as e:
        content._extract(_make(tmp_path, _wrapped(both, "x", "pk_n1") | _wrapped(both, "y", "pk_n2"), "tgz"), content._tracks_dir(), auto=True)
    assert e.value.status_code == 400 and "nothing in the pack could be installed" in e.value.detail and "pk_n1" in e.value.detail and "pk_n2" in e.value.detail



def test_a_copy_that_fails_halfway_through_a_pack_is_reported_and_the_next_ones_still_go_in(permissive, tmp_path, monkeypatch):
    real = shutil.copytree
    def copy(src, dst, *a, **kw):
        if Path(dst).name == "pk_c2":
            raise shutil.Error([(str(src), str(dst), "disk said no")])
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(content.shutil, "copytree", copy)
    files = _wrapped(CAR, "p", "pk_c1") | _wrapped(CAR, "p", "pk_c2") | _wrapped(CAR, "p", "pk_c3")
    res = content._extract(_make(tmp_path, files, "tgz"), content._cars_dir(), auto=True)
    assert [i["name"] for i in res.items] == ["pk_c1", "pk_c3"] and [f["name"] for f in res.failed] == ["pk_c2"] and "disk said no" in res.failed[0]["error"], (res.items, res.failed)
