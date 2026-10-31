"""Parses acServer session result JSON into a normalized classification.

Reference: the stable `results/*.json` format acServer writes itself
(Cars/Result/Laps + session metadata), unchanged since AC 1.x. `Result` is
already in finishing order, so parsing is mostly a rename + gap computation.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from sqlmodel import Session, select

from app.models import ChampionshipEvent, LeagueMember, Penalty


NO_LAP_MS = 999_999_999   # the BestLap acServer writes for a car that set no lap
STAGES_PENDING = "stages.pending"   # the same, while the race is on (the manager moves it to <result>.stages when acServer says the session ended)
STAGES_SUFFIX = ".stages"   # <result file name without .json>.stages: the order at the end of every stage of a NASCAR race, {"stages": [{"stage": n, "order": [steam ids]}]} (app/racecontrol.py)
FINISH_SUFFIX = ".finish"   # <unix time>.finish, written by the manager at the chequered flag of a NASCAR race (app/racecontrol.py): {"at": unix time, "laps": laps of the winner}


def _pending_at(d: Path) -> float | None:
    try:
        return float(json.loads((d / STAGES_PENDING).read_text())["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pending_is_unclaimed(d: Path, name: str) -> bool:
    """The journal in `d` can be the one of the race result `name`: it was written before that result (if it exists yet) and no other race result was written after it."""
    at = _pending_at(d)
    if at is None or any(p.stat().st_mtime >= at for p in d.glob("*_RACE.json") if p.name != name):
        return False
    mine = d / name
    return not mine.is_file() or at <= mine.stat().st_mtime


def claim_orphan(d: Path) -> None:
    """A journal whose race never got its end-of-session message (the manager was not told): before the next race overwrites it, it goes to the first race result written after it."""
    at = _pending_at(d)
    if at is None:
        return
    for _, p in sorted((p.stat().st_mtime, p) for p in d.glob("*_RACE.json") if p.stat().st_mtime >= at):
        if not p.with_suffix(STAGES_SUFFIX).exists():
            os.replace(d / STAGES_PENDING, p.with_suffix(STAGES_SUFFIX))
        return


def read_stages(path: Path) -> list[dict]:
    """The order of the field at the end of each stage of this race result ([] = none recorded): [{"stage": n, "order": [steam id, ...]}], first of the order first."""
    try:
        mine = path.with_suffix(STAGES_SUFFIX)
        data = json.loads((mine if mine.is_file() else path.parent / STAGES_PENDING).read_text())   # (read once: what is checked is what is used)
        if not mine.is_file():   # acServer's end-of-session message did not reach us: the journal of the race is the one written before this result and after the previous race result
            written, at = path.stat().st_mtime, float(data["at"])
            if at > written or any(at <= p.stat().st_mtime < written for p in path.parent.glob("*_RACE.json") if p != path):
                return []
        return [{"stage": int(s["stage"]), "order": [str(g) for g in s["order"]]} for s in data["stages"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _finish_cut(path: Path, laps: list[dict]) -> int | None:
    """The race is long in acServer and the manager decides when it ends, so the cars keep lapping after the chequered flag and acServer would rank them on those laps. -> the session time
    (ms) at which the winner completed the distance: every car's race ends at its first lap completed from then on. None = nothing to cut. The flag a result file belongs to is the last one
    before it was written, unless another race result was written in between (that one took it)."""
    try:
        written = path.stat().st_mtime
        marks = []
        for f in path.parent.glob("*" + FINISH_SUFFIX):
            m = json.loads(f.read_text())
            if float(m["at"]) <= written:
                marks.append((float(m["at"]), int(m["laps"])))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not marks:
        return None
    at, n = max(marks)
    if n < 1 or any(at <= p.stat().st_mtime < written for p in path.parent.glob("*_RACE.json") if p != path):
        return None
    seen, stamps = Counter(), []
    for lap in laps:
        g = lap.get("DriverGuid")
        if g and (lap.get("LapTime") or 0) > 0:   # (the laps apply_penalties counts)
            seen[g] += 1
            if seen[g] == n:
                stamps.append(lap.get("Timestamp") or 0)
    return min(stamps) if stamps else None


def _cut_after_finish(data: dict, classification: list[dict], cut: int) -> None:
    """Each car's race ends at its first lap completed at or after `cut`: the laps after it are dropped and the classification is recomputed on the rest (laps, then time)."""
    kept: dict[str, list[dict]] = defaultdict(list)
    done: set[str] = set()
    for lap in data.get("Laps", []):
        g = lap.get("DriverGuid")
        if not g:
            kept[""].append(lap)   # (no GUID: nobody to follow, left as written)
        elif g not in done:
            kept[g].append(lap)
            if (lap.get("Timestamp") or 0) >= cut:
                done.add(g)
    keep = {id(lap) for laps in kept.values() for lap in laps}
    data["Laps"] = [lap for lap in data.get("Laps", []) if id(lap) in keep]
    for e in classification:
        mine = kept.get(e["driver_guid"], [])
        if e["driver_guid"] in done:   # (a car that never crossed the line again keeps what acServer wrote)
            e["total_time_ms"] = sum(lap.get("LapTime") or 0 for lap in mine)
            clean = [lap["LapTime"] for lap in mine if lap.get("LapTime") and not lap.get("Cuts")]
            e["best_lap_ms"] = min(clean) if clean else None
        e["_laps"] = len(mine)
    classification.sort(key=lambda e: (-e["_laps"], e["total_time_ms"] or float("inf")))
    for i, e in enumerate(classification):
        e["position"] = i + 1
        del e["_laps"]


