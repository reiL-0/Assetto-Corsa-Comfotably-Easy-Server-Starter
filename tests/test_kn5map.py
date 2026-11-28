"""A live map drawn from a track's .kn5 (app/kn5map.py): only the driveable meshes, placed by their nodes' matrices, never over a good map.png of the pack."""

import struct
import zlib
from pathlib import Path

import pytest

from app import kn5map


def _s(text):
    b = text.encode()
    return struct.pack("<i", len(b)) + b


def _mesh(name, verts, idx):
    body = struct.pack("<i", 2) + _s(name) + struct.pack("<i", 0) + b"\x01" + b"\x01\x01\x00"
    body += struct.pack("<i", len(verts)) + b"".join(struct.pack("<3f3f2f3f", x, y, z, 0, 1, 0, 0, 0, 1, 0, 0) for x, y, z in verts)
    body += struct.pack("<i", len(idx)) + struct.pack(f"<{len(idx)}H", *idx)
    return body + struct.pack("<ii", 0, 0) + struct.pack("<2f", 0, 1) + struct.pack("<4f", 0, 0, 0, 1) + b"\x01"


def _kn5(children, tx=0.0, tz=0.0, version=6):
    """A .kn5 with one texture (skipped), no material, and a root dummy that moves its meshes by (tx, tz)."""
    head = b"sc6969" + struct.pack("<i", version) + (struct.pack("<i", 0) if version > 5 else b"")
    tex = struct.pack("<i", 1) + struct.pack("<i", 1) + _s("big.dds") + struct.pack("<i", 1000) + b"\x00" * 1000
    mats = struct.pack("<i", 0)
    m = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, tx, 0, tz, 1.0]
    root = struct.pack("<i", 1) + _s("ROOT") + struct.pack("<i", len(children)) + b"\x01" + struct.pack("<16f", *m)
    return head + tex + mats + root + b"".join(children)


SQUARE = [(0, 0, 0), (100, 0, 0), (100, 0, 50), (0, 0, 50)]
SURFACES = "[SURFACE_0]\nKEY=ROAD\nIS_VALID_TRACK=1\n\n[SURFACE_1]\nKEY=GRASS\nIS_VALID_TRACK=0\n\n[SURFACE_2]\nKEY=PIT\nIS_PITLANE=1\n"


def test_only_driveable_meshes_are_read_and_the_dummy_matrix_moves_them(tmp_path):
    f = tmp_path / "t.kn5"
    f.write_bytes(_kn5([_mesh("1ROAD", SQUARE, [0, 1, 2, 0, 2, 3]), _mesh("TREES", SQUARE, [0, 1, 2]), _mesh("1GRASS", SQUARE, [0, 1, 2])], tx=500, tz=-20))
    keys = kn5map.surface_keys(_ini(tmp_path / "surfaces.ini", SURFACES))
    assert keys == ("ROAD", "PIT")
    tris = kn5map.read_triangles(f, keys)
    assert len(tris) == 12, "the road's two triangles (6 numbers each); the trees and the grass are no track"
    assert tuple(tris[0:4]) == (500.0, -20.0, 600.0, -20.0), "moved by the root dummy's translation"


def _ini(p, text):
    p.write_text(text)
    return p


@pytest.mark.parametrize("bad", [b"", b"notakn5....", b"sc6969" + struct.pack("<i", 9), _kn5([_mesh("1ROAD", SQUARE, [0, 1, 2])])[:-30]])
def test_an_encrypted_or_broken_kn5_fails_cleanly(tmp_path, bad):
    f = tmp_path / "x.kn5"
    f.write_bytes(bad)
    with pytest.raises((ValueError, struct.error)):
        kn5map.read_triangles(f, ("ROAD",))


