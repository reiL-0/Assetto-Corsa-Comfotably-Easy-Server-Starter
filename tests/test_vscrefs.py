"""app/vscrefs.py: the VSC's reference laps in the database, one row per track layout and car model (the fastest), between sessions."""
from types import SimpleNamespace

from sqlmodel import Session, select

from app import racecontrol as adapter, supervisor, vscrefs
from app.db import engine
from app.live import racecontrol as live_rc
from app.models import VscRef


def test_one_row_per_combination_and_only_a_faster_lap_replaces_it():
    vscrefs.save("spa", "", {"gt3": (140.0, [10.0] * 20), "nsx": (150.0, [11.0] * 20)})
    vscrefs.save("spa", "", {"gt3": (141.0, [9.0] * 20)})   # slower: not kept
    assert vscrefs.load("spa", "")["gt3"] == (140.0, [10.0] * 20)
    vscrefs.save("spa", "", {"gt3": (139.5, [9.9] * 20)})   # quicker: replaces the row, no second row
    vscrefs.save("spa", "gp", {"gt3": (200.0, [15.0] * 20)})   # another layout: its own row
    assert vscrefs.load("spa", "")["gt3"][0] == 139.5 and vscrefs.load("spa", "gp")["gt3"][0] == 200.0 and set(vscrefs.load("spa", "")) == {"gt3", "nsx"}
    with Session(engine) as s:
        assert len(s.exec(select(VscRef).where(VscRef.track == "spa", VscRef.track_config == "", VscRef.car_model == "gt3")).all()) == 1
    assert vscrefs.key_of({"track": "csp/3898/../spa", "track_config": "gp"}) == ("spa", "gp") and vscrefs.key_of({}) is None


def test_the_monitor_sync_loads_the_stored_laps_into_a_new_session_and_writes_the_new_ones(monkeypatch):
    sid = 991
    vscrefs.save("imola", "", {"gt3": (100.0, [5.0] * 20)})
    session = {"session_type": 3, "track": "imola", "track_config": ""}
    inst = SimpleNamespace(server_id=sid, acsp=SimpleNamespace(session=session), running=True)
    monkeypatch.setattr(supervisor, "live", lambda: [inst])
    try:
        rc = live_rc.state_of(sid, True, session, create=True)
        assert rc.reflaps.best == {}
        adapter.vsc_refs_sync()
        assert rc.reflaps.best["gt3"] == (100.0, [5.0] * 20), "a state that had no laps takes the stored ones"
        assert set(rc.reflaps.profiles(1.35)) == {"gt3"}
        rc.reflaps.best["gt3"] = (99.0, [4.9] * 20); rc.reflaps.dirty.add("gt3")   # a quicker lap learned in this session
        rc.reflaps.best["nsx"] = (120.0, [6.0] * 20); rc.reflaps.dirty.add("nsx")
        adapter.vsc_refs_sync()
        assert vscrefs.load("imola", "")["gt3"][0] == 99.0 and vscrefs.load("imola", "")["nsx"][0] == 120.0, "written"
        assert not rc.reflaps.dirty
    finally:
        live_rc._states.pop(sid, None)
