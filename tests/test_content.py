import io
import zipfile

import pytest
from conftest import ADMIN
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import content
from app.main import app

client = TestClient(app, headers=ADMIN)


def _car_zip(name: str, brand: str = "Ferrari") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/ui/ui_car.json", f'{{"name": "{name}", "brand": "{brand}"}}')
        zf.writestr(f"{name}/data.acd", b"fake-data")
        zf.writestr(f"{name}/skins/red/livery.png", b"png-bytes")
    return buf.getvalue()


def test_safe_rejects_traversal_and_separators():
    for bad in ("..", ".", "a/b", "a\\b", ""):
        with pytest.raises(HTTPException):
            content._safe(bad)
    assert content._safe("ok_name") == "ok_name"


def test_upload_list_checksum_and_download_car():
    r = client.post(
        "/api/v1/content/cars", files={"file": ("test_car.zip", _car_zip("test_car"), "application/zip")}
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"car": "test_car"}

    cars = client.get("/api/v1/content/cars").json()
    entry = next(c for c in cars if c["car"] == "test_car")
    assert entry["name"] == "test_car"
    assert entry["brand"] == "Ferrari"
    assert entry["skins"] == ["red"]

    sums = client.get("/api/v1/content/cars/test_car/checksum").json()
    assert sums["data_acd"] == content._sha1(content._cars_dir() / "test_car" / "data.acd")

    dl = client.get("/api/v1/content/cars/test_car.zip")
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert "test_car/data.acd" in zf.namelist()


def test_entry_list_builder_validates_car_and_skin():
    client.post("/api/v1/content/cars", files={"file": ("c2.zip", _car_zip("c2"), "application/zip")})

    r = client.post("/api/v1/content/entry_list", json=[{"car": "c2", "skin": "red"}])
    assert r.status_code == 200
    assert r.json() == [{"MODEL": "c2", "SKIN": "red"}]

    assert client.post("/api/v1/content/entry_list", json=[{"car": "nope"}]).status_code == 404
    assert (
        client.post("/api/v1/content/entry_list", json=[{"car": "c2", "skin": "no-such-skin"}]).status_code
        == 404
    )


def test_upload_rejects_zip_slip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil/data.acd", b"x")
    r = client.post("/api/v1/content/cars", files={"file": ("evil.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400


def _png(w: int = 88, h: int = 88) -> bytes:
    import struct, zlib
    def chunk(k: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d))
    return content.PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0" + b"\0" * w) * 1) + chunk(b"IEND", b"")


def _skin_pack(**skins: bytes | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, logo in skins.items():
            zf.writestr(f"pack/{name}/ui_skin.json", '{"skinname": "x"}')
            if logo is not None:
                zf.writestr(f"pack/{name}/Livery.PNG", logo)
    return buf.getvalue()


def test_skin_pack_keeps_a_valid_livery_serves_it_and_drops_it_on_a_bad_resend():
    client.post("/api/v1/content/cars", files={"file": ("lcar.zip", _car_zip("lcar"), "application/zip")})
    up = lambda **s: client.post("/api/v1/content/cars/lcar/skins", files={"file": ("p.zip", _skin_pack(**s), "application/zip")})
    assert up(cortana=_png(), bad=b"png-bytes", huge=_png(5000, 5000), cut=_png()[:-12], none=None).status_code == 201
    car = next(c for c in client.get("/api/v1/content/cars").json() if c["car"] == "lcar")
    assert set(car["liveries"]) == {"cortana"} and {"cortana", "bad", "huge", "cut", "none"} <= set(car["skins"])
    r = client.get("/api/v1/content/cars/lcar/skins/cortana/livery.png")
    assert r.status_code == 200 and r.content == _png() and r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/v1/content/cars/lcar/skins/bad/livery.png").status_code == 404
    assert client.get("/api/v1/content/cars/lcar/skins/red/livery.png").status_code == 404, "a whole-car upload's unchecked file is not served"
    assert up(cortana=b"junk").status_code == 201
    assert client.get("/api/v1/content/cars/lcar/skins/cortana/livery.png").status_code == 404, "the pack replaces the skin: the old logo goes"


def test_loose_data_is_packed_into_data_acd_and_new_version_replaces_it():
    from app import acd

    def loose(name, power):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{name}/ui/ui_car.json", '{"name": "x"}')
            zf.writestr(f"{name}/data/car.ini", f"[HEADER]\nPOWER={power}\n")
            zf.writestr(f"{name}/data/extra.ini", "[A]\n")
        return buf.getvalue()

    post = lambda z: client.post("/api/v1/content/cars", files={"file": ("c.zip", z, "application/zip")})
    assert post(loose("packme", 1)).status_code == 201
    d = content._cars_dir() / "packme"
    assert not (d / "data").exists() and (d / "data.acd").is_file()
    assert acd.unpack((d / "data.acd").read_bytes(), "packme")["car.ini"] == b"[HEADER]\nPOWER=1\n"
    assert next(c for c in client.get("/api/v1/content/cars").json() if c["car"] == "packme")["usable"]

    (d / "data").mkdir()                                  # an old loose data/ left by an earlier upload
    (d / "data" / "stale.ini").write_text("old")
    assert post(loose("packme", 2)).status_code == 201    # new version: the old data/ goes, the new pack replaces data.acd
    assert not (d / "data").exists()
    assert acd.unpack((d / "data.acd").read_bytes(), "packme")["car.ini"] == b"[HEADER]\nPOWER=2\n"


def test_acd_key_and_format_match_a_real_kunos_file():
    from app import acd
    # keys that decrypted the league server's real data.acd of these cars (the long name exercises the int32 overflow of the key)
    assert acd.key("ks_mazda_787b") == b"177-121-208-61-54-63-8-99"
    files = {"a.ini": b"[X]\nY=1\n", "b.lut": bytes(range(256))}
    assert acd.unpack(acd.pack(files, "ks_mazda_787b"), "ks_mazda_787b") == files


def test_acd_pack_order_is_content_managers():
    from app import acd
    names = ["wing_rear.lut", "__cm_tyre_0.lut", "flame_presets.ini", "flames.ini", "suspension_graphics.ini", "suspensions.ini", "brakes.ini", "brakes - C-pia.ini", "aero.ini"]
    assert sorted(names, key=acd.order) == ["aero.ini", "brakes - C-pia.ini", "brakes.ini", "flames.ini", "flame_presets.ini", "suspensions.ini", "suspension_graphics.ini", "wing_rear.lut", "__cm_tyre_0.lut"]