def test_a_layout_without_a_good_map_is_drawn_and_one_with_it_is_left_alone(tmp_path):
    top = tmp_path / "mytrack"
    (top / "gp" / "data").mkdir(parents=True)
    (top / "short" / "data").mkdir(parents=True)
    _ini(top / "gp" / "data" / "surfaces.ini", SURFACES)
    _ini(top / "short" / "data" / "surfaces.ini", SURFACES)
    (top / "road.kn5").write_bytes(_kn5([_mesh("1ROAD", SQUARE, [0, 1, 2, 0, 2, 3])]))
    _ini(top / "models_gp.ini", "[MODEL_0]\nFILE=road.kn5\nPOSITION=0,0,0\n")
    _ini(top / "models_short.ini", "[MODEL_0]\nFILE=road.kn5\nPOSITION=0,0,0\n")
    png = lambda w, h: b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    (top / "gp" / "map.png").write_bytes(png(20, 20))       # a placeholder: drawn
    (top / "short" / "map.png").write_bytes(png(900, 600))  # a real map: left alone
    stage = tmp_path / "kn5-stage"
    stage.mkdir()
    assert kn5map.stage(top, stage) == 1
    assert not (top / "road.kn5").exists() and (stage / "road.kn5").exists(), "moved aside before the cut"
    out = tmp_path / "maps"
    assert kn5map.draw(stage, out) == {"gp": "ok"}
    ini = (out / "gp" / "map.ini").read_text()
    w, h = kn5map.png_size(out / "gp" / "map.png")
    assert f"WIDTH={w}" in ini and f"HEIGHT={h}" in ini and w > h, "a 100 x 50 m road: wider than tall"
    vals = dict(line.split("=") for line in ini.splitlines() if "=" in line)
    px = (100 + float(vals["X_OFFSET"])) / float(vals["SCALE_FACTOR"]) + float(vals["PADDING"])
    assert abs(px - (w - 1 - kn5map.PAD_PX)) < 2, "the road's far end lands at the far end of the picture (AC's formula)"
    raw = zlib.decompress((out / "gp" / "map.png").read_bytes()[8 + 25 + 8:-12])
    mid = (h // 2) * (1 + 2 * w) + 1 + 2 * (w // 2)
    assert raw[mid + 1] == 0xFF, "the middle of the road is painted"


def test_the_live_map_routes_serve_the_drawn_map_first():
    from fastapi.testclient import TestClient

    from app.content import _tracks_dir
    from app.main import app
    d = kn5map.maps_dir("drawn_trk") / "-"
    d.mkdir(parents=True, exist_ok=True)
    (d / "map.png").write_bytes(b"\x89PNG drawn")
    (d / "map.ini").write_text("[PARAMETERS]\nWIDTH=10\n")
    (_tracks_dir() / "drawn_trk").mkdir(parents=True, exist_ok=True)
    (_tracks_dir() / "drawn_trk" / "map.png").write_bytes(b"\x89PNG placeholder")
    api = TestClient(app)
    assert api.get("/api/v1/servers/1/acsm/content/tracks/drawn_trk/map.png").content == b"\x89PNG drawn"
    assert "WIDTH=10" in api.get("/api/v1/servers/1/acsm/content/tracks/drawn_trk/data/map.ini").text
    kn5map.forget("drawn_trk")
    assert api.get("/api/v1/servers/1/acsm/content/tracks/drawn_trk/map.png").content == b"\x89PNG placeholder", "a new upload: the pack's own again"
    assert kn5map.generated("../x", "", "map.png") is None and kn5map.generated("drawn_trk", "", "../../etc") is None


def test_an_uploaded_track_without_a_map_gets_one_drawn_from_its_kn5_and_keeps_no_kn5():
    import io
    import time
    import zipfile

    from conftest import ADMIN
    from fastapi.testclient import TestClient

    from app.content import _scratch, _tracks_dir
    from app.main import app
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("kn5_up_trk/kn5_up_trk.kn5", _kn5([_mesh("1ROAD", SQUARE, [0, 1, 2, 0, 2, 3])]))
        zf.writestr("kn5_up_trk/data/surfaces.ini", SURFACES)
        zf.writestr("kn5_up_trk/ui/ui_track.json", '{"name": "kn5 test"}')
    r = TestClient(app, headers=ADMIN).post("/api/v1/content/tracks", files={"file": ("t.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 201, r.text
    assert not list((_tracks_dir() / "kn5_up_trk").glob("*.kn5")), "the server pack keeps no .kn5"
    png = kn5map.maps_dir("kn5_up_trk") / "-" / "map.png"
    for _ in range(100):   # drawn in the background, by a child process
        if png.is_file() and not list(_scratch().glob("kn5-*")):
            break
        time.sleep(0.2)
    assert png.is_file() and kn5map.png_size(png)[0] > kn5map.PLACEHOLDER_PX
    assert not list(_scratch().glob("kn5-*")), "the stage and its .kn5 are gone once drawn"


def test_a_drawing_of_an_older_upload_that_ends_late_publishes_nothing(tmp_path):
    def drawn(tag):
        d = tmp_path / tag / "-"
        d.mkdir(parents=True)
        (d / "map.png").write_bytes(tag.encode())
        (d / "map.ini").write_text(tag)
        return tmp_path / tag
    kn5map.forget("race_trk")
    old = kn5map._gen["race_trk"]
    kn5map.forget("race_trk")   # the track was uploaded again while the first drawing ran
    new = kn5map._gen["race_trk"]
    assert kn5map.publish("race_trk", new, drawn("second")) is True
    assert kn5map.publish("race_trk", old, drawn("first")) is False, "the first drawing ends late: not published"
    assert kn5map.generated("race_trk", "", "map.png").read_bytes() == b"second"
