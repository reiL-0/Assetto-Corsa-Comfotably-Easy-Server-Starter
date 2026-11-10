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
    data = {**RACE_JSON, "Type": "Qualify", "Laps": [*RACE_JSON["Laps"], {"DriverGuid": "2", "LapTime": 91000, "Cuts": 0}]}
    p = tmp_path / "q.json"
    p.write_text(json.dumps(data))

    parsed = parse_result_file(p)
    assert parsed["classification"][1]["gap_ms"] == 1000  # 91000 - 90000


def test_qualifying_classification_uses_only_clean_laps_not_acserver_order(tmp_path):
    data = {**RACE_JSON, "Type": "Qualify", "Result": [
        {"DriverName": "Bob", "DriverGuid": "2", "CarModel": "car_b", "BestLap": 88000, "TotalTime": 1},
        {"DriverName": "Alice", "DriverGuid": "1", "CarModel": "car_a", "BestLap": 90000, "TotalTime": 1},
        {"DriverName": "Cat", "DriverGuid": "3", "CarModel": "car_c", "BestLap": 89000, "TotalTime": 1},
    ], "Laps": [
        {"DriverGuid": "2", "LapTime": 88000, "Cuts": 1},
        {"DriverGuid": "2", "LapTime": 92000, "Cuts": 0},
        {"DriverGuid": "1", "LapTime": 90000, "Cuts": 0},
        {"DriverGuid": "3", "LapTime": 89000, "Cuts": 2},
    ]}
    p = tmp_path / "q.json"
    p.write_text(json.dumps(data))
    cls = parse_result_file(p)["classification"]
    assert [(e["driver_guid"], e["best_lap_ms"], e["position"], e["gap_ms"]) for e in cls] == [
        ("1", 90000, 1, None), ("2", 92000, 2, 2000), ("3", None, 3, None)]


def test_mixed_qualifying_cut_data_does_not_count_known_dirty_lap(tmp_path):
    data = {**RACE_JSON, "Type": "Qualify", "Result": [
        {"DriverName": "Bob", "DriverGuid": "2", "CarModel": "car_b", "BestLap": 88000, "TotalTime": 1},
        {"DriverName": "Alice", "DriverGuid": "1", "CarModel": "car_a", "BestLap": 90000, "TotalTime": 1},
    ], "Laps": [
        {"DriverGuid": "2", "LapTime": 88000, "Cuts": 1},
        {"DriverGuid": "1", "LapTime": 90000},
    ]}
    p = tmp_path / "mixed-q.json"
    p.write_text(json.dumps(data))
    cls = parse_result_file(p)["classification"]
    assert [(e["driver_guid"], e["best_lap_ms"]) for e in cls] == [("1", 90000), ("2", None)]


def test_old_qualifying_result_without_cut_data_preserves_original_classification(tmp_path):
    data = {**RACE_JSON, "Type": "Qualify", "Laps": [{"DriverGuid": "1", "LapTime": 90000}, {"DriverGuid": "2", "LapTime": 91000}]}
    p = tmp_path / "q.json"
    p.write_text(json.dumps(data))
    assert [(e["driver_guid"], e["best_lap_ms"]) for e in parse_result_file(p)["classification"]] == [("1", 90000), ("2", 91000)]


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


def test_a_driver_in_two_slots_is_one_row_the_one_that_drove_kept_as_it_is(tmp_path):   # he reconnected: acServer lists the old empty slot and the new one
    def parse(rows):
        p = tmp_path / "race.json"
        p.write_text(json.dumps({**RACE_JSON, "Result": rows}))
        return parse_result_file(p)["classification"]
    empty = {"DriverName": "Alice", "DriverGuid": "1", "CarModel": "car_a", "BestLap": 999999999, "TotalTime": 0}
    bob = {"DriverName": "Bob", "DriverGuid": "2", "CarModel": "car_b", "BestLap": 91000, "TotalTime": 605000}
    alice = {"DriverName": "Alice", "DriverGuid": "1", "CarModel": "car_c", "BestLap": 90000, "TotalTime": 600000}
    for rows in ([empty, bob, alice], [alice, bob, empty], [bob, empty, alice]):   # wherever the empty one is, one row of Alice, the one that drove, untouched
        cls = parse(rows)
        assert [e["position"] for e in cls] == [1, 2] and sorted(e["driver_name"] for e in cls) == ["Alice", "Bob"]
        a = next(e for e in cls if e["driver_name"] == "Alice")
        assert (a["car_model"], a["best_lap_ms"], a["total_time_ms"]) == ("car_c", 90000, 600000)
    assert [e["driver_name"] for e in parse([empty, bob, alice])] == ["Bob", "Alice"], "the empty slot does not give him its place"
    other = {**alice, "CarModel": "car_d", "BestLap": 92000, "TotalTime": 100}   # two rows with a time: the first stays, nothing is added up or mixed
    kept = parse([other, bob, alice])
    assert len(kept) == 2 and (kept[0]["car_model"], kept[0]["best_lap_ms"], kept[0]["total_time_ms"]) == ("car_d", 92000, 100)


