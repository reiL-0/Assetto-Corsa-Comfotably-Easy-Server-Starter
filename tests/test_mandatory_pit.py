"""The mandatory pit stop (rule `mandatory_pit`, RaceControl._mpit_*): what counts as the stop, the drive-through when the window closes, the seconds at the chequered flag."""
import pytest
from pydantic import ValidationError

from app.live import racecontrol as rcmod
from app.live.racecontrol import RaceControl
from app.rulebook import Detectors, MandatoryPitRule


def rc_with(**rule):
    rcmod._flag_dues_pending.clear()
    rc = RaceControl(session_ref={"session_type": 3})
    rc.server_id = 9
    rc.rules = {**rcmod.FLAG_DEFAULTS, "mandatory_pit": MandatoryPitRule(**rule).model_dump()}
    rc.audit_guids = {c: f"G{c}" for c in range(1, 5)}
    return rc


def stop(rc, car, t, *, stood=True, **data):
    rc.report_pit(car, True, now=t, kmh=0 if stood else 80)
    rc.report_pit(car, False, now=t + 20, stop=data)


def dues(rc, guid):
    return [d for d in rcmod._flag_dues_pending.values() if d["driver_guid"] == guid and d["kind"] == "mandatory_pit"]


def test_only_a_stop_in_the_window_that_did_what_the_rule_asks_counts_and_the_window_closing_is_a_drive_through():
    rc = rc_with(from_lap=3, to_lap=6, require_tyres=True, require_fuel=True, min_fuel_l=10, unserved_s=25)
    for car in (1, 2, 3, 4):
        rc.lap_done(car, 90000, 0, 2, 100.0)   # (on lap 3: the window is open)
    stop(rc, 1, 110.0, tyres=True, fuel=30.0)                 # valid
    stop(rc, 2, 110.0, tyres=True, fuel=2.0)                  # not enough fuel
    stop(rc, 3, 110.0, stood=False, tyres=True, fuel=30.0)    # never stood still (the manager's own reading): the hub's word is not enough
    stop(rc, 4, 110.0, fuel=30.0)                             # no tyres
    assert rc.mpit_done == {"G1"}
    for car in (1, 2, 3, 4):
        rc.lap_done(car, 90000, 0, 6, 500.0)
    assert not dues(rc, "G1")
    for g in ("G2", "G3", "G4"):
        [d] = dues(rc, g)
        assert d["seconds"] == 25   # (a drive-through: its seconds only if never served)
    assert len(rc.flag_dt_open) == 3
    rc.set_flag("checkered")
    assert all(len(dues(rc, g)) == 1 for g in ("G2", "G3", "G4")), "charged once per race"
    assert rc.snapshot()["mandatory_pit"]["made"] == [1] and rc.snapshot()["mandatory_pit"]["charged"] == [2, 3, 4]


def test_a_stop_before_the_window_does_not_count_and_the_chequered_flag_puts_seconds_on_whoever_reached_the_window():
    rc = rc_with(from_lap=5, to_lap=0, unserved_s=20)
    rc.lap_done(1, 90000, 0, 1, 100.0)
    stop(rc, 1, 110.0, tyres=True)            # lap 2: too early
    rc.lap_done(2, 90000, 0, 4, 100.0)
    stop(rc, 2, 110.0, tyres=True)            # lap 5: valid
    rc.lap_done(1, 90000, 0, 2, 200.0)        # car 1 never reaches lap 5
    rc.lap_done(3, 90000, 0, 9, 200.0)        # car 3 did, never stopped
    rc.set_flag("checkered")
    assert not dues(rc, "G1") and not dues(rc, "G2")
    [d] = dues(rc, "G3")
    assert d["seconds"] == 20 and not rc.flag_dt_open, "at the flag: seconds, no drive-through"


def test_two_compounds_and_reconnection_by_driver_and_no_rule_outside_a_race():
    rc = rc_with(two_compounds=True)
    rc.lap_done(1, 90000, 0, 3, 100.0)
    stop(rc, 1, 110.0, tyres=True, cfrom=1, cto=1)   # same compound: the stop is made, the two compounds are not
    rc.audit_guids[2] = "G1"                          # he reconnected in another slot
    rc.lap_done(2, 90000, 0, 5, 200.0)
    rc.set_flag("checkered")
    [d] = dues(rc, "G1")
    assert d["seconds"] == 30
    rc2 = rc_with(); rc2.session_ref = {"session_type": 2}
    rc2.lap_done(1, 90000, 0, 3, 100.0); rc2.set_flag("checkered")
    assert not rcmod._flag_dues_pending and rc2.snapshot()["mandatory_pit"] is None
    with pytest.raises(ValidationError):
        MandatoryPitRule(from_lap=10, to_lap=5)
    assert Detectors().mandatory_pit is None
