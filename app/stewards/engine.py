"""Turns detections into `Incident` rows. Phase 0: shadow only, it records and never punishes or talks to the driver."""

import time

from sqlmodel import Session

from app.db import engine
from app.models import Incident, Server
from app.stewards.detectors import at_contact, at_fault, detect

DEDUP_S = 3.0  # acServer reports one contact from both cars (up to ~2 s apart, seen in a real session) and a wall scrape as a burst: one incident per car/pair per 3 s
LIMITS_MIN_S = 0.5  # a driver's script cannot report more often than this (it is client-side, so it could flood)
_recent: dict[tuple, float] = {}  # (server_id, kind, car ids) -> when it was last recorded


def on_event(client, event: dict, now: float | None = None) -> None:
    """Called by ACSPClient._apply with every event; `client` is the ACSPClient (cars, session, board). Cheap unless something was detected."""
    d = detect(event)
    if not d:
        return
    now = now or time.time()
    cars = (d["car_id"], d["other_car_id"]) if d["kind"] != "contact" else tuple(sorted((d["car_id"], d["other_car_id"])))
    key = (client.server_id, d["kind"], cars)
    if d["kind"] != "cuts" and now - _recent.get(key, 0) < DEDUP_S:
        return
    with Session(engine) as s:
        srv = s.get(Server, client.server_id)
        if not srv or srv.stewards != "shadow":
            return
        _recent[key] = now
        if len(_recent) > 200:
            for k in [k for k, v in _recent.items() if now - v >= DEDUP_S]:   # (cheap prune: keeps the dict from growing all night)
                del _recent[k]
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
        s.add(Incident(server_id=client.server_id, ts=now, session_type=sess_info.get("session_type"), session_name=sess_info.get("name", ""),
                       session_ms=client.board.elapsed_ms(now), kind=d["kind"], car_id=d["car_id"],
                       driver_guid=me.get("driver_guid"), driver_name=me.get("driver_name", ""),
                       other_guid=other.get("driver_guid"), other_name=other.get("driver_name", ""),
                       speed=d["speed"], value=d["value"], world_pos=d["world_pos"], fault_guid=fault, evidence=evidence))
        s.commit()


def record_limits(client, car_id: int, name: str, report: dict, now: float | None = None) -> bool:
    """A driver's own CSP script (OPR's opr_cuts.lua) says it left the track: `report` = {ms, wheels, speed, lap, spline, pos}. Stored as a `limits`
    incident when the server is in shadow mode and `car_id` is a car on it whose driver name matches `name`. False = ignored (mode off, unknown car, flood).
    The client can lie, so this is only ever evidence for a steward, never a sanction by itself."""
    now = now or time.time()
    car = client.cars.get(car_id)
    if not car or (car.get("driver_name") or "").strip().lower() != name.strip().lower():
        return False
    key = (client.server_id, "limits", (car_id,))
    if now - _recent.get(key, 0) < LIMITS_MIN_S:
        return False
    with Session(engine) as s:
        srv = s.get(Server, client.server_id)
        if not srv or srv.stewards != "shadow":
            return False
        _recent[key] = now
        sess_info = client.session or {}
        s.add(Incident(server_id=client.server_id, ts=now, session_type=sess_info.get("session_type"), session_name=sess_info.get("name", ""),
                       session_ms=client.board.elapsed_ms(now), kind="limits", car_id=car_id, driver_guid=car.get("driver_guid"),
                       driver_name=car.get("driver_name", ""), speed=report["speed"], value=report["ms"], world_pos=report["pos"],
                       evidence={"source": "client", "wheels": report["wheels"], "lap": report["lap"], "spline": report["spline"]}))
        s.commit()
    return True
