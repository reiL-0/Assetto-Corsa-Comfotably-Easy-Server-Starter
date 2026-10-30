import json

from app.results import parse_result_file

RACE_JSON = {
    "Type": "Race",
    "TrackName": "spa",
    "TrackConfig": "",
    "Name": "Race",
    "Result": [
        {"DriverName": "Alice", "DriverGuid": "1", "CarModel": "car_a", "BestLap": 90000, "TotalTime": 600000},
        {"DriverName": "Bob", "DriverGuid": "2", "CarModel": "car_b", "BestLap": 91000, "TotalTime": 605000},
    ],
    "Laps": [
        {"DriverName": "Alice", "DriverGuid": "1", "CarId": 0, "LapTime": 90000, "Cuts": 0},
        {"DriverName": "Bob", "DriverGuid": "2", "CarId": 1, "LapTime": 91000, "Cuts": 1},
    ],
}


def test_parse_race_result_computes_position_and_gap(tmp_path):
    p = tmp_path / "race.json"
    p.write_text(json.dumps(RACE_JSON))

    parsed = parse_result_file(p)
    assert parsed["type"] == "Race"
    assert parsed["track"] == "spa"
    assert [e["position"] for e in parsed["classification"]] == [1, 2]
    assert parsed["classification"][0]["gap_ms"] is None
    assert parsed["classification"][1]["gap_ms"] == 5000
    assert len(parsed["laps"]) == 2


def test_parse_qualify_result_gaps_by_best_lap(tmp_path):
    data = dict(RACE_JSON, Type="Qualify")
    p = tmp_path / "q.json"
    p.write_text(json.dumps(data))

    parsed = parse_result_file(p)
    assert parsed["classification"][1]["gap_ms"] == 1000  # 91000 - 90000


def test_empty_slots_and_cars_without_a_lap_are_not_given_a_time(tmp_path):
    data = {**RACE_JSON, "Type": "Qualify", "Result": [
        {"DriverName": "Alice", "DriverGuid": "1", "CarModel": "car_a", "BestLap": 90000, "TotalTime": 0},
        {"DriverName": "Bob", "DriverGuid": "2", "CarModel": "car_b", "BestLap": 999999999, "TotalTime": 0},          # connected, no lap
        {"DriverName": "", "DriverGuid": "", "CarModel": "car_a", "BestLap": 999999999, "TotalTime": 0}] * 1 + [      # empty slot
        {"DriverName": "", "DriverGuid": "", "CarModel": "car_a", "BestLap": 999999999, "TotalTime": 0}]}
    p = tmp_path / "q.json"
    p.write_text(json.dumps(data))
    cls = parse_result_file(p)["classification"]
    assert [e["driver_name"] for e in cls] == ["Alice", "Bob"] and [e["position"] for e in cls] == [1, 2]
    assert cls[1]["best_lap_ms"] is None and cls[1]["gap_ms"] is None


def _lap(guid, name, n, ts, t=40000):
    return {"DriverGuid": guid, "DriverName": name, "CarId": 0, "LapTime": t, "Timestamp": ts, "Cuts": 0}


def _long_race(tmp_path, mark=True, at_offset=-5):
    """A wins on lap 3 (t=300000); B is a lap down (2 laps then 3 at 320000) and goes on lapping until 5 laps, so acServer ranks B first with 5 laps; C stops at 2 laps."""
    laps = [_lap("A", "Ann", i, i * 100_000) for i in (1, 2, 3, 4)] + \
           [_lap("B", "Bob", i, 20_000 + i * 100_000, 41000) for i in (1, 2, 3, 4, 5)] + \
           [_lap("C", "Cat", i, i * 105_000) for i in (1, 2)]
    laps.sort(key=lambda l: l["Timestamp"])
    p = tmp_path / "2026_10_30_5_45_RACE.json"
    p.write_text(json.dumps({"TrackName": "t", "TrackConfig": "", "Type": "RACE", "Laps": laps, "Result": [
        {"DriverName": "Bob", "DriverGuid": "B", "CarModel": "m", "BestLap": 40000, "TotalTime": 200000},
        {"DriverName": "Ann", "DriverGuid": "A", "CarModel": "m", "BestLap": 40000, "TotalTime": 400000},
        {"DriverName": "Cat", "DriverGuid": "C", "CarModel": "m", "BestLap": 40000, "TotalTime": 80000}]}))
    if mark:
        (tmp_path / "1.finish").write_text(json.dumps({"at": p.stat().st_mtime + at_offset, "laps": 3}))
    return p


def test_laps_after_the_chequered_flag_do_not_count_and_the_winner_is_who_finished_first(tmp_path):
    parsed = parse_result_file(_long_race(tmp_path))
    names = [(e["driver_name"], e["position"]) for e in parsed["classification"]]
    assert names == [("Ann", 1), ("Bob", 2), ("Cat", 3)], names
    assert parsed["type"] == "Race"
    assert sum(1 for l in parsed["laps"] if l["driver_name"] == "Ann") == 3 and sum(1 for l in parsed["laps"] if l["driver_name"] == "Bob") == 3, "each car's race ends at its first lap after the flag"
    assert sum(1 for l in parsed["laps"] if l["driver_name"] == "Cat") == 2, "a car that never crossed the line again keeps its laps"
    assert parsed["classification"][0]["total_time_ms"] == 120_000 and parsed["classification"][0]["best_lap_ms"] == 40000


def test_without_a_marker_or_with_one_of_another_race_the_file_is_left_as_acserver_wrote_it(tmp_path):
    assert parse_result_file(_long_race(tmp_path, mark=False))["classification"][0]["driver_name"] == "Bob"
    other = tmp_path / "other"
    other.mkdir()
    p = _long_race(other, at_offset=+5)   # the flag came after this file was written
    assert parse_result_file(p)["classification"][0]["driver_name"] == "Bob"


def test_an_older_race_keeps_its_cut_when_a_later_race_has_its_own_flag(tmp_path):
    p = _long_race(tmp_path)
    old = p.stat().st_mtime
    (tmp_path / "2.finish").write_text(json.dumps({"at": old + 3600, "laps": 99}))   # the next race's flag, written long after this result
    assert parse_result_file(p)["classification"][0]["driver_name"] == "Ann"
