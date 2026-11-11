"""Parses acServer session result JSON into a normalized classification.

Reference: the stable `results/*.json` format acServer writes itself
(Cars/Result/Laps + session metadata), unchanged since AC 1.x. `Result` is
already in finishing order, so parsing is mostly a rename + gap computation.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app import csp
from app.models import Championship, ChampionshipEvent, Event, LeagueMember, Penalty


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


def _finish_cut(path: Path, laps: list[dict]) -> tuple[int, set[str] | None] | None:
    """The race is long in acServer and the manager decides when it ends, so the cars keep lapping after the chequered flag and acServer would rank them on those laps. -> the session time
    (ms) at which the winner completed the distance: every car's race ends at its first lap completed from then on, and the SteamIDs that were connected at the flag (None = unknown: nobody is left out). None = nothing to cut. The flag a result file belongs to is the last one
    before it was written, unless another race result was written in between (that one took it)."""
    try:
        written = path.stat().st_mtime
        marks = []
        for f in path.parent.glob("*" + FINISH_SUFFIX):
            m = json.loads(f.read_text())
            if float(m["at"]) <= written:
                marks.append((float(m["at"]), int(m["laps"]), m.get("guids")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not marks:
        return None
    at, n, here = max(marks, key=lambda m: m[:2])
    if n < 1 or any(at <= p.stat().st_mtime < written for p in path.parent.glob("*_RACE.json") if p != path):
        return None
    seen, stamps = Counter(), []
    for lap in laps:
        g = lap.get("DriverGuid")
        if g and (lap.get("LapTime") or 0) > 0:   # (the laps apply_penalties counts)
            seen[g] += 1
            if seen[g] == n:
                stamps.append(lap.get("Timestamp") or 0)
    if not stamps:
        return None
    present = {str(g) for g in here} if isinstance(here, list) and here else None   # (a marker from before this field: nobody is left out)
    return min(stamps), present


def _cut_after_finish(data: dict, classification: list[dict], cut: int, present: set[str] | None = None) -> None:
    """Each car's race ends at its first lap completed at or after `cut`: the laps after it are dropped and the classification is recomputed on the rest (laps, then finish crossing order).
    A driver who joined after the flag is not in the race: not connected at the flag AND no lap closed before it (one who left earlier after racing is a retirement and stays) -> his row and laps go."""
    if present is not None:
        raced = {lap.get("DriverGuid") for lap in data.get("Laps", []) if (lap.get("Timestamp") or 0) < cut}
        late = {e["driver_guid"] for e in classification if e["driver_guid"] and e["driver_guid"] not in present and e["driver_guid"] not in raced}
        classification[:] = [e for e in classification if e["driver_guid"] not in late]
        data["Laps"] = [lap for lap in data.get("Laps", []) if lap.get("DriverGuid") not in late]
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
            clean = [lap["LapTime"] for lap in mine if lap.get("LapTime") and not lap.get("Cuts")]
            e["best_lap_ms"] = min(clean) if clean else None
        e["_laps"] = len(mine)
        e["_finish_ts"] = mine[-1].get("Timestamp") if mine else None
        e["_in_done"] = e["driver_guid"] in done
        e["_sum_lap_ms"] = sum(lap.get("LapTime") or 0 for lap in mine)

    # Lead car crosses at or after cut first; cars on the lead lap finish in the order they crossed the line;
    # lapped cars taking the flag finish ahead of DNFs at the same lap count.
    classification.sort(key=lambda e: (
        -e["_laps"],
        0 if e["_in_done"] else 1,
        e["_finish_ts"] if e["_finish_ts"] is not None else float("inf"),
    ))

    winner = classification[0] if classification else None
    winner_laps = winner["_laps"] if winner else 0
    winner_ts = winner["_finish_ts"] if winner else 0
    winner_time = (winner["_sum_lap_ms"] or winner.get("total_time_ms") or 0) if winner else 0

    for i, e in enumerate(classification):
        e["position"] = i + 1
        if winner and e["_laps"] == winner_laps and e["_in_done"] and e["_finish_ts"] is not None and winner_ts is not None:
            e["total_time_ms"] = winner_time + (e["_finish_ts"] - winner_ts)
        else:
            e["total_time_ms"] = 0
        del e["_laps"]
        del e["_finish_ts"]
        del e["_in_done"]
        del e["_sum_lap_ms"]


def parse_result_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    session_type = data.get("Type", "").capitalize()   # (acServer writes RACE / QUALIFY / PRACTICE)
    is_race = session_type == "Race"

    rows = data.get("Result", [])
    # acServer can replace a kicked driver's GUID with a shared placeholder after reconnecting.
    # Drop only the phantom row when the same name has a real Steam ID with recorded laps.
    driven = {lap.get("DriverGuid") for lap in data.get("Laps", []) if (lap.get("LapTime") or 0) > 0}
    rejoined = {(r.get("DriverName"), r.get("CarModel")) for r in rows if r.get("DriverGuid") in driven and str(r.get("DriverGuid", "")).isdigit()}
    classification = []
    for r in rows:
        if r.get("DriverGuid") == "$#@@!$kicked" and (r.get("DriverName"), r.get("CarModel")) in rejoined and not any(
            lap.get("DriverGuid") == "$#@@!$kicked" and lap.get("DriverName") == r.get("DriverName") for lap in data.get("Laps", [])
        ):
            continue
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

    one: dict[str, dict] = {}   # a driver is classified once (everything downstream goes by SteamID): the row that drove is kept as it is, the other one (the empty slot he left) is dropped
    merged = []
    for e in classification:
        first = one.get(e["driver_guid"]) if e["driver_guid"] else None
        if first is None:
            merged.append(e)
            if e["driver_guid"]:
                one[e["driver_guid"]] = e
        elif not first["total_time_ms"] and e["total_time_ms"]:   # the first was the empty slot: this one drove and takes its place in the order
            merged.remove(first)
            merged.append(e)
            one[e["driver_guid"]] = e
    for i, e in enumerate(merged):
        e["position"] = i + 1
    classification = merged

    if session_type == "Qualify" and data.get("Laps") and any("Cuts" in lap for lap in data["Laps"]):
        clean: dict[str, int] = {}
        for lap in data.get("Laps", []):
            guid, ms = lap.get("DriverGuid"), lap.get("LapTime")
            if guid and ms and 0 < ms < NO_LAP_MS and lap.get("Cuts", 0) == 0:
                clean[guid] = min(clean.get(guid, ms), ms)
        for e in classification:
            e["best_lap_ms"] = clean.get(e["driver_guid"])
        classification.sort(key=lambda e: e["best_lap_ms"] if e["best_lap_ms"] is not None else float("inf"))
        for i, e in enumerate(classification):
            e["position"] = i + 1

    cut = _finish_cut(path, data.get("Laps", [])) if is_race else None
    if cut is not None:
        _cut_after_finish(data, classification, *cut)

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
        "track": csp.clean_track(data.get("TrackName")),
        "track_config": data.get("TrackConfig"),
        "name": data.get("Name"),
        "classification": classification,
        "laps": laps,
        "stages": read_stages(path) if is_race else [],
    }


KINDS = ("time", "stopgo", "position", "dsq", "grid", "points")
TIMED = ("time", "stopgo")   # stored in ms and added to the race time (acServer cannot force a stop-and-go, so the stop is charged as time)


def penalties_for(sess: Session, server_id: int, filename: str) -> list[Penalty]:
    return list(sess.exec(select(Penalty).where(Penalty.server_id == server_id, Penalty.filename == filename, Penalty.dismissed == False).order_by(Penalty.id)))


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
        if p.dismissed:
            continue
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


# --- readable names for the result files (the selectors show these; the file name stays the id) -----------------------------------------------------------------
_RESULT_NAME = re.compile(r"(\d{4})_(\d{1,2})_(\d{1,2})_(\d{1,2})_(\d{1,2})_([A-Za-z]+)\.json")   # acServer does not zero-pad: 2026_11_11_5_13_RACE.json
_TYPE_WORD = {"RACE": "Race", "QUALIFY": "Quali", "PRACTICE": "Practice"}
_track_cache: dict[tuple, str | None] = {}   # (path, mtime_ns, size) -> track; a result file never changes once written. ponytail: unbounded, ~100 bytes per file


def result_start(filename: str) -> datetime | None:
    """Aware start of a session from its file name. The clock in the name is the acServer host's own (the VPS runs on Europe/Berlin), so it is read as the host's local time."""
    m = _RESULT_NAME.fullmatch(filename)
    try:
        return datetime(*map(int, m.groups()[:5])).astimezone() if m else None
    except ValueError:
        return None


def result_track(path: Path) -> str | None:
    """«Track» or «Track (layout)» as the file says; None when it cannot be read (a corrupt or empty file must not break the whole list)."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key not in _track_cache:
            d = json.loads(path.read_text(encoding="utf-8-sig"))
            track, cfg = csp.clean_track(d.get("TrackName")), d.get("TrackConfig")
            track, cfg = track if isinstance(track, str) else None, cfg if isinstance(cfg, str) else None   # a hand-edited file may hold anything: only text goes into a label
            track = track.replace("_", " ").strip() if track else track   # «rt_zolder» -> «rt zolder»
            _track_cache[key] = f"{track} ({cfg})" if track and cfg else track or None
        return _track_cache[key]
    except (OSError, ValueError, AttributeError):
        return None


