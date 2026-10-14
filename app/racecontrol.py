"""Race control: the director's flag, the virtual safety car (VSC) and drive-throughs to serve, for the CSP hub script on every client (app/live/racecontrol.py).

  GET    /servers/{id}/race-state               what the hub reads (the website serves it as /api/csp/state): the website's token (role reporter) and everyone above
  POST   /servers/{id}/race-control/pit         {car_id, name, pit}: the hub of a car says whether it is in the pit lane (a VSC exemption; reporter and above)
  POST   /servers/{id}/race-control             {flag: green|yellow|vsc|white|checkered, ...}: the director (steward). A yellow is only a flag; a `vsc` needs pace_kmh and may carry
                                                grace_s (10), tolerance_kmh (5) and penalty {kind: none|pits, laps}
  POST   /servers/{id}/race-control/penalties   {car_id, laps}: a drive-through to serve, the game enforces it (steward)
  DELETE /servers/{id}/race-control/penalties/{pid}
Each change is also said in the chat, so drivers without the hub see it. The state is in memory and goes with the server or the session. `monitor_forever` (started by the app's
lifespan) watches a VSC: it samples the speeds and judges the drivers when the grace ends.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import supervisor
from app.auth import require, require_any
from app.db import SessionDep
from app.live import acsp
from app.live.racecontrol import GRACE_DEFAULT, GRACE_MAX, GRACE_MIN, LAPS_MAX, PACE_MAX, PACE_MIN, TOL_DEFAULT, TOL_MAX, state_of, tick
from app.servers import _get

log = logging.getLogger("acmanager.racecontrol")
# the hub's token is the lowest role: it reads the state and reports its pit-lane status, nothing else of the manager
state_router = APIRouter(prefix="/servers/{server_id}", tags=["race-control"], dependencies=[Depends(require_any("reporter", "driver", "steward", "admin"))])
control_router = APIRouter(prefix="/servers/{server_id}/race-control", tags=["race-control"], dependencies=[Depends(require("steward"))])

SAY = {"green": "BANDERA VERDE: se reanuda la carrera", "yellow": "BANDERA AMARILLA: sin adelantar", "white": "BANDERA BLANCA: última vuelta", "checkered": "BANDERA DE CUADROS"}
MONITOR_EVERY = 0.25   # s; ACSP positions arrive about every 0.2 s


class PenaltyCfg(BaseModel):
    kind: Literal["none", "pits"] = "none"
    laps: int = Field(default=3, ge=1, le=LAPS_MAX)


class FlagIn(BaseModel):
    flag: Literal["green", "yellow", "vsc", "white", "checkered"]
    pace_kmh: int | None = Field(default=None, ge=PACE_MIN, le=PACE_MAX)   # a VSC only (a yellow is only a flag)
    grace_s: int = Field(default=GRACE_DEFAULT, ge=GRACE_MIN, le=GRACE_MAX)   # a VSC: seconds every driver gets to slow down by themselves
    tolerance_kmh: int = Field(default=TOL_DEFAULT, ge=0, le=TOL_MAX)         # a VSC: km/h over the pace that are forgiven when the grace ends
    penalty: PenaltyCfg = PenaltyCfg()                                        # a VSC: what whoever is still over gets


class PenaltyIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    laps: int = Field(default=3, ge=1, le=LAPS_MAX)


class PitIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    name: str = Field(max_length=80)
    pit: bool


def _live(server_id: int):
    inst = supervisor.get(server_id)
    return inst if inst and inst.running and inst.acsp else None


def _chat(inst, text: str) -> None:
    inst.acsp.send(acsp.encode_broadcast_chat(text))


@state_router.get("/race-state")
def race_state(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    inst = _live(server_id)
    rc = state_of(server_id, bool(inst), inst.acsp.session if inst else None)
    return rc.snapshot() if rc else {"rev": 0, "ts": 0, "running": bool(inst), "flag": "green", "pace_kmh": None, "penalties": [], "vsc_id": 0, "grace_left": 0,
                                     "tolerance_kmh": None, "vsc_penalty": None, "violations": [], "exempt": []}


@state_router.post("/race-control/pit", status_code=204)
def report_pit(server_id: int, body: PitIn, sess: SessionDep) -> None:
    """The hub of a car says whether it is in the pit lane (only used as a VSC exemption). 409 when that car is not that driver, like the track-limit reports."""
    _get(sess, server_id)
    inst = _live(server_id)
    car = inst.acsp.cars.get(body.car_id) if inst else None
    if not car or (car.get("driver_name") or "").strip().lower() != body.name.strip().lower():
        raise HTTPException(409, "that car is not that driver")
    rc = state_of(server_id, True, inst.acsp.session)
    if rc:
        rc.report_pit(body.car_id, body.pit)


def _control(server_id: int, sess: SessionDep):
    _get(sess, server_id)
    inst = _live(server_id)
    if not inst:
        raise HTTPException(409, "server not running or ACSP plugin not connected")
    return inst, state_of(server_id, True, inst.acsp.session, create=True)


@control_router.post("")
def set_flag(server_id: int, body: FlagIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    try:
        rc.set_flag(body.flag, body.pace_kmh, body.grace_s, body.tolerance_kmh, body.penalty.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.flag == "vsc":
        pen = f" · si sigues por encima: drive-through de {body.penalty.laps} vueltas" if body.penalty.kind == "pits" else ""
        _chat(inst, f"VIRTUAL SAFETY CAR: baja a {body.pace_kmh} km/h en {body.grace_s} s{pen}")
    else:
        _chat(inst, SAY[body.flag])
    return rc.snapshot()


@control_router.post("/penalties", status_code=201)
def add_penalty(server_id: int, body: PenaltyIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    car = inst.acsp.cars.get(body.car_id)
    if not car:
        raise HTTPException(409, "that car is not on the server")
    p = rc.add_penalty(body.car_id, body.laps)
    _chat(inst, f"DRIVE-THROUGH para {car.get('driver_name') or '#' + str(body.car_id)}: pasa por boxes en {body.laps} vueltas")
    return p


@control_router.delete("/penalties/{penalty_id}", status_code=204)
def clear_penalty(server_id: int, penalty_id: int, sess: SessionDep) -> None:
    _, rc = _control(server_id, sess)
    if not rc.clear_penalty(penalty_id):
        raise HTTPException(404, "penalty not found")


def announce(server_id: int, violations: list[dict]) -> None:
    """The chat line of a judged VSC (names, not car numbers)."""
    inst = _live(server_id)
    if not inst:
        return
    if not violations:
        _chat(inst, "VSC: todos al ritmo")
        return
    names = ", ".join((inst.acsp.cars.get(v["car_id"], {}).get("driver_name") or "#" + str(v["car_id"])) + f" ({v['speed_kmh']} km/h)" for v in violations)
    _chat(inst, "VSC: no bajaron a tiempo: " + names)


async def monitor_forever() -> None:
    """Judges every VSC when its grace ends (one loop for all servers, started by the app's lifespan)."""
    while True:
        try:
            now = time.time()
            live = [(i.server_id, i.acsp.cars) for i in supervisor.live()]
            for server_id, violations in tick(now, live):
                announce(server_id, violations)
        except Exception:   # a bug here must not stop the loop: the next tick tries again
            log.exception("race control monitor")
        await asyncio.sleep(MONITOR_EVERY)
