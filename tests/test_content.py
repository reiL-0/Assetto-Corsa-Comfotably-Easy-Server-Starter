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