def result_label(filename: str, server_name: str, track: str | None, league: str | None, title: str | None) -> str:
    """«GT3 Fecha 3 · Liga GT · Race», or «Servidor · Pista · Quali» for a loose result: the date is NOT in it, the page adds `started_at` in the browser's own zone.
    A name that is not a dated result file is returned as it is."""
    m, at = _RESULT_NAME.fullmatch(filename), result_start(filename)
    if not m or not at:
        return filename
    kind = _TYPE_WORD.get(m.group(6).upper(), m.group(6).capitalize())
    parts = [title or league or server_name]
    if title and league:
        parts.append(league)
    if not title:
        parts.append(track or "pista desconocida")
    parts.append(kind)
    return " · ".join(parts)


def result_details(sess: Session, server_id: int, server_name: str, folder: Path) -> list[dict]:
    """Every result file of a server, newest first (by the date in the name: sorted as text, `_5_13_` would come after `_10_00_`):
    [{filename, label, type, league, title, started_at}]. `title` = the saved event that produced it, `league` = the league that counts it; both None for a loose result."""
    counted = {e.filename: e for e in sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.server_id == server_id))}
    leagues = {c.id: c.name for c in sess.exec(select(Championship).where(Championship.id.in_({e.championship_id for e in counted.values()})))} if counted else {}
    titles = {e.id: e.title for e in sess.exec(select(Event).where(Event.id.in_({e.event_id for e in counted.values() if e.event_id})))} if any(e.event_id for e in counted.values()) else {}
    out = []
    for p in folder.glob("*.json") if folder.exists() else []:
        e, at = counted.get(p.name), result_start(p.name)
        league, title = (leagues.get(e.championship_id), titles.get(e.event_id)) if e else (None, None)
        m = _RESULT_NAME.fullmatch(p.name)
        out.append({"filename": p.name, "label": result_label(p.name, server_name, None if title else result_track(p), league, title), "type": m.group(6).capitalize() if m else None,
                    "league": league, "title": title, "started_at": at.astimezone(ZoneInfo("UTC")).isoformat() if at else None})
    return sorted(out, key=lambda r: (r["started_at"] or "", r["filename"]), reverse=True)

