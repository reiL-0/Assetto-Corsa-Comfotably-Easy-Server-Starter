"""Live board (app/live/board.py): what the live page adds to ACSM's table — the session feed, contacts on the map, race gaps and places won since the grid."""

from app.live.board import CONTACT_KEEP_S, LiveBoard


def _board(session_type=1):
    b = LiveBoard()
    b.apply({"type": "new_session", "session_type": session_type, "name": "Race" if session_type == 3 else "Practice", "track": "spa", "track_config": ""}, now=0)
    for car, name in ((0, "Ana"), (1, "Beto")):
        b.apply({"type": "new_connection", "car_id": car, "driver_name": name, "driver_guid": f"7656119900000000{car}", "car_model": "gt3", "car_skin": "s"}, now=0)
    return b


def _lap(b, car, ms, cuts=0, now=0):
    b.apply({"type": "lap_completed", "car_id": car, "laptime_ms": ms, "cuts": cuts, "leaderboard": [{"car_id": car, "laps": 1, "laptime_ms": ms}]}, now=now)


def _at(b, car, spline, now, kmh=100.0):
    b.apply({"type": "car_update", "car_id": car, "pos": [0, 0, 0], "spline_pos": spline, "velocity": [kmh / 3.6, 0, 0]}, now=now)


def _row(b, name):
    lb = b.leaderboard(now=1000)
    return next(e for e in lb["ConnectedDrivers"] + lb["DisconnectedDrivers"] if e["CarInfo"]["DriverName"] == name)


def test_the_feed_says_who_joined_the_fastest_lap_a_personal_best_and_a_cut_lap():
    b = _board()
    _lap(b, 0, 100_000, now=10)
    _lap(b, 1, 99_000, now=20)       # quicker than Ana: fastest of the session
    _lap(b, 0, 99_500, now=30)       # Ana improves, still not the fastest
    _lap(b, 1, 95_000, cuts=2, now=40)
    kinds = [(e["kind"], e["name"]) for e in b.leaderboard(now=50)["Events"]]
    assert kinds == [("session", ""), ("join", "Ana"), ("join", "Beto"), ("best", "Ana"), ("best", "Beto"), ("personal", "Ana"), ("cut", "Beto")]
    assert b.events[-1]["cuts"] == 2 and _row(b, "Beto")["LastLapCuts"] == 2
    ids = [e["id"] for e in b.events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), "ids grow: the page shows each event once"


def test_a_sweep_answer_is_not_a_join_and_a_new_session_starts_the_feed_again():
    b = _board()
    b.apply({"type": "new_connection", "answer": True, "car_id": 0, "driver_name": "Ana", "driver_guid": "76561199000000000", "car_model": "gt3", "car_skin": "s"}, now=5)
    assert [e["kind"] for e in b.events].count("join") == 2
    b.apply({"type": "new_session", "session_type": 2, "name": "Qualify", "track": "spa", "track_config": ""}, now=60)
    assert [e["kind"] for e in b.events] == ["session"] and b.events[0]["session"] == "Qualify"


def test_a_contact_counts_for_both_cars_and_stays_on_the_map_ten_minutes():
    b = _board()
    b.apply({"type": "client_event", "event_type": 10, "car_id": 0, "other_car_id": 1, "speed": 42.4, "world_pos": [10.0, 1.0, -5.0], "rel_pos": [0, 0, 0]}, now=100)
    b.apply({"type": "client_event", "event_type": 11, "car_id": 1, "other_car_id": None, "speed": 80.0, "world_pos": [1.0, 0.0, 2.0], "rel_pos": [0, 0, 0]}, now=200)
    assert _row(b, "Ana")["Hits"] == 1 and _row(b, "Beto")["Hits"] == 2
    lb = b.leaderboard(now=300)
    assert lb["Contacts"][0] == {"at": 100, "x": 10.0, "z": -5.0, "car": True, "name": "Ana", "speed": 42}
    assert [e["kind"] for e in lb["Events"][-2:]] == ["contact", "wall"] and lb["Events"][-2]["other"] == "Beto"
    assert len(b.leaderboard(now=100 + CONTACT_KEEP_S + 1)["Contacts"]) == 1, "the first one faded"


