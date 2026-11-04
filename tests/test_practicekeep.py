"""app/practicekeep.py: the practice (best laps + grip) survives an idle stop and is gone when the session kind changes."""

from app import practicekeep
from app.live.board import NS, LiveBoard

GUID = "76561199000000001"


def _lap(b, car, ms, cuts=0):
    b.apply({"type": "lap_completed", "car_id": car, "laptime_ms": ms, "cuts": cuts, "leaderboard": [{"car_id": car, "laps": 1, "laptime_ms": ms}]})


def _practice_board():
    b = LiveBoard()
    b.apply({"type": "new_session", "session_type": 1, "track": "spa", "track_config": ""})
    b.apply({"type": "new_connection", "car_id": 0, "driver_name": "Ana", "driver_guid": GUID, "car_model": "gt3", "car_skin": "s"})
    return b


def test_kept_best_survives_the_restart_and_a_slower_lap_does_not_replace_it():
    old = _practice_board()
    _lap(old, 0, 100_000)
    practicekeep.save(901, old, 0.93)
    kept = practicekeep.load(901)
    assert round(kept.grip, 2) == 0.93 and kept.drivers[0]["best_ms"] == 100_000

    new = LiveBoard()   # the server woke up: empty board, then the session arrives
    practicekeep.seed(new, kept.drivers)
    new.apply({"type": "session_info", "session_type": 1, "track": "spa", "track_config": ""})
    lb = new.leaderboard()
    assert next(iter(lb["DisconnectedDrivers"][0]["Cars"].values()))["BestLap"] == 100_000 * NS   # listed, not connected
    new.apply({"type": "new_connection", "car_id": 3, "driver_name": "Ana", "driver_guid": GUID, "car_model": "gt3", "car_skin": "s"})
    _lap(new, 3, 105_000)   # slower than the kept one: acServer's own table says 105 s
    car = next(iter(new.leaderboard()["ConnectedDrivers"][0]["Cars"].values()))
    assert car["BestLap"] == 100_000 * NS
    _lap(new, 3, 98_000)
    assert next(iter(new.leaderboard()["ConnectedDrivers"][0]["Cars"].values()))["BestLap"] == 98_000 * NS


def test_clear_and_grip():
    b = _practice_board()
    _lap(b, 0, 100_000)
    practicekeep.save(902, b, 0.9)
    practicekeep.clear(902)
    assert practicekeep.load(902) is None
    cfg = {"PRACTICE": {}, "DYNAMIC_TRACK": {"SESSION_START": 95, "RANDOMNESS": 2}}
    assert practicekeep.with_grip(cfg, 0.934)["DYNAMIC_TRACK"] == {"SESSION_START": 93, "RANDOMNESS": 2}
    assert cfg["DYNAMIC_TRACK"]["SESSION_START"] == 95   # the saved config is not touched
    assert practicekeep.with_grip({"PRACTICE": {}}, 0.9) == {"PRACTICE": {}}   # no dynamic track: nothing to set
    assert practicekeep.with_grip(cfg, None) is cfg


def _seeded():
    old = _practice_board()
    _lap(old, 0, 100_000)
    practicekeep.save(903, old, 0.9)
    new = LiveBoard()
    practicekeep.seed(new, practicekeep.load(903).drivers)
    return new


def test_a_practice_session_after_the_seed_keeps_the_drivers_and_another_kind_drops_them():
    b = _seeded()
    b.apply({"type": "new_session", "session_type": 1, "track": "spa", "track_config": ""})   # the practice starts for real
    assert len(b.leaderboard()["DisconnectedDrivers"]) == 1
    b.apply({"type": "new_session", "session_type": 1, "track": "spa", "track_config": ""})   # a second one is a new practice: the table starts over
    assert b.leaderboard()["DisconnectedDrivers"] == []

    b = _seeded()
    b.apply({"type": "new_connection", "car_id": 2, "driver_name": "Ana", "driver_guid": GUID, "car_model": "gt3", "car_skin": "s"})
    b.apply({"type": "new_session", "session_type": 3, "track": "spa", "track_config": ""})   # the race: no best lap of the practice may cap a lap
    _lap(b, 2, 110_000)
    assert next(iter(b.leaderboard()["ConnectedDrivers"][0]["Cars"].values()))["BestLap"] == 110_000 * NS


def test_found_running_in_another_kind_of_session_the_kept_practice_goes():
    b = _seeded()
    assert b.has_kept()
    b.apply({"type": "session_info", "session_type": 3, "track": "spa", "track_config": ""})
    b.drop_kept()   # (what ACSPClient._apply does when has_kept() and the session is not a practice)
    assert not b.has_kept() and b.leaderboard()["DisconnectedDrivers"] == []


def test_session_info_that_finds_the_practice_consumes_the_seed_and_a_returning_driver_starts_from_zero_in_the_race():
    b = _seeded()
    b.apply({"type": "session_info", "session_type": 1, "track": "spa", "track_config": ""})
    b.apply({"type": "new_session", "session_type": 1, "track": "spa", "track_config": ""})   # a later practice is a new one
    assert b.leaderboard()["DisconnectedDrivers"] == []

    b = _seeded()
    b.apply({"type": "new_connection", "car_id": 2, "driver_name": "Ana", "driver_guid": GUID, "car_model": "gt3", "car_skin": "s"})
    b.apply({"type": "session_info", "session_type": 3, "track": "spa", "track_config": ""})
    b.drop_kept()   # (ACSPClient._apply, because has_kept())
    car = next(iter(b.leaderboard()["ConnectedDrivers"][0]["Cars"].values()))
    assert car["BestLap"] == 0 and car["NumLaps"] == 0
