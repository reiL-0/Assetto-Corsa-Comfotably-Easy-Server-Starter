"""Sector flags: the zones of a track (its sections.ini, or three equal parts) and the director's local yellow / double yellow on one of them (a notice only)."""
import pytest

from app.live import trackzones
from app.live.racecontrol import RaceControl


def test_a_track_without_sections_ini_has_three_equal_sectors(tmp_path, monkeypatch):
    monkeypatch.setattr(trackzones, "_tracks_dir", lambda: tmp_path)
    (tmp_path / "imola" / "data").mkdir(parents=True)
    assert [z["name"] for z in trackzones.zones("imola")] == ["Sector 1", "Sector 2", "Sector 3"]


def test_the_sections_of_the_track_and_its_layout_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(trackzones, "_tracks_dir", lambda: tmp_path)
    d = tmp_path / "melb" / "f1" / "data"
    d.mkdir(parents=True)
    (d / "sections.ini").write_text("[SECTION_0]\nIN=0.055\nOUT=0.080\nTEXT=Jones\n\n[SECTION_1]\nIN=0.081\nOUT=0.106\nTEXT=Brabham\n")
    assert trackzones.zones("melb", "f1") == [{"name": "Jones", "from": 0.055, "to": 0.08}, {"name": "Brabham", "from": 0.081, "to": 0.106}]
    assert trackzones.zones("melb") == trackzones.FALLBACK, "the base track has no file of its own"


def test_a_link_out_of_the_content_or_a_huge_file_is_not_read(tmp_path, monkeypatch):
    content, outside = tmp_path / "tracks", tmp_path / "secret"
    monkeypatch.setattr(trackzones, "_tracks_dir", lambda: content)
    outside.mkdir()
    (outside / "sections.ini").write_text("[SECTION_0]\nIN=0.1\nOUT=0.2\nTEXT=Leak\n")
    (content / "evil").mkdir(parents=True)
    (content / "evil" / "data").symlink_to(outside)
    assert trackzones.zones("evil") == trackzones.FALLBACK
    (content / "big" / "data").mkdir(parents=True)
    (content / "big" / "data" / "sections.ini").write_text("[SECTION_0]\nIN=0.1\nOUT=0.2\nTEXT=X\n" + "#" * trackzones.MAX_BYTES)
    assert trackzones.zones("big") == trackzones.FALLBACK


def test_the_director_puts_and_takes_off_sector_flags():
    rc = RaceControl({"session_type": 3, "track": "t"})
    rc.set_zones(("t", ""), trackzones.FALLBACK)
    rc.set_sector_flag(1, "yellow")
    rc.set_sector_flag(2, "double")
    assert rc.snapshot()["sector_flags"] == [{"name": "Sector 2", "from": 0.3333, "to": 0.6667, "kind": "yellow"},
                                             {"name": "Sector 3", "from": 0.6667, "to": 1.0, "kind": "double"}]
    rc.set_sector_flag(1, None)
    assert [f["name"] for f in rc.snapshot()["sector_flags"]] == ["Sector 3"]
    with pytest.raises(ValueError):
        rc.set_sector_flag(3, "yellow")
    with pytest.raises(ValueError):
        rc.set_sector_flag(None, "yellow")
    rc.set_sector_flag(None, None)
    assert rc.snapshot()["sector_flags"] == []


def test_another_track_clears_the_sector_flags_and_a_late_read_of_the_old_one_is_ignored():
    rc = RaceControl({"session_type": 3, "track": "a"})
    rc.set_zones(("a", ""), trackzones.FALLBACK)
    rc.set_sector_flag(0, "yellow")
    rc.set_zones(("a", ""), list(trackzones.FALLBACK))
    assert rc.sector_flags, "the same track read again keeps them"
    rc.session_ref["track"] = "b"
    rc.set_zones(("b", ""), trackzones.FALLBACK[:2])
    assert rc.sector_flags == {} and len(rc.zones) == 2
    rc.set_sector_flag(1, "double")
    rc.set_zones(("a", ""), trackzones.FALLBACK)
    assert rc.zones_key == ("b", "") and rc.sector_flags == {1: "double"}, "an older read finishing late changes nothing"


def test_brackets_in_a_name_cannot_cut_the_list_the_hub_reads():
    assert trackzones.parse("[SECTION_0]\nIN=0.1\nOUT=0.2\nTEXT=Curva [1] {x}\n")[0]["name"] == "Curva 1 x"
