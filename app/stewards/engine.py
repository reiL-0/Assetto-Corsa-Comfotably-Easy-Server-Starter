"""Turns detections into `Incident` rows. Phase 0: shadow only, it records and never punishes or talks to the driver.

The UDP callback (the event loop) must not wait for SQLite: `on_event` / `record_limits` build the `Incident` from in-memory state and hand it to ONE
writer thread through a bounded queue, like `metrics.enqueue`. The writer is also where `Server.stewards` is read, so the mode is always the stored one."""

import logging
import threading
import time
from collections import deque

from sqlmodel import Session

from app.db import engine
from app.models import Incident, Server
from app.stewards.detectors import at_contact, at_fault, detect

log = logging.getLogger("acmanager.stewards")

DEDUP_S = 3.0  # acServer reports one contact from both cars (up to ~2 s apart, seen in a real session) and a wall scrape as a burst: one incident per car/pair per 3 s
LIMITS_MIN_S = 0.5  # a driver's script cannot report more often than this (it is client-side, so it could flood)
MAX_QUEUED = 1000   # incidents waiting for the writer; one more is dropped (the newest) and counted

_cv = threading.Condition()   # guards ALL the shared state below: dedup keys, queue, the incident being written, counters
_recent: dict[tuple, float] = {}  # (server_id, kind, car ids) -> when it was last ADMITTED; released again if the write fails
_buf: deque = deque()         # (key, stamp, Incident) accepted and not yet taken by the writer
_current = None               # the item the writer is writing right now
_writer: threading.Thread | None = None
_accepting = True        # False while the manager shuts down: nothing new is admitted after the last flush()
_stats = {"written": 0, "failed": 0, "dropped": 0, "ignored": 0}   # ignored: the server was not in shadow mode when the writer got to it


def _admit(key: tuple, now: float, window: float, build) -> bool:
    """Dedup, bounded queue and hand-off in ONE step. `build()` makes the Incident (cheap, memory only) and only runs for an admitted event."""
    global _writer
    with _cv:
        if not _accepting or now - _recent.get(key, 0) < window:
            return False
        if len(_buf) >= MAX_QUEUED:
            _stats["dropped"] += 1
            n = _stats["dropped"]
            row = None
        else:
            row = build()
            _recent[key] = now
            if len(_recent) > 200:
                for k in [k for k, v in _recent.items() if now - v >= DEDUP_S]:   # (cheap prune: keeps the dict from growing all night)
                    del _recent[k]
            if _writer is None or not _writer.is_alive():
                _writer = threading.Thread(target=_drain, name="incident-writer", daemon=True)
                _writer.start()
            _buf.append((key, now, row))
            _cv.notify_all()
            return True
    if n in (1, 10, 100) or n % 1000 == 0:
        log.error("incident queue full: %d incident(s) dropped so far", n)
    return False


def _drain() -> None:
    global _current
    while True:
        with _cv:
            _cv.wait_for(lambda: _buf)
            item = _current = _buf.popleft()   # taken and marked in ONE step: flush() never sees it as neither queued nor being written
        key, stamp, row = item
        outcome = "failed"
        try:
            with Session(engine) as s:
                srv = s.get(Server, row.server_id)
                if srv and srv.stewards == "shadow":
                    s.add(row)
                    s.commit()
                    outcome = "written"
                else:
                    outcome = "ignored"
        except Exception:   # a failed write is logged and counted; its dedup key is released (below) so the next report can try again
            log.exception("could not record the %s incident of server %s", row.kind, row.server_id)
        with _cv:
            _stats[outcome] += 1
            if outcome != "written" and _recent.get(key) == stamp:   # not recorded (failed, or the mode was off): the key is free again; a newer admission of it stays
                del _recent[key]
            _current = None
            _cv.notify_all()


def accept(flag: bool) -> None:
    """Open (startup) or close (shutdown, before the last flush) the intake. UDP sockets stay alive until the process ends, so closing it is what makes that flush final."""
    global _accepting
    with _cv:
        _accepting = flag


def flush(timeout: float = 5.0) -> bool:
    """Wait until every admitted incident has been written (tests, shutdown). Blocks the calling thread: not for the event loop."""
    with _cv:
        return _cv.wait_for(lambda: not _buf and _current is None, timeout)


def stats() -> dict:
    with _cv:
        return {"queued": len(_buf) + (1 if _current is not None else 0), **_stats}


def on_event(client, event: dict, now: float | None = None) -> None:
    """Called by ACSPClient._apply with every event; `client` is the ACSPClient (cars, session, board). Cheap unless something was detected, and never waits for the database."""
    d = detect(event)
    if not d:
        return
    now = now or time.time()
    cars = (d["car_id"], d["other_car_id"]) if d["kind"] != "contact" else tuple(sorted((d["car_id"], d["other_car_id"])))
    key = (client.server_id, d["kind"], cars)

    def build() -> Incident:
        me, other = client.cars.get(d["car_id"], {}), client.cars.get(d["other_car_id"], {})
        sess_info = client.session or {}
        fault, evidence = None, {}
        if d["kind"] == "contact":
            moment = at_contact(client.trail.get(d["car_id"], ()), client.trail.get(d["other_car_id"], ()), d["world_pos"])
            if moment:
                pm, po, t = moment
                who, why = at_fault(pm, po)
                fault = (me, other)[who].get("driver_guid") if who is not None else None
                evidence = {"reason": why, "moment_ago_s": round(now - t, 2),
                            "cars": [{"car_id": c, **p} for c, p in ((d["car_id"], pm), (d["other_car_id"], po))]}
            else:
                evidence = {"reason": "sin posiciones de los dos autos en el momento del choque"}
        return Incident(server_id=client.server_id, ts=now, session_type=sess_info.get("session_type"), session_name=sess_info.get("name", ""),
                        session_ms=client.board.elapsed_ms(now), kind=d["kind"], car_id=d["car_id"],
                        driver_guid=me.get("driver_guid"), driver_name=me.get("driver_name", ""),
                        other_guid=other.get("driver_guid"), other_name=other.get("driver_name", ""),
                        speed=d["speed"], value=d["value"], world_pos=d["world_pos"], fault_guid=fault, evidence=evidence)

    _admit(key, now, 0 if d["kind"] == "cuts" else DEDUP_S, build)


def record_limits(client, car_id: int, name: str, report: dict, now: float | None = None) -> bool:
    """A driver's own CSP script (OPR's opr_cuts.lua) says it left the track: `report` = {ms, wheels, speed, lap, spline, pos}. Queued as a `limits`
    incident (the writer drops it if the server is not in shadow mode) when `car_id` is a car on it whose driver name matches `name`. False = not
    queued (unknown car, flood, full queue). Call it from the event loop, like on_event: it reads the client's live state.
    The client can lie, so this is only ever evidence for a steward, never a sanction by itself."""
    now = now or time.time()
    car = client.cars.get(car_id)
    if not car or (car.get("driver_name") or "").strip().lower() != name.strip().lower():
        return False

    def build() -> Incident:
        sess_info = client.session or {}
        return Incident(server_id=client.server_id, ts=now, session_type=sess_info.get("session_type"), session_name=sess_info.get("name", ""),
                        session_ms=client.board.elapsed_ms(now), kind="limits", car_id=car_id, driver_guid=car.get("driver_guid"),
                        driver_name=car.get("driver_name", ""), speed=report["speed"], value=report["ms"], world_pos=report["pos"],
                        evidence={"source": "client", "wheels": report["wheels"], "lap": report["lap"], "spline": report["spline"]})

    return _admit((client.server_id, "limits", (car_id,)), now, LIMITS_MIN_S, build)
