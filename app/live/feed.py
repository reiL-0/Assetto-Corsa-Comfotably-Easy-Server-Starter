"""What the website's lap recorder needs to know about the cars on THIS manager's servers (OPR WP/telemetry_api.py asks for it, with the site's `reporter` token).

    GET /api/v1/telemetry-feed -> {"servers": [{"serverId": 2, "session": {"type": 1, "track": "spa", "trackConfig": "", "serverName": "...", "index": 0},
                                                 "cars": [{"carId": 3, "name": "Ana", "guid": "7656...", "connected": true, "splinePos": 0.41, "carModel": "gt3", "speedKmh": 181.2,
                                                           "laps": 5, "lastLapMs": 103420}]}]}

One entry per running server with its ACSP plugin socket. Only cars that are connected and have a Steam ID; `speedKmh` is the horizontal speed the acServer reports (None until its
first position packet), `lastLapMs` the time acServer gave for the last finished lap (None before the first). `carId` + `name` let the website tell which account sits in a slot of a server (the language of the banners, OPR WP/csp_state.py). Everything here is already on the public live timing.
The website's recorder is what writes laps down; this only reads the in-memory state.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app import supervisor
from app.auth import require_any
from app.live.racecontrol import speed_kmh

router = APIRouter(prefix="/telemetry-feed", tags=["telemetry"], dependencies=[Depends(require_any("reporter", "driver", "steward", "admin"))])


def snapshot() -> dict:
    out = []
    for inst in supervisor.live():
        board, s = inst.acsp.board, inst.acsp.board.session
        cars = []
        for d in board.drivers:
            if not (d.connected and d.guid):
                continue
            v = speed_kmh(inst.acsp.cars.get(d.car_id) or {})
            cars.append({"carId": d.car_id, "name": d.name, "guid": d.guid, "connected": True, "splinePos": d.spline, "carModel": d.model,
                         "speedKmh": None if v is None else round(v, 1), "laps": d.laps, "lastLapMs": d.last_ms or None})
        out.append({"serverId": inst.server_id, "cars": cars,
                    "session": {"type": s.get("session_type", 0), "track": s.get("track", ""), "trackConfig": s.get("track_config", ""),
                                "serverName": s.get("server_name", ""), "index": s.get("session_index", 0)}})
    return {"servers": out}


@router.get("")
def feed() -> dict:
    return snapshot()
