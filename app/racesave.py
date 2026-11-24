"""A race that finished but acServer has not written yet: the manager writes its result before anything throws the session away.

acServer saves `results/*.json` only when a session passes to the NEXT one (`Saving session json file` right before `NextSession`), never on RESTART_SESSION or when
the server stops: a restart pressed after the chequered flag lost a whole race (server 3, 2026-11-22). The ACSP client keeps every lap of the session as it arrives
(`ACSPClient.session_laps`, with acServer's own `has_completed` of that car at that crossing), so the manager can write the same file acServer would have (acServer itself writes nothing on SIGTERM or RESTART_SESSION: measured on the simulator, so a copy never duplicates a later file of its own):
same name pattern (`AAAA_M_D_H_M_RACE.json`, host local time, no zero padding), same fields `app/results.py` reads (Type, TrackName, Result, Laps, Cars), so the results list,
the league and the web take it without a change. Each driver's race ends at HIS first lap with `has_completed` (acServer decides that, timed or by laps); later laps are dropped.

Callers: `supervisor.Instance.stop` (every stop: manual, idle, end of the event, apply with restart) and POST /servers/{id}/restart_session. `next_session` needs nothing: acServer
writes the file itself. NASCAR keeps its own mechanism (`.finish` markers + `racecontrol.stop_after_save`): its races are cut by the manager, not by acServer's has_completed."""
import errno
import json
import logging
import os
import threading
import time
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

RACE_TYPE = 3
NO_LAP_MS = 999_999_999   # the BestLap acServer writes for a car that set no lap
LOCK_WAIT_S = 10   # s a save waits for another save of the same server (a slow disk): then it fails like a disk error instead of hanging a restart for good
_locks: dict[int, threading.Lock] = {}   # one check-and-write at a time PER SERVER: a restart request, a stop and an idle stop can meet, and a slow disk of one server holds no other


def results_dir(server_id: int) -> Path:
    return Path(settings.data_dir) / "instances" / str(server_id) / "results"


def snapshot(client) -> dict:
    """What the client knows NOW, copied once: the UDP thread replaces `session` / `session_laps` when a new session begins, and a check and a write that read them apart could mix two
    sessions. `session_laps` is a NEW list per session, so if it is not the same list after the copy a session began meanwhile: copy again."""
    for _ in range(3):
        ref = client.session_laps
        snap = {"session": dict(client.session or {}), "laps": list(ref), "drivers": list(client.board.drivers), "t0": client.session_t0}
        if client.session_laps is ref:
            break
    return snap


def unsaved(snap: dict, d: Path) -> bool:
    """A car completed the race (acServer says so) and no race file was written since the first one did: acServer writes its file AFTER the finish (never before it), so an older file is another race's."""
    done = [lap["at"] for lap in snap["laps"] if lap["completed"]]
    if snap["session"].get("session_type") != RACE_TYPE or not done:
        return False
    try:
        return not any(p.stat().st_mtime >= min(done) for p in d.glob("*_RACE.json"))
    except OSError:
        return True


def document(snap: dict) -> dict:
    """The file acServer would have written, from what the client saw. Result is in finishing order: laps, then total time."""
    s, t0 = snap["session"], snap["t0"]
    mine: dict[str, list[dict]] = {}
    for lap in sorted(snap["laps"], key=lambda x: x["at"]):
        mine.setdefault(lap["guid"] or lap["name"], []).append(lap)
    rows, laps_out = [], []
    for key, laps in mine.items():
        end = next((i for i, lap in enumerate(laps) if lap["completed"]), len(laps) - 1)   # his race ends at his first completed lap: what he drove after it is not the race
        kept = laps[: end + 1]
        good = [lap["ms"] for lap in kept if 0 < lap["ms"] < NO_LAP_MS]
        clean = [lap["ms"] for lap in kept if 0 < lap["ms"] < NO_LAP_MS and not lap["cuts"]]
        last = kept[-1]
        rows.append({"DriverName": last["name"], "DriverGuid": last["guid"], "CarId": last["car_id"], "CarModel": last["model"], "BestLap": min(clean or good, default=NO_LAP_MS),
                     "TotalTime": sum(good), "BallastKG": 0, "Restrictor": 0, "_laps": len(good)})
        for lap in kept:
            laps_out.append({"DriverName": lap["name"], "DriverGuid": lap["guid"], "CarId": lap["car_id"], "CarModel": lap["model"], "LapTime": lap["ms"],
                             "Timestamp": int((lap["at"] - t0) * 1000), "Cuts": lap["cuts"], "Sectors": []})
    rows.sort(key=lambda r: (-r["_laps"], r["TotalTime"]))
    for d in snap["drivers"]:   # connected and never crossed the line: acServer lists them too, with no time
        if d.connected and d.guid and d.guid not in mine:
            rows.append({"DriverName": d.name, "DriverGuid": d.guid, "CarId": d.car_id, "CarModel": d.model, "BestLap": NO_LAP_MS, "TotalTime": 0, "BallastKG": 0, "Restrictor": 0, "_laps": 0})
    for r in rows:
        del r["_laps"]
    laps_out.sort(key=lambda x: x["Timestamp"])
    return {"TrackName": s.get("track") or "", "TrackConfig": s.get("track_config") or "", "Type": "RACE", "Name": s.get("name") or "Race",
            "DurationSecs": int(s.get("time_min") or 0) * 60, "RaceLaps": int(s.get("laps") or 0),
            "Cars": [{"CarId": r["CarId"], "Driver": {"Name": r["DriverName"], "Guid": r["DriverGuid"], "Team": "", "Nation": ""}, "Model": r["CarModel"], "Skin": "", "BallastKG": 0, "Restrictor": 0} for r in rows],
            "Result": rows, "Laps": laps_out, "Events": [], "Source": "acmanager-racesave"}


def save(server_id: int, client, now: float | None = None) -> Path | None:
    """Writes the result of a finished, unsaved race. None = nothing to save (not a race, nobody finished, or a file is already there). OSError = it could not be written."""
    d = results_dir(server_id)
    lock = _locks.setdefault(server_id, threading.Lock())
    if not lock.acquire(timeout=LOCK_WAIT_S):
        raise OSError(errno.EBUSY, "another save of this server is still writing")
    try:
        snap = snapshot(client)
        if not unsaved(snap, d):
            return None
        now = now if now is not None else time.time()
        d.mkdir(parents=True, exist_ok=True)
        while True:   # (a file with that name already there is never overwritten: the next minute)
            lt = time.localtime(now)
            path = d / f"{lt.tm_year}_{lt.tm_mon}_{lt.tm_mday}_{lt.tm_hour}_{lt.tm_min}_RACE.json"
            if not path.exists():
                break
            now += 60
        tmp = d / (path.name + ".tmp")
        tmp.write_text(json.dumps(document(snap), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)   # (atomic: the results list never reads half a file)
        log.warning("server %s: the race had finished and acServer had not saved it; the manager wrote %s", server_id, path.name)
        return path
    finally:
        lock.release()
