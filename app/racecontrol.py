"""Race control: the director's flag, the pace of a full-course yellow and drive-throughs to serve, for the CSP hub script on every client (app/live/racecontrol.py).

  GET    /servers/{id}/race-state               what the hub reads (the website serves it as /api/csp/state): the website's token (role reporter) and everyone above
  POST   /servers/{id}/race-control             {flag: green|yellow|white|checkered, pace_kmh?}: the director (steward)
  POST   /servers/{id}/race-control/penalties   {car_id, laps}: a drive-through to serve, the game enforces it (steward)
  DELETE /servers/{id}/race-control/penalties/{pid}
Each change is also said in the chat, so drivers without the hub see it. The state is in memory and goes with the server or the session.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import supervisor
from app.auth import require, require_any
from app.db import SessionDep
from app.live import acsp
from app.live.racecontrol import LAPS_MAX, PACE_MAX, PACE_MIN, state_of
from app.servers import _get

# the hub's token is the lowest role: it reads this and nothing else of the manager
state_router = APIRouter(prefix="/servers/{server_id}", tags=["race-control"], dependencies=[Depends(require_any("reporter", "driver", "steward", "admin"))])
control_router = APIRouter(prefix="/servers/{server_id}/race-control", tags=["race-control"], dependencies=[Depends(require("steward"))])

SAY = {"green": "BANDERA VERDE: se reanuda la carrera", "yellow": "BANDERA AMARILLA: sin adelantar", "white": "BANDERA BLANCA: última vuelta", "checkered": "BANDERA DE CUADROS"}


class FlagIn(BaseModel):
    flag: Literal["green", "yellow", "white", "checkered"]
    pace_kmh: int | None = Field(default=None, ge=PACE_MIN, le=PACE_MAX)   # a yellow only; without it the hub shows the flag and does not limit anyone


class PenaltyIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    laps: int = Field(default=3, ge=1, le=LAPS_MAX)


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
    return rc.snapshot() if rc else {"rev": 0, "ts": 0, "running": bool(inst), "flag": "green", "pace_kmh": None, "penalties": []}


def _control(server_id: int, sess: SessionDep):
    _get(sess, server_id)
    inst = _live(server_id)
    if not inst:
        raise HTTPException(409, "server not running or ACSP plugin not connected")
    return inst, state_of(server_id, True, inst.acsp.session, create=True)


@control_router.post("")
def set_flag(server_id: int, body: FlagIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    rc.set_flag(body.flag, body.pace_kmh)
    _chat(inst, SAY[body.flag] + (f" · ritmo {body.pace_kmh} km/h" if body.flag == "yellow" and body.pace_kmh else ""))
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
