"""app/vscrefs.py: the VSC's reference laps in the database, one row per track layout and car model, chosen from a window of recent laps (an outlier is never the reference)."""
from types import SimpleNamespace

from sqlmodel import Session, select

from app import racecontrol as adapter, supervisor, vscrefs
from app.db import engine
from app.live import racecontrol as live_rc
from app.live.sectors import KEEP_LAPS, MAX_AVG_MS, RefLaps, reference
from app.models import VscRef

S = lambda lap: [lap / 20] * 20   # a lap's 20 sector times


def laps(*times):
    return [(t, S(t)) for t in times]


def test_the_reference_is_the_quickest_lap_that_is_no_outlier_of_the_median():
    assert reference([]) is None
    assert reference(laps(20.0)) == (20.0, S(20.0)), "one lap: nothing to compare with"
    assert reference(laps(20.0, 80.0))[0] == 20.0, "two laps: still no average"
    assert reference(laps(80.0, 81.0, 20.0, 82.0, 80.5))[0] == 80.0, "a 20 s lap on a 1:20 track is left out: the quickest lap near the average wins"
    assert reference(laps(80.0, 81.0, 79.0, 82.0))[0] == 79.0 and reference(laps(80.0, 70.0, 71.0, 69.0))[0] == 69.0, "a real improvement (the median moves) is a reference"


def test_a_lap_too_quick_for_the_track_length_is_not_even_kept():
    r = RefLaps()
    r.track_m = 5000.0   # 5 km: a 20 s lap would be 250 m/s
    r.add_lap("gt3", 20.0, S(20.0))
    assert r.recent == {} and r.best == {} and r.fresh == {}
    r.add_lap("gt3", 80.0, S(80.0))
    assert r.best["gt3"][0] == 80.0 and 5000.0 / 80.0 < MAX_AVG_MS


def test_the_window_keeps_the_last_laps_and_a_bad_reference_heals_as_it_rolls():
    r = RefLaps()
    for t in (80.0, 81.0, 20.0):   # with no track length known the 20 s lap gets in; the median leaves it out as soon as there are 3 laps
        r.add_lap("gt3", t, S(t))
    assert r.best["gt3"][0] == 80.0
    for t in [80.5] * KEEP_LAPS:   # the window rolls: the outlier is gone from it altogether
        r.add_lap("gt3", t, S(t))
    assert len(r.recent["gt3"]) == KEEP_LAPS and all(lap == 80.5 for lap, _ in r.recent["gt3"]) and r.best["gt3"][0] == 80.5


def test_one_row_per_combination_with_its_window_and_a_poisoned_row_heals():
    vscrefs.save("spa", "", {"gt3": laps(140.0, 141.0), "nsx": laps(150.0)})
    vscrefs.save("spa", "gp", {"gt3": laps(200.0)})   # another layout: its own row
    vscrefs.save("spa", "", {"gt3": laps(40.0)})   # an outlier saved at some point (the first 3 laps had no average to compare with)
    with Session(engine) as s:
        rows = s.exec(select(VscRef).where(VscRef.track == "spa", VscRef.track_config == "", VscRef.car_model == "gt3")).all()
    assert len(rows) == 1 and len(rows[0].recent_laps) == 3 and rows[0].lap_s == 140.0, "one row; the 40 s lap is in the window but is not the reference"
    assert vscrefs.load("spa", "")["gt3"][0] == (140.0, S(140.0)) and set(vscrefs.load("spa", "")) == {"gt3", "nsx"} and vscrefs.load("spa", "gp")["gt3"][0][0] == 200.0
    vscrefs.save("spa", "", {"gt3": laps(*[141.0] * KEEP_LAPS)})
    assert len(vscrefs.load("spa", "")["gt3"]) == KEEP_LAPS
    assert min(lap for lap, _ in vscrefs.load("spa", "")["gt3"]) > 100.0, "after KEEP_LAPS laps the outlier has left the window"
    assert vscrefs.key_of({"track": "csp/3898/../spa", "track_config": "gp"}) == ("spa", "gp") and vscrefs.key_of({}) is None


def test_the_monitor_sync_loads_the_stored_windows_and_writes_the_new_laps(monkeypatch):
    sid = 991
    vscrefs.save("imola", "", {"gt3": laps(100.0, 101.0, 102.0)})
    session = {"session_type": 3, "track": "imola", "track_config": ""}
    inst = SimpleNamespace(server_id=sid, acsp=SimpleNamespace(session=session), running=True)
    monkeypatch.setattr(supervisor, "live", lambda: [inst])
    try:
        rc = live_rc.state_of(sid, True, session, create=True)
        rc.reflaps.add_lap("nsx", 120.0, S(120.0))   # learned before the first sync
        adapter.vsc_refs_sync()
        assert rc.reflaps.best["gt3"][0] == 100.0 and set(rc.reflaps.profiles(1.35)) == {"gt3", "nsx"}, "a state takes the stored laps and keeps its own"
        rc.reflaps.add_lap("gt3", 99.0, S(99.0))
        adapter.vsc_refs_sync()
        stored = vscrefs.load("imola", "")
        assert [lap for lap, _ in stored["gt3"]] == [100.0, 101.0, 102.0, 99.0] and [lap for lap, _ in stored["nsx"]] == [120.0], "written, appended to the window"
        assert not rc.reflaps.fresh
    finally:
        live_rc._states.pop(sid, None)


def test_a_vsc_refs_table_from_before_the_window_column_is_migrated(tmp_path):
    import sqlite3
    from app import db
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute('CREATE TABLE vsc_refs (track VARCHAR NOT NULL, track_config VARCHAR NOT NULL, car_model VARCHAR NOT NULL, lap_s FLOAT NOT NULL, sectors JSON NOT NULL, set_at DATETIME NOT NULL, PRIMARY KEY (track, track_config, car_model))')
    con.execute("INSERT INTO vsc_refs VALUES ('spa', '', 'gt3', 140.0, '[]', '2026-11-13')")
    db._m016_vsc_refs_window(con)
    db._m016_vsc_refs_window(con)   # idempotent
    assert con.execute("SELECT recent_laps FROM vsc_refs").fetchone()[0] == "[]"
