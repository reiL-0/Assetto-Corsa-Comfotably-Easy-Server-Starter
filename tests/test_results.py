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
