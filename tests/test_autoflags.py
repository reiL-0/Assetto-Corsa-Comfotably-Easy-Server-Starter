"""Blue flag and danger warning in shadow (app/live/autoflags.py), on a synthetic 3000 m track: what it decides for cars at known places."""

from app.live.autoflags import AutoFlags
from app.live.field import Sample

L = 3000.0


def smp(metres, now=100.0, since=0.0, epoch=1):
    return Sample(metres / L, epoch, 0, now, since)


def run(af, cars, speeds=None, now=100.0, pit=(), length=L, racing=True):
    """cars: {id: metres of progress}"""
    af.step(now, {c: smp(m, now) for c, m in cars.items()}, speeds if speeds is not None else {c: 150.0 for c in cars}, set(pit), length, racing)



def _pace(rc, *cars):
    """The blue flag needs to know the pace of both cars (app/live/flagpace.py): the higher car number is the quicker one here."""
    for c in cars:
        for _ in range(3):
            rc.pace.lap(c, 100000 - c * 1000, True)

def test_blue_for_the_car_a_leader_is_about_to_lap_with_hysteresis_and_end_on_pass():
    af = AutoFlags()
    # car 2 is a lap ahead of car 1 in progress and 120 m behind it on the track
    run(af, {1: 4000.0, 2: 4000.0 + 3000.0 - 120.0})
    assert list(af.blue) == [(1, 2)] and abs(af.blue[(1, 2)] - 120.0) < 1e-6, "car 2 is 120 m behind car 1 and a lap ahead: car 1 gets blue"
    run(af, {1: 4000.0, 2: 4000.0 + 3000.0 - 200.0}, now=101.0)
    assert (1, 2) in af.blue, "200 m: inside the hysteresis, it stays"
    run(af, {1: 4000.0, 2: 4000.0 + 3000.0 - 300.0}, now=102.0)
    assert af.blue == {}, "300 m: gone"
    run(af, {1: 4000.0, 2: 4000.0 + 3000.0 - 200.0}, now=103.0)
    assert af.blue == {}, "200 m again: it does not come back until 150"
    run(af, {1: 4000.0, 2: 4000.0 + 3000.0 - 100.0}, now=104.0)
    run(af, {1: 4000.0, 2: 4000.0 + 3000.0 + 40.0}, now=105.0)
    assert af.blue == {}, "car 2 passed car 1"
    assert [e["op"] for e in af.log if e["kind"] == "blue"] == ["open", "close", "open", "close"]


def test_no_blue_without_a_lap_between_them_or_for_the_car_behind():
    af = AutoFlags()
    run(af, {1: 4000.0, 2: 3900.0})
    assert af.blue == {}, "car 2 is just behind car 1 in the same lap: they are racing, not lapping"
    run(af, {1: 4000.0, 2: 4000.0 + 3000.0 - 100.0}, now=101.0)
    assert (2, 1) not in af.blue and (1, 2) in af.blue


def test_cars_that_cannot_be_trusted_take_no_part():
    af = AutoFlags()
    cars = {1: 4000.0, 2: 6900.0}
    run(af, cars, pit=[2])
    assert af.blue == {}, "in the pit lane"
    af.step(100.0, {1: smp(4000.0, 100.0), 2: Sample(6900.0 / L, 1, 0, 100.0, 99.5)}, {1: 150.0, 2: 150.0}, set(), L, True)
    assert af.blue == {}, "just re-anchored (0.5 s of continuity)"
    af.step(100.0, {1: smp(4000.0, 100.0), 2: Sample(6900.0 / L, 1, 0, 97.0, 0.0)}, {1: 150.0, 2: 150.0}, set(), L, True)
    assert af.blue == {}, "stale sample"


def test_without_the_length_or_off_the_race_it_decides_nothing_and_closes_what_it_had():
    af = AutoFlags()
    run(af, {1: 4000.0, 2: 6900.0})
    assert af.blue
    run(af, {1: 4000.0, 2: 6900.0}, now=101.0, length=None)
    assert af.blue == {} and af.unknown_length and af.log[-1]["op"] == "close"
    run(af, {1: 4000.0, 2: 6900.0}, now=102.0, racing=False)
    assert af.blue == {} and not af.unknown_length


def test_danger_counts_down_for_the_cars_approaching_a_stopped_car_and_ends_when_it_moves():
    af = AutoFlags()
    run(af, {1: 1000.0, 2: 700.0, 3: 100.0}, {1: 150.0, 2: 150.0, 3: 150.0}, now=90.0)   # (car 1 was racing)
    for i in range(5):   # car 1 stops, 3 s
        run(af, {1: 1000.0, 2: 700.0 + i, 3: 100.0}, {1: 0.0, 2: 150.0, 3: 150.0}, now=96.0 + i)
    assert af.danger == {(2, 1): 300}, "car 2 is ~296 m behind the stopped car (rounded to 10); car 3 is too far"
    run(af, {1: 1000.0, 2: 900.0, 3: 100.0}, {1: 0.0, 2: 150.0, 3: 150.0}, now=101.0)
    assert af.danger == {(2, 1): 100}
    run(af, {1: 1000.0, 2: 1020.0, 3: 100.0}, {1: 0.0, 2: 150.0, 3: 150.0}, now=102.0)
    assert af.danger == {}, "car 2 passed the stopped car"
    run(af, {1: 1000.0, 2: 1020.0, 3: 800.0}, {1: 0.0, 2: 150.0, 3: 150.0}, now=103.0)
    assert af.danger == {(3, 1): 200}
    run(af, {1: 1005.0, 2: 1020.0, 3: 810.0}, {1: 60.0, 2: 150.0, 3: 150.0}, now=104.0)
    assert af.danger == {}, "the stopped car moves: over"


