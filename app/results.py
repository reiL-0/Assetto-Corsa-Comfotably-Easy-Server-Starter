"""Parses acServer session result JSON into a normalized classification.

Reference: the stable `results/*.json` format acServer writes itself
(Cars/Result/Laps + session metadata), unchanged since AC 1.x. `Result` is
already in finishing order, so parsing is mostly a rename + gap computation.
"""

from __future__ import annotations

import json
from pathlib import Path


def parse_result_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    session_type = data.get("Type", "")
    is_race = session_type == "Race"

    classification = []
    for i, r in enumerate(data.get("Result", [])):
        classification.append(
            {
                "position": i + 1,
                "driver_name": r.get("DriverName"),
                "driver_guid": r.get("DriverGuid"),
                "car_model": r.get("CarModel"),
                "best_lap_ms": r.get("BestLap"),
                "total_time_ms": r.get("TotalTime"),
            }
        )

    gap_key = "total_time_ms" if is_race else "best_lap_ms"
    leader_value = classification[0][gap_key] if classification else None
    for i, entry in enumerate(classification):
        gv = entry[gap_key]
        entry["gap_ms"] = gv - leader_value if i > 0 and leader_value and gv else None

    laps = [
        {
            "driver_guid": lap.get("DriverGuid"),
            "driver_name": lap.get("DriverName"),
            "car_id": lap.get("CarId"),
            "lap_time_ms": lap.get("LapTime"),
            "cuts": lap.get("Cuts"),
        }
        for lap in data.get("Laps", [])
    ]

    return {
        "type": session_type,
        "track": data.get("TrackName"),
        "track_config": data.get("TrackConfig"),
        "name": data.get("Name"),
        "classification": classification,
        "laps": laps,
    }
