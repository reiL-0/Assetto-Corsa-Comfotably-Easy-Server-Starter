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


def _track_zip(name: str) -> bytes:
    return _zip({f"{name}/models.ini": b"[MODEL_0]", f"{name}/data/surfaces.ini": b"[SURFACE_0]", f"{name}/{name}.kn5": b"K" * 500})


def test_a_car_sent_as_a_track_and_a_track_sent_as_a_car_are_installed_where_they_belong():
    car = _zip({"mix_car/data.acd": b"d", "mix_car/ui/ui_car.json": b'{"name": "mix"}', "mix_car/mix_car.kn5": b"K" * 500})
    r = client.post("/api/v1/content/tracks", files={"file": ("c.zip", car, "application/zip")})        # a car, uploaded as a track
    assert r.status_code == 201 and r.json()["car"] == "mix_car" and "installed as a car" in r.json()["note"], r.text
    assert (content._cars_dir() / "mix_car" / "data.acd").is_file() and not (content._tracks_dir() / "mix_car").exists()
    r = client.post("/api/v1/content/cars", files={"file": ("t.zip", _track_zip("mix_track"), "application/zip")})   # a track, uploaded as a car
    assert r.status_code == 201 and r.json()["track"] == "mix_track" and "installed as a track" in r.json()["note"], r.text
    assert (content._tracks_dir() / "mix_track" / "models.ini").is_file() and not (content._cars_dir() / "mix_track").exists()
    ok = client.post("/api/v1/content/tracks", files={"file": ("ok.zip", _track_zip("right_track"), "application/zip")})
    assert ok.json() == {"track": "right_track"}                                                          # correct uploads answer as before


def test_detect_kind_says_nothing_when_it_is_not_clear(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "readme.txt").write_text("?")
    assert uploadguard.detect_kind(tmp_path / "x") is None
    both = tmp_path / "both"
    (both / "ui").mkdir(parents=True)
    (both / "ui" / "ui_car.json").write_text("{}")
    (both / "models.ini").write_text("[M]")
    assert uploadguard.detect_kind(both) is None                                                           # car and track markers: the uploader's choice stands


@pytest.mark.parametrize("marker", ["data.acd", "MODELS.ini", "data/surfaces.ini", "ui/gp/ui_track.json"])
def test_directories_are_not_kind_markers(tmp_path, marker):
    (tmp_path / marker).mkdir(parents=True)
    assert uploadguard.detect_kind(tmp_path) is None


@pytest.mark.parametrize("marker,kind", [("DATA.ACD", "car"), ("MODELS_gp.INI", "track")])
def test_top_level_file_markers_ignore_case(tmp_path, marker, kind):
    (tmp_path / marker).write_bytes(b"x")
    assert uploadguard.detect_kind(tmp_path) == kind


@pytest.fixture
def isolated_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(content.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(content, "_uploads", {})
    # Execute workers synchronously so assertions observe completion without polling races.
    monkeypatch.setattr(content.threading, "Thread", lambda target, args, daemon: type(
        "Worker", (), {"start": lambda self: target(*args)})())
    logs = []
    monkeypatch.setattr(content.metrics, "log", lambda *args, **kwargs: logs.append((args, kwargs)))
    return logs


@pytest.mark.parametrize("via", ["chunks", "link"])
@pytest.mark.parametrize("effective", ["car", "track"])
@pytest.mark.parametrize("pack", [False, True])
def test_background_upload_reports_effective_kind_and_prunes_correctly(isolated_uploads, monkeypatch, via, effective, pack):
    asked = "track" if effective == "car" else "car"
    name = "redirected"
    files = {f"{name}/data/physics.ini": b"physics", f"{name}/model.kn5": b"model"}
    if effective == "car":
        files.update({f"{name}/data.acd": b"car", f"{name}/ui/ui_car.json": b"{}", f"{name}/skins/red/body.dds": b"texture"})
        marker = "data.acd"
    else:
        files.update({f"{name}/models_gp.ini": b"models", f"{name}/gp/data/surfaces.ini": b"surfaces", f"{name}/ui/gp/ui_track.json": b"{}"})
        marker = "models_gp.ini"
    archive = _zip(files)
    body = {"kind": asked, "pack": pack}
    if via == "chunks":
        r = client.post("/api/v1/content/uploads", json=body)
        assert r.status_code == 201, r.text
        uid = r.json()["id"]
        split = len(archive) // 2
        for offset, part in [(0, archive[:split]), (split, archive[split:])]:
            assert client.put(f"/api/v1/content/uploads/{uid}?offset={offset}", content=part).status_code == 200
        assert client.post(f"/api/v1/content/uploads/{uid}/complete").status_code == 202
    else:
        monkeypatch.setattr(content.download, "check_url", lambda url: None)
        monkeypatch.setattr(content.download, "opener", lambda: None)
        monkeypatch.setattr(content.download, "resolve", lambda url, opener: url)
        def fetch(url, path, limit, progress):
            path.write_bytes(archive)
            progress(len(archive), len(archive))
        monkeypatch.setattr(content.download, "fetch", fetch)
        r = client.post("/api/v1/content/uploads/from-link", json=body | {"url": "https://example.test/mod.zip"})
        assert r.status_code == 202, r.text
        uid = r.json()["id"]
    status = client.get(f"/api/v1/content/uploads/{uid}").json()
    assert status["state"] == "done", status
    assert status["kind"] == effective and status["requested_kind"] == asked
    assert status["result"] == name and f"installed as a {effective}" in status["note"]
    dest = content._content_dir() / f"{effective}s" / name
    assert (dest / marker).is_file()
    assert not (content._content_dir() / f"{asked}s" / name).exists()
    assert (dest / "model.kn5").exists() is (not pack)
    assert (dest / "data/physics.ini").is_file()
    if effective == "car":
        assert (dest / "ui/ui_car.json").is_file()
        assert (dest / "skins/red").is_dir()
        assert (dest / "skins/red/body.dds").exists() is (not pack)
    else:
        assert (dest / "gp/data/surfaces.ini").is_file()
        assert (dest / "ui/gp/ui_track.json").is_file()
    assert isolated_uploads[-1] == ((0, "import_ok"), {"name": name, "track": effective})


@pytest.mark.parametrize("asked", ["car", "track"])
def test_ambiguous_upload_respects_requested_destination(isolated_uploads, asked):
    archive = _zip({"both/data.acd": b"car", "both/models.ini": b"track"})
    r = client.post(f"/api/v1/content/{asked}s", files={"file": ("both.zip", archive, "application/zip")})
    assert r.status_code == 201, r.text
    assert r.json() == {asked: "both"}
    assert (content._content_dir() / f"{asked}s/both/data.acd").is_file()
    assert (content._content_dir() / f"{asked}s/both/models.ini").is_file()


def test_inbox_reports_redirected_destination(isolated_uploads):
    (content.inbox_dir() / "car.zip").write_bytes(_zip({"inbox_car/data.acd": b"car"}))
    r = client.post("/api/v1/content/tracks/import", json={"file": "car.zip"})
    assert r.status_code == 201, r.text
    assert r.json()["car"] == "inbox_car" and "installed as a car" in r.json()["note"]
    assert (content._cars_dir() / "inbox_car/data.acd").is_file()
    assert not (content._tracks_dir() / "inbox_car").exists()