def test_finish_crossing_order_ranks_by_line_crossing_not_laptime_sum(tmp_path):
    """Ann finishes 3 laps at t=300000; Bob finishes 3 laps at t=300500 (+500ms behind) but had smaller sum(LapTime) due to rolling start or disconnect."""
    laps = [
        _lap("A", "Ann", 1, 100_000, 40000),
        _lap("A", "Ann", 2, 200_000, 40000),
        _lap("A", "Ann", 3, 300_000, 40000),
        _lap("B", "Bob", 1, 100_100, 35000),  # Bob's LapTime is smaller
        _lap("B", "Bob", 2, 200_200, 35000),
        _lap("B", "Bob", 3, 300_500, 35000),  # but crossed the line 500ms after Ann
        _lap("C", "Cat", 1, 105_000, 42000),
        _lap("C", "Cat", 2, 301_000, 42000),  # Cat takes checkered flag at lap 2
        _lap("D", "Dan", 1, 105_000, 42000),
        _lap("D", "Dan", 2, 210_000, 42000),  # Dan retired earlier at lap 2
    ]
    laps.sort(key=lambda l: l["Timestamp"])
    p = tmp_path / "2026_11_07_6_24_RACE.json"
    p.write_text(json.dumps({
        "TrackName": "t", "TrackConfig": "", "Type": "RACE", "Laps": laps,
        "Result": [
            {"DriverName": "Bob", "DriverGuid": "B", "CarModel": "m", "BestLap": 35000, "TotalTime": 105000},
            {"DriverName": "Ann", "DriverGuid": "A", "CarModel": "m", "BestLap": 40000, "TotalTime": 120000},
            {"DriverName": "Cat", "DriverGuid": "C", "CarModel": "m", "BestLap": 42000, "TotalTime": 84000},
            {"DriverName": "Dan", "DriverGuid": "D", "CarModel": "m", "BestLap": 42000, "TotalTime": 84000},
        ],
    }))
    (tmp_path / "1.finish").write_text(json.dumps({"at": p.stat().st_mtime - 1, "laps": 3}))

    parsed = parse_result_file(p)
    cls = parsed["classification"]
    assert [e["driver_name"] for e in cls] == ["Ann", "Bob", "Cat", "Dan"]
    assert cls[0]["position"] == 1 and cls[0]["gap_ms"] is None and cls[0]["total_time_ms"] == 120000
    assert cls[1]["position"] == 2 and cls[1]["gap_ms"] == 500 and cls[1]["total_time_ms"] == 120500
    assert cls[2]["position"] == 3 and cls[2]["gap_ms"] is None and cls[2]["total_time_ms"] == 0
    assert cls[3]["position"] == 4 and cls[3]["gap_ms"] is None and cls[3]["total_time_ms"] == 0


def _late_joiner_race(tmp_path, guids):
    """Like _long_race plus D, who joined after the flag (A wins on lap 3 at t=300000) and only has laps after it."""
    p = _long_race(tmp_path, mark=False)
    data = json.loads(p.read_text())
    data["Laps"] += [_lap("D", "Dan", i, 310_000 + i * 90_000) for i in (1, 2)]
    data["Laps"].sort(key=lambda l: l["Timestamp"])
    data["Result"].append({"DriverName": "Dan", "DriverGuid": "D", "CarModel": "m", "BestLap": 90000, "TotalTime": 180000})
    p.write_text(json.dumps(data))
    mark = {"at": p.stat().st_mtime, "laps": 3}
    if guids is not None:
        mark["guids"] = guids
    (tmp_path / "1.finish").write_text(json.dumps(mark))
    return p


def test_a_driver_who_joined_after_the_flag_is_not_in_the_race(tmp_path):
    parsed = parse_result_file(_late_joiner_race(tmp_path, ["A", "B", "C"]))
    assert [(e["driver_name"], e["position"]) for e in parsed["classification"]] == [("Ann", 1), ("Bob", 2), ("Cat", 3)]
    assert all(l["driver_name"] != "Dan" for l in parsed["laps"])


def test_a_marker_from_before_the_roster_leaves_nobody_out(tmp_path):   # (fails if the old marker format stops being read as «unknown»)
    names = [e["driver_name"] for e in parse_result_file(_late_joiner_race(tmp_path, None))["classification"]]
    assert "Dan" in names


def test_a_driver_who_retired_before_the_flag_stays_in_the_race(tmp_path):   # (left the server after racing: not connected at the flag, but he did race)
    parsed = parse_result_file(_late_joiner_race(tmp_path, ["A", "B"]))   # C (2 laps before the flag) is not connected any more
    assert [e["driver_name"] for e in parsed["classification"]] == ["Ann", "Bob", "Cat"]
    assert sum(1 for l in parsed["laps"] if l["driver_name"] == "Cat") == 2
