"""The client's content upload decides by itself whether an archive is a car or a track, and finds its folder wherever the mod put it (README beside it, assettocorsa/content/cars/<name>), as the
league's own panel does; but only .zip is opened (no .rar/.7z/.tar: an external unpacker with a history of bugs) and every size and bomb check is the same. The panel sends no kind at all."""
import io
import time
import zipfile

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content
from app.config import settings
from app.main import app

client = TestClient(app, headers=ADMIN)
OCT = {**ADMIN, "Content-Type": "application/octet-stream"}
MB = 1024 * 1024


def make_zip(files, method=zipfile.ZIP_STORED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


CAR = {"ui/ui_car.json": '{"name": "A", "brand": "X"}', "data.acd": b"acd", "skins/red/ui_skin.json": "{}", "body.kn5": b"k" * 5000}
TRACK = {"models.ini": "[MODEL_0]", "data/surfaces.ini": "[SURFACE_0]", "ui/ui_track.json": '{"name": "T"}', "map.png": b"png", "track.kn5": b"k" * 5000}


@pytest.fixture
def mode(tmp_path, monkeypatch):
    ac = tmp_path / "acserver"
    (ac / "content" / "cars").mkdir(parents=True)
    (ac / "content" / "tracks").mkdir()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(settings, "acserver_cmd", str(ac / "acServer"))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "storage_quota_mb", 0)
    monkeypatch.setattr(settings, "upload_permissive", False)
    monkeypatch.setattr(settings, "upload_autodetect", True)
    return ac


def upload(data, body=None):
    """The panel's chunked upload with NO kind in it; returns the final status."""
    r = client.post("/api/v1/content/uploads", json=body if body is not None else {})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert client.put(f"/api/v1/content/uploads/{uid}?offset=0", content=data, headers=OCT).status_code == 200
    assert client.post(f"/api/v1/content/uploads/{uid}/complete").status_code == 202
    for _ in range(200):
        st = client.get(f"/api/v1/content/uploads/{uid}").json()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("the upload never finished")


def test_the_client_profile_detects_the_kind_and_opens_the_common_formats():
    c = type(settings)(profile="client")
    assert c.upload_autodetect is True and c.upload_permissive is True, "the client panel must detect the kind and take any common archive (the owner's decision)"
    league = type(settings)()
    assert league.upload_autodetect is False and league.upload_permissive is False   # (a plain manager keeps the strict mode)


def test_without_the_mode_a_missing_kind_is_still_refused(mode, monkeypatch):
    monkeypatch.setattr(settings, "upload_autodetect", False)
    r = client.post("/api/v1/content/uploads", json={})
    assert r.status_code == 422 and "car or a track" in r.json()["detail"]


def test_a_car_and_a_track_are_told_apart_and_cut_to_the_server_pack(mode):
    st = upload(make_zip({f"my_car/{k}": v for k, v in CAR.items()}))
    assert st["state"] == "done" and st["kind"] == "car" and st["result"] == "my_car", st
    assert (mode / "content" / "cars" / "my_car" / "data.acd").exists() and not (mode / "content" / "cars" / "my_car" / "body.kn5").exists(), "not cut to the pack"
    st = upload(make_zip({f"my_track/{k}": v for k, v in TRACK.items()}))
    assert st["state"] == "done" and st["kind"] == "track" and st["result"] == "my_track", st
    assert (mode / "content" / "tracks" / "my_track" / "data" / "surfaces.ini").exists() and not (mode / "content" / "tracks" / "my_track" / "track.kn5").exists()


def test_the_folder_may_sit_anywhere_inside_like_a_mod_ships_it(mode):
    st = upload(make_zip({"README.txt": "read me", **{f"assettocorsa/content/cars/modded_car/{k}": v for k, v in CAR.items()}}))
    assert st["state"] == "done" and st["kind"] == "car" and st["result"] == "modded_car", st
    assert (mode / "content" / "cars" / "modded_car" / "data.acd").exists()


@pytest.mark.parametrize("files,sentence", [
    ({"only/readme.txt": "x"}, "no car or track folder"),
    ({"a/" + k: v for k, v in CAR.items()} | {"b/" + k: v for k, v in CAR.items()}, "several cars or tracks"),
    ({k: v for k, v in CAR.items()}, "loose at the root"),
])
def test_what_cannot_be_classified_is_refused_with_a_sentence_and_installs_nothing(mode, files, sentence):
    st = upload(make_zip(files))
    assert st["state"] == "error" and sentence in st["error"], st
    assert not list((mode / "content" / "cars").iterdir()) and not list((mode / "content" / "tracks").iterdir())


def tar_gz(files):
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            data = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def formats(mode, monkeypatch):
    monkeypatch.setattr(settings, "upload_permissive", True)   # what the client profile sets
    return mode


def test_a_tar_gz_is_opened_and_installed_like_a_zip(formats):
    import shutil
    if not shutil.which("bsdtar"):
        pytest.skip("bsdtar is not installed here")
    st = upload(tar_gz({f"tar_car/{k}": v for k, v in CAR.items()}))
    assert st["state"] == "done" and st["kind"] == "car" and st["result"] == "tar_car", st
    assert (formats / "content" / "cars" / "tar_car" / "data.acd").exists() and not (formats / "content" / "cars" / "tar_car" / "body.kn5").exists()


@pytest.mark.parametrize("magic", [b"Rar!\x1a\x07\x00" + b"\0" * 600, b"7z\xbc\xaf\x27\x1c" + b"\0" * 600])
def test_a_broken_rar_or_7z_ends_as_an_error_not_a_crash(formats, magic):
    st = upload(magic)
    assert st["state"] == "error" and st["error"], st
    assert not list((formats / "content" / "cars").iterdir()) and not list((formats / "content" / "tracks").iterdir())


@pytest.mark.parametrize("data", [b"hello", b"\x1f\x8b\x08" + b"\0" * 600, b"MZ" + b"\0" * 600, b"%PDF-1.4" + b"\0" * 600])
def test_what_is_not_an_archive_is_refused(formats, data):
    st = upload(data)
    assert st["state"] == "error" and ("unrecognised archive" in st["error"] or "unpack" in st["error"] or "archive" in st["error"]), st
    assert not list((formats / "content" / "cars").iterdir()) and not list((formats / "content" / "tracks").iterdir())


def test_the_zip_only_mode_still_exists_for_a_plain_manager(mode):
    st = upload(b"Rar!\x1a\x07\x00" + b"\0" * 600)   # autodetect on, formats NOT opened: the previous behaviour
    assert st["state"] == "error" and "only .zip" in st["error"], st


def test_the_bomb_checks_are_the_same(mode):
    bomb = make_zip({"big_car/ui/ui_car.json": "{}", "big_car/data.acd": b"\0" * (100 * MB)}, zipfile.ZIP_DEFLATED)
    st = upload(bomb)
    assert st["state"] == "error" and "zip bomb" in st["error"], st
    assert not list((mode / "content" / "cars").iterdir())
