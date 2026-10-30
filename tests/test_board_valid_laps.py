"""Live board (app/live/board.py): the valid laps of each driver (no cuts) and the quickest of them, kept for the whole session, also after the driver left."""

from app.live.board import NS, LiveBoard


def _lap(b, car, ms, cuts):
    b.apply({"type": "lap_completed", "car_id": car, "laptime_ms": ms, "cuts": cuts, "leaderboard": [{"car_id": car, "laps": 1, "laptime_ms": ms}]})


def _board():
    b = LiveBoard()
    b.apply({"type": "new_session", "session_type": 1, "track": "spa", "track_config": ""})
    for car, name in ((0, "Ana"), (1, "Beto")):
        b.apply({"type": "new_connection", "car_id": car, "driver_name": name, "driver_guid": f"7656119900000000{car}", "car_model": "gt3", "car_skin": "s"})
    return b


def _cars(board, group):
    return {e["CarInfo"]["DriverName"]: next(iter(e["Cars"].values())) for e in board.leaderboard()[group]}


def test_valid_laps_and_the_quickest_valid_lap_stay_after_the_driver_leaves_until_the_next_session():
    b = _board()
    _lap(b, 0, 103_000, 0)
    _lap(b, 0, 99_000, 2)    # quicker but cut: not valid
    _lap(b, 0, 101_500, 0)
    assert _cars(b, "ConnectedDrivers")["Ana"]["ValidLaps"] == 2
    assert _cars(b, "ConnectedDrivers")["Ana"]["BestValidLap"] == 101_500 * NS
    assert _cars(b, "ConnectedDrivers")["Ana"]["NumLaps"] == 3, "the lap count is every lap, as before"
    b.apply({"type": "connection_closed", "car_id": 0})
    assert _cars(b, "DisconnectedDrivers")["Ana"]["ValidLaps"] == 2 and _cars(b, "DisconnectedDrivers")["Ana"]["BestValidLap"] == 101_500 * NS
    assert "ValidLaps" not in _cars(b, "ConnectedDrivers")["Beto"], "no lap yet: nothing to show"
    b.apply({"type": "new_session", "session_type": 2, "track": "spa", "track_config": ""})   # the session changed: its own table
    assert _cars(b, "DisconnectedDrivers") == {} and "ValidLaps" not in _cars(b, "ConnectedDrivers")["Beto"]


def test_a_driver_whose_laps_are_all_cut_has_zero_valid_laps_and_no_best():
    b = _board()
    _lap(b, 1, 98_000, 1)
    c = _cars(b, "ConnectedDrivers")["Beto"]
    assert c["ValidLaps"] == 0 and c["BestValidLap"] == 0


def test_a_lap_without_a_real_time_is_not_a_valid_lap():
    b = _board()
    _lap(b, 0, 0, 0)
    _lap(b, 0, 2**31, 0)   # acServer's "no time" sentinel
    c = _cars(b, "ConnectedDrivers")["Ana"]
    assert c["ValidLaps"] == 0 and c["BestValidLap"] == 0
