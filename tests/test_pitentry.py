"""The pit entry worked out from a track's AI lines at upload (app/pitentry.py) and handed to the hub with the pit-lane limit."""
import json
import struct

from app import pitentry


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
    assert json.loads((lay / "data" / "pit_entry.json").read_text()) == {"entry": entry}
    assert pitentry.read(tmp_path / "glen", "gp") == entry and pitentry.read(tmp_path / "glen", "other") is None


def test_files_that_are_not_ai_lines_give_no_entry_and_a_saved_value_out_of_range_is_not_read(tmp_path):
    lay = _track(tmp_path / "t")
    (lay / "ai" / "pit_lane.ai").write_bytes(b"not an ai file at all")
    assert pitentry.entry_of(lay) is None and pitentry.write_all(tmp_path / "t") == 0
    (lay / "data" / "pit_entry.json").write_text('{"entry": 7}')
    assert pitentry.read(tmp_path / "t", "gp") is None
    (lay / "data" / "pit_entry.json").write_text('{"entry": "x"}')
    assert pitentry.read(tmp_path / "t", "gp") is None


def test_the_race_state_hands_the_entry_to_the_hub_with_the_limit():
    from app.live.racecontrol import RaceControl
    rc = RaceControl(session_ref={"session_type": 3}); rc.server_id = 9
    rc.pit_limit = {"limit_kmh": 80, "tolerance_kmh": 5, "penalty_s": 0}
    assert rc.snapshot()["pit_limit"] == {"limit_kmh": 80, "tolerance_kmh": 5}, "not known: the hub learns it"
    rc.pit_entry = 0.6123
    assert rc.snapshot()["pit_limit"] == {"limit_kmh": 80, "tolerance_kmh": 5, "entry": 0.6123}
