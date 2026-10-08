"""Incident list, the on/off switch of the automatic stewards and the intake of track-limit reports from the drivers' own game (steward reads,
admin writes: the router guard in api/v1)."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from app import supervisor
from app.db import SessionDep
from app.models import Incident
from app.servers import ServerOut, _get, _out
from app.stewards import engine

router = APIRouter(prefix="/servers/{server_id}", tags=["stewards"])


class StewardsIn(BaseModel):
    mode: Literal["off", "shadow"]


@router.get("/incidents", response_model=list[Incident])
def list_incidents(server_id: int, sess: SessionDep, kind: str | None = None, limit: int = 100) -> list[Incident]:
    q = select(Incident).where(Incident.server_id == server_id).order_by(Incident.ts.desc()).limit(min(limit, 500))
    if kind:
        q = q.where(Incident.kind == kind)
    return list(sess.exec(q).all())


@router.put("/stewards", response_model=ServerOut)
def set_stewards(server_id: int, body: StewardsIn, sess: SessionDep) -> ServerOut:
    """off: the engine ignores the server. shadow: it records incidents and does nothing else."""
    s = _get(sess, server_id)
    s.stewards = body.mode
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return _out(s)


class LimitsIn(BaseModel):
    """What a driver's CSP script (static/csp/opr_cuts.lua on the website) saw: it was off the track for `ms`."""

    car_id: int = Field(ge=0, le=255)  # its server slot (ac.getCar(0).sessionID) == the ACSP car_id
    name: str = Field(max_length=80)  # the driver's name, to check the slot is theirs
    ms: int = Field(ge=0, le=600_000)  # time spent outside
    wheels: int = Field(ge=0, le=4)  # most wheels outside during it
    speed: float = Field(ge=0, le=1000)  # top speed outside, km/h
    lap: int = Field(default=0, ge=0, le=10_000)
    spline: float = Field(default=0.0, ge=0, le=1)  # track position where it started
    pos: list[float] = Field(default_factory=list, max_length=3)  # world x, y, z where it started


@router.post("/stewards/report", status_code=204)
def report_limits(server_id: int, body: LimitsIn, sess: SessionDep) -> None:
    """Track-limit evidence from the driver's own game (sent through the website, which checks nothing but the shape: this route does the rest).
    204 recorded or ignored on purpose (mode off, flood) · 409 the server is not running or that slot is not that driver."""
    _get(sess, server_id)
    inst = next((i for i in supervisor.live() if i.server_id == server_id), None)
    if not inst or not inst.acsp:
        raise HTTPException(409, "server not running")
    cars = inst.acsp.cars
    car = cars.get(body.car_id)
    if not car or (car.get("driver_name") or "").strip().lower() != body.name.strip().lower():
        raise HTTPException(409, "that car is not that driver")
    engine.record_limits(inst.acsp, body.car_id, body.name, body.model_dump(exclude={"car_id", "name"}))