def test_race_gap_and_interval_come_from_when_each_car_passed_the_same_point():
    b = _board(session_type=3)
    _at(b, 0, 0.98, 0)   # the grid: Ana in front
    _at(b, 1, 0.96, 0)
    t = 1.0
    for i in range(1, 50):   # both drive at the same pace, Beto 2 s behind
        _at(b, 0, (0.98 + i * 0.01) % 1, t)
        _at(b, 1, (0.96 + i * 0.01) % 1, t)
        t += 1.0
    _lap(b, 0, 60_000, now=t)  # Ana leads on laps
    beto = _row(b, "Beto")
    assert 1500 <= beto["GapMs"] <= 2500, beto
    assert beto["IntervalMs"] == beto["GapMs"], "two cars: the car ahead is the leader"
    assert beto["PosChange"] == 0 and _row(b, "Ana")["PosChange"] == 0
    assert "GapMs" not in _row(b, "Ana")


def test_a_car_a_lap_down_shows_laps_not_milliseconds():
    b = _board(session_type=3)
    _at(b, 0, 0.95, 0)
    _at(b, 1, 0.94, 0)
    t = 1.0
    for lap in range(2):   # Ana drives two laps, Beto stays where he is
        for s in (0.05, 0.3, 0.6, 0.9, 0.95):
            _at(b, 0, s, t)
            t += 1.0
    _at(b, 1, 0.94, t)
    _lap(b, 0, 60_000, now=t)
    assert _row(b, "Beto")["GapLaps"] == 2


def test_points_skipped_between_two_packets_are_filled_so_a_short_lap_still_has_gaps():
    b = _board(session_type=3)
    _at(b, 0, 0.99, 0)
    _at(b, 1, 0.98, 0)
    t = 1.0
    for i in range(1, 30):   # 5 hundredths of a lap per packet: 4 points skipped every time
        _at(b, 0, (0.99 + i * 0.05) % 1, t)
        _at(b, 1, (0.98 + i * 0.05) % 1, t)
        t += 1.0
    _lap(b, 0, 20_000, now=t)
    assert 100 <= _row(b, "Beto")["GapMs"] <= 400, _row(b, "Beto")


def test_the_grid_puts_a_car_just_past_the_line_ahead_and_a_new_driver_in_a_slot_starts_from_nothing():
    b = _board(session_type=3)
    _at(b, 0, 0.97, 0)
    _at(b, 1, 0.02, 0)   # Beto starts past the line: he is ahead
    assert b.grid == {1: 1, 0: 2}
    b.apply({"type": "connection_closed", "car_id": 1}, now=1)
    assert 1 not in b._wraps and 1 not in b._marks, "the slot's progress went with its driver"


def test_both_reports_of_one_contact_count_once_and_a_lap_ruled_invalid_without_cuts_is_not_called_a_cut():
    b = _board()
    for car, other in ((0, 1), (1, 0)):   # acServer sends one per car
        b.apply({"type": "client_event", "event_type": 10, "car_id": car, "other_car_id": other, "speed": 30.0, "world_pos": [0, 0, 0], "rel_pos": [0, 0, 0]}, now=100)
    assert _row(b, "Ana")["Hits"] == 1 and _row(b, "Beto")["Hits"] == 1 and len(b.contacts) == 1
    b.apply({"type": "client_event", "event_type": 10, "car_id": 0, "other_car_id": 1, "speed": 30.0, "world_pos": [0, 0, 0], "rel_pos": [0, 0, 0]}, now=110)
    assert _row(b, "Ana")["Hits"] == 2, "a later touch is another contact"
    b.invalidate_lap(0)
    _lap(b, 0, 90_000, cuts=0, now=120)
    assert b.events[-1]["kind"] == "invalid" and b.events[-1]["cuts"] == 0


def test_stopped_is_a_connected_car_below_walking_pace():
    b = _board()
    _at(b, 0, 0.3, 1, kmh=2)
    _at(b, 1, 0.3, 1, kmh=150)
    assert _row(b, "Ana")["Stopped"] is True and _row(b, "Beto")["Stopped"] is False
    assert _row(b, "Beto")["SpeedKmh"] == 150
