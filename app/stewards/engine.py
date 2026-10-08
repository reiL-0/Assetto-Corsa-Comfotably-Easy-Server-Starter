"""Turns detections into `Incident` rows. Phase 0: shadow only, it records and never punishes or talks to the driver."""

import time

from sqlmodel import Session

from app.db import engine
from app.models import Incident, Server
from app.stewards.detectors import at_fault, detect

DEDUP_S = 1.0  # acServer reports one contact from both cars, and a wall scrape as a burst: one incident per car/pair per second
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
        if d["kind"] == "contact" and "pos" in me and "pos" in other:
            who, why = at_fault(me, other)
            fault = (me, other)[who].get("driver_guid") if who is not None else None
            evidence = {"reason": why, "cars": [{"car_id": c, "pos": x["pos"], "velocity": x["velocity"]} for c, x in ((d["car_id"], me), (d["other_car_id"], other))]}
        s.add(Incident(server_id=client.server_id, ts=now, session_type=sess_info.get("session_type"), session_name=sess_info.get("name", ""),
                       session_ms=client.board.elapsed_ms(now), kind=d["kind"], car_id=d["car_id"],
                       driver_guid=me.get("driver_guid"), driver_name=me.get("driver_name", ""),
                       other_guid=other.get("driver_guid"), other_name=other.get("driver_name", ""),
                       speed=d["speed"], value=d["value"], world_pos=d["world_pos"], fault_guid=fault, evidence=evidence))
        s.commit()