def test_a_car_that_never_raced_is_not_a_hazard():
    af = AutoFlags()
    for i in range(6):
        run(af, {1: 1000.0, 2: 700.0}, {1: 0.0, 2: 150.0}, now=100.0 + i)
    assert af.danger == {}, "car 1 was never moving: a spectator or the garage"


def test_the_registry_runs_it_in_a_race_only():
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    _pace(rc, 1, 2)
    cars = {1: smp(4000.0, 100.0), 2: smp(6880.0, 100.0)}
    rc.autoflags_tick(cars, {1: 150.0, 2: 150.0}, L, 100.0, 1000.0)
    assert rc.autoflags.view()["blue"] == [{"car": 1, "by": 2, "m": 120}]
    rc.pace_car = 2
    rc.autoflags_tick(cars, {1: 150.0, 2: 150.0}, L, 101.0, 1001.0)
    assert rc.autoflags.view()["blue"] == [], "the pace car is not a competitor"
    q = RaceControl({"session_type": 1})
    q.autoflags_tick(cars, {1: 150.0, 2: 150.0}, L, 100.0, 1000.0)
    assert q.autoflags.view()["blue"] == [], "not a race"


def test_a_slot_that_changes_hands_between_two_looks_forgets_its_last_driver():
    af = AutoFlags()
    run(af, {1: 1000.0, 2: 700.0}, {1: 150.0, 2: 150.0}, now=90.0)   # car 1 was racing
    for i in range(5):
        run(af, {1: 1000.0, 2: 700.0}, {1: 0.0, 2: 150.0}, now=96.0 + i)
    assert af.danger, "car 1 is stopped"
    # the driver left and somebody else sits in slot 1, stopped on the same spot, never moved: generation 1
    new = {1: Sample(1000.0 / L, 1, 1, 101.0, 0.0), 2: smp(700.0, 101.0)}
    af.step(101.0, new, {1: 0.0, 2: 150.0}, set(), L, True)
    af.step(102.0, {1: Sample(1000.0 / L, 1, 1, 102.0, 0.0), 2: smp(700.0, 102.0)}, {1: 0.0, 2: 150.0}, set(), L, True)
    assert af.danger == {}, "the new driver never moved: he is not a stopped racer"


def _live_rc(**modes):
    from app.live.racecontrol import RACE_TYPE, RaceControl

    rc = RaceControl({"session_type": RACE_TYPE})
    rc.flag_modes = {"blue": "shadow", "danger": "shadow", **modes}
    _pace(rc, 1, 2)
    cars = {1: smp(4000.0, 100.0), 2: smp(6880.0, 100.0)}
    rc.autoflags_tick(cars, {1: 150.0, 2: 150.0}, L, 100.0, 1000.0)
    return rc


def test_a_screen_gets_only_live_types_from_a_fresh_look_for_its_own_slot():
    assert _live_rc().auto_for(1, 0, 100.5) is None, "shadow: nothing reaches the screen"
    rc = _live_rc(blue="live")
    assert rc.auto_for(1, 0, 100.5) == {"blue": {"by": 2, "m": 120, "left_s": 15, "n": 0, "step": 0, "ignored": False}, "danger": None, "age_ms": 500, "session": rc.autoflags.uid}
    assert rc.auto_for(2, 0, 100.5) is None, "the faster car has no flag"
    assert rc.auto_for(1, 1, 100.5) is None, "the slot changed hands since the look"
    assert rc.auto_for(1, 0, 104.0) is None, "a look older than 3 s is not served"
    rc.auto_off.add("blue")
    rc.autoflags_tick({1: smp(4000.0, 101.0), 2: smp(6880.0, 101.0)}, {1: 150.0, 2: 150.0}, L, 101.0, 1001.0)
    assert rc.auto_for(1, 0, 101.2) is None and rc.autoflags.blue == {}, "the kill switch: not served and not even decided"
    rc.auto_off.clear()
    rc.flag = "yellow"
    assert rc.auto_for(1, 0, 101.2) is None, "not under green"


def test_the_rulebook_defaults_to_live_and_an_old_shadow_reads_as_live():
    from app.rulebook import Rulebook

    assert Rulebook().detectors.flags.model_dump() == {k: "live" for k in ("blue", "danger", "yellow", "meatball", "black", "red")}, "every flag rule is born live: there is no shadow"
    assert Rulebook.model_validate({"detectors": {"flags": {"blue": "off"}}}).detectors.flags.danger == "live"
    assert Rulebook.model_validate({"detectors": {"flags": {"blue": "shadow"}}}).detectors.flags.blue == "live", "a rulebook saved in the shadow days"
    import pytest
    with pytest.raises(Exception):
        Rulebook.model_validate({"detectors": {"flags": {"blue": "maybe"}}})


def test_a_failed_or_changed_reading_of_the_league_takes_a_live_flag_away():
    rc = _live_rc(blue="live")
    assert rc.auto_for(1, 0, 100.5, {"blue": "live", "danger": "shadow"})
    assert rc.auto_for(1, 0, 100.5, {}) is None, "the reading failed"
    assert rc.auto_for(1, 0, 100.5, {"blue": "shadow", "danger": "shadow"}) is None, "the league changed it to shadow"


def test_a_server_with_no_rulebook_takes_its_default_modes_from_the_deployment(monkeypatch):
    from app import league
    from app.config import settings
    for default in ("shadow", "live"):
        monkeypatch.setattr(settings, "flag_modes_default", default)
        league._EFFECTIVE.clear()
        assert set(league.flag_modes(987001).values()) == {default}, default
    league._EFFECTIVE.clear()
