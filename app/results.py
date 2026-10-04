"""Parses acServer session result JSON into a normalized classification.

Reference: the stable `results/*.json` format acServer writes itself
(Cars/Result/Laps + session metadata), unchanged since AC 1.x. `Result` is
already in finishing order, so parsing is mostly a rename + gap computation.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlmodel import Session, select

from app.models import Penalty


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


KINDS = ("time", "position", "dsq", "grid", "points")


def penalties_for(sess: Session, server_id: int, filename: str) -> list[Penalty]:
    return list(sess.exec(select(Penalty).where(Penalty.server_id == server_id, Penalty.filename == filename).order_by(Penalty.id)))


def _gaps(cls: list[dict], race: bool) -> None:
    lead = next((e for e in cls if e["position"] == 1), None)
    for e in cls:
        if e["position"] is None or lead is None or e is lead:
            e["gap_ms"] = None
        elif race:
            e["gap_ms"] = e["adjusted_total_ms"] - lead["adjusted_total_ms"] if e["laps"] == lead["laps"] else None
        else:
            e["gap_ms"] = e["best_lap_ms"] - lead["best_lap_ms"] if e["best_lap_ms"] and lead["best_lap_ms"] else None


def apply_penalties(parsed: dict, penalties: list[Penalty]) -> dict:
    """The classification with the stewards' decisions applied; the original stays visible (`original_position`).

    time -> added to the race time and the order is recomputed (laps, then time); position -> the driver drops N
    places; dsq -> removed from the classification (position None, listed last); points -> not a classification
    change, carried in `points_penalty` for the championship; grid -> only changes `grid`, the order for the next race.
    ponytail: with time penalties the re-sort is laps desc, then total time; a driver who did not finish and whose
    TotalTime is 0 sorts first among equals. Good enough while leagues review such cases by hand."""
    race = parsed["type"] == "Race"
    laps = Counter(lap["driver_guid"] for lap in parsed["laps"] if (lap.get("lap_time_ms") or 0) > 0)
    by_guid: dict[str, list[Penalty]] = defaultdict(list)
    for p in penalties:
        by_guid[p.driver_guid].append(p)

    cls = [dict(e) for e in parsed["classification"]]
    for e in cls:
        ps = by_guid.get(e["driver_guid"], [])
        e.update(
            original_position=e["position"], laps=laps.get(e["driver_guid"], 0),
            time_penalty_ms=sum(p.value for p in ps if p.kind == "time"),
            position_penalty=sum(p.value for p in ps if p.kind == "position"),
            grid_penalty=sum(p.value for p in ps if p.kind == "grid"),
            points_penalty=sum(p.value for p in ps if p.kind == "points"),
            disqualified=any(p.kind == "dsq" for p in ps),
            penalties=[{"id": p.id, "kind": p.kind, "value": p.value, "reason": p.reason} for p in ps],
        )
        e["adjusted_total_ms"] = (e["total_time_ms"] or 0) + e["time_penalty_ms"]

    ok = [e for e in cls if not e["disqualified"]]
    out = [e for e in cls if e["disqualified"]]
    if race and any(e["time_penalty_ms"] for e in ok):
        ok.sort(key=lambda e: (-e["laps"], e["adjusted_total_ms"]))
    for e in sorted((e for e in ok if e["position_penalty"]), key=lambda e: e["original_position"]):
        i = ok.index(e)
        ok.remove(e)
        ok.insert(min(i + e["position_penalty"], len(ok)), e)
    for i, e in enumerate(ok):
        e["position"] = i + 1
    for e in out:
        e["position"] = None
    final = ok + out
    _gaps(final, race)

    grid = list(ok)
    for e in sorted((e for e in ok if e["grid_penalty"]), key=lambda e: e["position"]):
        i = grid.index(e)
        grid.remove(e)
        grid.insert(min(i + e["grid_penalty"], len(grid)), e)
    return {**parsed, "classification": final, "grid": [e["driver_guid"] for e in grid + out if e["driver_guid"]]}
