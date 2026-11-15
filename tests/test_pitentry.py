"""The pit entry worked out from a track's AI lines at upload (app/pitentry.py) and handed to the hub with the pit-lane limit."""
import json
import struct

import pytest

from app import pitentry
from app.config import settings


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))


def _ai(path, pts):
    """A version 7 .ai file with these (x, z) points; the length runs along them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out, run = [struct.pack("<4i", 7, len(pts), 0, 0)], 0.0
    for i, (x, z) in enumerate(pts):
        if i:
            run += ((x - pts[i - 1][0]) ** 2 + (z - pts[i - 1][1]) ** 2) ** 0.5
        out.append(struct.pack("<4fi", x, 0.0, z, run, i))
    path.write_bytes(b"".join(out))


def _track(root, layout="gp"):
    lay = root / layout
    (lay / "data").mkdir(parents=True)
    _ai(lay / "ai" / "fast_lane.ai", [(float(x), 0.0) for x in range(0, 1001, 5)])   # a straight "lap" of 1000 m
    _ai(lay / "ai" / "pit_lane.ai", [(590.0 + i * 5, min(20.0, i * 2.0)) for i in range(40)])   # the pit axis runs along it from 590 m and drifts away: 8 m off at about 615 m
    return lay


def test_the_entry_is_where_the_pit_axis_leaves_the_racing_line_and_it_is_saved_per_layout(tmp_path):
    lay = _track(tmp_path / "glen")
    entry = pitentry.entry_of(lay)
    assert entry is not None and 0.60 <= entry <= 0.64, entry
    (tmp_path / "glen" / "skins").mkdir()   # (a folder that is not a layout is left alone)
    assert pitentry.write_all(tmp_path / "glen") == 1
    assert json.loads(pitentry.store_path("glen", "gp").read_text()) == {"entry": entry}
    assert list((tmp_path / "glen").rglob("pit_entry*")) == [], "nothing is written inside the track: its folders are hashed, sealed and offered to the drivers"
    assert pitentry.read("glen", "gp") == entry and pitentry.read("glen", "other") is None


def test_files_that_are_not_ai_lines_give_no_entry_and_a_saved_value_out_of_range_is_not_read(tmp_path):
    lay = _track(tmp_path / "t")
    (lay / "ai" / "pit_lane.ai").write_bytes(b"not an ai file at all")
    assert pitentry.entry_of(lay) is None and pitentry.write_all(tmp_path / "t") == 0
    dest = pitentry.store_path("t", "gp")
    dest.parent.mkdir(parents=True)
    for bad in ('{"entry": 7}', '{"entry": "x"}', '{"entry": true}', '{"entry": -0.1}', "[1]", "not json", "[" * 5000, '{"entry": 0.5, "pad": "' + "x" * 400 + '"}'):
        dest.write_text(bad)
        assert pitentry.read("t", "gp") is None, bad[:30]


def test_the_race_state_hands_the_entry_to_the_hub_with_the_limit():
    from app.live.racecontrol import RaceControl
    rc = RaceControl(session_ref={"session_type": 3}); rc.server_id = 9
    rc.pit_limit = {"limit_kmh": 80, "tolerance_kmh": 5, "penalty_s": 0}
    assert rc.snapshot()["pit_limit"] == {"limit_kmh": 80, "tolerance_kmh": 5}, "not known: the hub learns it"
    rc.pit_entry = 0.6123
    assert rc.snapshot()["pit_limit"] == {"limit_kmh": 80, "tolerance_kmh": 5, "entry": 0.6123}


def test_a_layout_with_spaces_in_its_name_works_and_a_small_entry_is_rounded_to_plain_decimals(tmp_path):
    _track(tmp_path / "vir", "full course")   # (VIR: «full course»: the hub's session says track=vir, config=full course)
    assert pitentry.write_all(tmp_path / "vir") == 1
    assert pitentry.read("vir", "full course") is not None
    dest = pitentry.store_path("vir", "full course")
    dest.write_text('{"entry": 0.00001}')   # (json writes small numbers as 1e-05: the hub's Lua pattern would read it as 1)
    assert pitentry.read("vir", "full course") == 0.0
    dest.write_text('{"entry": 0.90214}')
    assert pitentry.read("vir", "full course") == 0.9021


@pytest.mark.parametrize("track, config", [("..", "gp"), ("a/b", ""), ("t", "../x"), ("t", "a/b"), ("", "gp"), (".hidden", ""), ("t\\u0000", "")])
def test_a_name_that_could_be_a_path_has_no_place_to_be_saved_or_read(track, config):
    assert pitentry.store_path(track, config) is None and pitentry.read(track, config) is None


def test_ai_files_are_limited_in_size_and_in_points(tmp_path):
    lay = _track(tmp_path / "big")
    (lay / "ai" / "fast_lane.ai").write_bytes(struct.pack("<4i", 7, 2_000_000, 0, 0) + b"\0" * 40_000_040)   # (the header lies: millions of points)
    assert pitentry.entry_of(lay) is None
    (lay / "ai" / "fast_lane.ai").write_bytes(struct.pack("<4i", 7, 10, 0, 0) + b"\0" * (pitentry.MAX_BYTES + 1))   # (and a file over the size limit)
    assert pitentry.entry_of(lay) is None


def test_two_different_pairs_never_share_a_file_and_the_entry_never_rounds_up_to_one(tmp_path):
    assert pitentry.store_path("a__b", "c") != pitentry.store_path("a", "b__c")
    _track(tmp_path / "vir", "gp")
    pitentry.write_all(tmp_path / "vir")
    dest = pitentry.store_path("vir", "gp")
    assert not list(dest.parent.glob("*.tmp")), "the temporary file of the atomic write is gone"
    dest.write_text('{"entry": 0.99999}')
    assert pitentry.read("vir", "gp") == 0.9999
