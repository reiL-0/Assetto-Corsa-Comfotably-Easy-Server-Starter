"""A ui_*.json saved as Windows-1252 (ks_nordschleife) must not 500 the track list (UnicodeDecodeError is not a JSONDecodeError)."""
from app import content


def test_cp1252_and_garbage(tmp_path):
    f = tmp_path / "ui_track.json"
    f.write_bytes('{"name": "Nordschleife 20°"}'.encode("cp1252"))
    assert content._read_json(f)["name"] == "Nordschleife 20°"
    f.write_bytes(b"\xff\xfe not json")
    assert content._read_json(f) == {}
    assert content._read_json(tmp_path / "missing.json") == {}