def parse_result_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    session_type = data.get("Type", "").capitalize()   # (acServer writes RACE / QUALIFY / PRACTICE)
    is_race = session_type == "Race"

    classification = []
    for r in data.get("Result", []):
        if not r.get("DriverName") and not r.get("DriverGuid"):
            continue  # acServer lists every empty slot too (BestLap 999999999): nobody drove it
        best = r.get("BestLap")
        classification.append(
            {
                "position": len(classification) + 1,
                "driver_name": r.get("DriverName"),
                "driver_guid": r.get("DriverGuid"),
                "car_model": r.get("CarModel"),
                "best_lap_ms": best if best and best < NO_LAP_MS else None,  # no lap set: acServer writes 999999999
                "total_time_ms": r.get("TotalTime"),
            }
        )

    one: dict[str, dict] = {}   # a driver is classified once (everything downstream goes by SteamID): a second slot of his is folded into the first
    merged = []
    for e in classification:
        first = one.get(e["driver_guid"]) if e["driver_guid"] else None
        if first is None:
            merged.append(e)
            if e["driver_guid"]:
                one[e["driver_guid"]] = e
            continue
        e["best_lap_ms"] = min((x for x in (first["best_lap_ms"], e["best_lap_ms"]) if x), default=None)
        if not first["total_time_ms"]:   # the first slot was an empty one he left: the row that drove takes its place in the order
            merged.remove(first)
            merged.append(e)
            one[e["driver_guid"]] = e
        else:   # he drove in both: the times add up, as when the finish cut recomputes them from his laps
            first["best_lap_ms"] = e["best_lap_ms"]
            first["total_time_ms"] += e["total_time_ms"] or 0
            if (e["total_time_ms"] or 0) > first["total_time_ms"] / 2:
                first["car_model"] = e["car_model"]
    for i, e in enumerate(merged):
        e["position"] = i + 1
    classification = merged

    cut = _finish_cut(path, data.get("Laps", [])) if is_race else None
    if cut is not None:
        _cut_after_finish(data, classification, cut)

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
        "stages": read_stages(path) if is_race else [],
    }


KINDS = ("time", "stopgo", "position", "dsq", "grid", "points")
TIMED = ("time", "stopgo")   # stored in ms and added to the race time (acServer cannot force a stop-and-go, so the stop is charged as time)


def penalties_for(sess: Session, server_id: int, filename: str) -> list[Penalty]:
    return list(sess.exec(select(Penalty).where(Penalty.server_id == server_id, Penalty.filename == filename).order_by(Penalty.id)))


def non_racing_for(sess: Session, server_id: int, filename: str) -> frozenset[str]:
    """Steam IDs of the cars that do not race (safety car, race director, caster) in the league this result belongs to."""
    row = sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.server_id == server_id, ChampionshipEvent.filename == filename)).first()
    if not row:
        return frozenset()
    return frozenset(sess.exec(select(LeagueMember.guid).where(LeagueMember.championship_id == row.championship_id, LeagueMember.non_racing)).all())


def _gaps(cls: list[dict], race: bool) -> None:
    lead = next((e for e in cls if e["position"] == 1), None)
    for e in cls:
        if e["position"] is None or lead is None or e is lead:
            e["gap_ms"] = None
        elif race:
            e["gap_ms"] = e["adjusted_total_ms"] - lead["adjusted_total_ms"] if e["laps"] == lead["laps"] else None
        else:
            e["gap_ms"] = e["best_lap_ms"] - lead["best_lap_ms"] if e["best_lap_ms"] and lead["best_lap_ms"] else None


def apply_penalties(parsed: dict, penalties: list[Penalty], non_racing: frozenset[str] = frozenset()) -> dict:
    """The classification with the stewards' decisions applied; the original stays visible (`original_position`).

    time -> added to the race time and the order is recomputed (laps, then time); position -> the driver drops N
    places; dsq -> removed from the classification (position None, listed last); points -> not a classification
    change, carried in `points_penalty` for the championship; grid -> only changes `grid`, the order for the next race.
    With time penalties the re-sort is laps desc, then total time; a driver with no finish time (TotalTime 0) goes behind everybody who has one at the same laps."""
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
            time_penalty_ms=sum(p.value for p in ps if p.kind in TIMED),
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
        ok.sort(key=lambda e: (-e["laps"], e["adjusted_total_ms"] if e["total_time_ms"] else float("inf")))   # no finish time = behind everybody who has one (the sort is stable: acServer's order among them stays)
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

    grid = [e for e in ok if e["driver_guid"] not in non_racing]   # cars that do not race take no place on the grid, so they do not count for the places lost
    for e in sorted((e for e in grid if e["grid_penalty"]), key=lambda e: e["position"]):
        i = grid.index(e)
        grid.remove(e)
        grid.insert(min(i + e["grid_penalty"], len(grid)), e)
    return {**parsed, "classification": final, "grid": [e["driver_guid"] for e in grid + [x for x in out if x["driver_guid"] not in non_racing] if e["driver_guid"]]}
