import io
import zipfile

import pytest
from conftest import ADMIN
from fastapi.testclient import TestClient

from app import content, uploadguard
from app.main import app

client = TestClient(app, headers=ADMIN)


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in files.items():
            zf.writestr(n, b)
    return buf.getvalue()


def test_member_names_are_checked_whatever_the_separator():
    assert uploadguard.safe_member("car/./data.acd") == "car/data.acd"
    for bad in ("../x", "a/../../x", "/etc/passwd", "a\\..\\x", "C:\\x", "C:/x", "a\0b", ""):
        with pytest.raises(uploadguard.Rejected):
            uploadguard.safe_member(bad)


def test_zip_limits_count_total_and_single_file():
    z = zipfile.ZipFile(io.BytesIO(_zip({"c/a": b"x" * 100, "c/b": b"y" * 100})))
    uploadguard.check_zip(z)
    with pytest.raises(uploadguard.Rejected, match="too many"):
        uploadguard.check_zip(z, max_files=1)
    with pytest.raises(uploadguard.Rejected, match="limit"):
        uploadguard.check_zip(z, max_total=150)
    with pytest.raises(uploadguard.Rejected, match="too big"):
        uploadguard.check_zip(z, max_file=50)


def test_pack_keeps_what_acserver_reads_for_cars_and_tracks():
    keep = uploadguard.keep_in_pack
    assert keep("car", "data.acd") and keep("car", "data/engine.ini") and keep("car", "ui/ui_car.json") and keep("car", "skins/red/ui_skin.json")
    assert not keep("car", "car.kn5") and not keep("car", "skins/red/body.dds") and not keep("car", "sfx/car.bank") and not keep("car", "ui/badge.png")
    assert keep("track", "data/surfaces.ini") and keep("track", "gp/data/surfaces.ini") and keep("track", "models.ini") and keep("track", "models_gp.ini")
    assert keep("track", "ui/gp/ui_track.json") and keep("track", "map.png")
    assert not keep("track", "imola.kn5") and not keep("track", "texture/asphalt.dds") and not keep("track", "ai/fast_lane.ai")


def test_pack_upload_drops_models_and_textures_but_keeps_skin_names():
    files = {"pk_car/data.acd": b"d", "pk_car/ui/ui_car.json": b'{"name": "pk"}', "pk_car/pk_car.kn5": b"K" * 5000,
             "pk_car/skins/red/body.dds": b"T" * 3000, "pk_car/sfx/a.bank": b"S"}
    r = client.post("/api/v1/content/cars?pack=true", files={"file": ("pk.zip", _zip(files), "application/zip")})
    assert r.status_code == 201, r.text
    d = content._cars_dir() / "pk_car"
    assert (d / "data.acd").is_file() and (d / "ui" / "ui_car.json").is_file()
    assert not (d / "pk_car.kn5").exists() and not (d / "sfx").exists() and not any(d.rglob("*.dds"))
    assert (d / "skins" / "red").is_dir()                                                   # the skin name survives (the entry list needs it)
    full = client.post("/api/v1/content/cars", files={"file": ("pk2.zip", _zip({k.replace("pk_car", "pk_full"): v for k, v in files.items()}), "application/zip")})
    assert full.status_code == 201 and (content._cars_dir() / "pk_full" / "pk_full.kn5").is_file()   # without pack nothing changes


def test_unsafe_or_bomb_archives_are_refused_with_a_sentence():
    r = client.post("/api/v1/content/cars", files={"file": ("bad.zip", _zip({"x/../../evil": b"1"}), "application/zip")})
    assert r.status_code == 400 and "unsafe" in r.text
