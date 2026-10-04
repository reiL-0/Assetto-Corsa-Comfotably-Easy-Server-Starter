"""Stewards' penalties on a result file: stored apart, applied when the result is read (app.results.apply_penalties)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import CurrentUser, require
from app.db import SessionDep
from app.models import Penalty
from app.results import parse_result_file, penalties_for
from app.servers import _get, result_path

# stewards write (the router default would make every write admin-only)
router = APIRouter(
    prefix="/servers/{server_id}/results/{filename}/penalties", tags=["penalties"], dependencies=[Depends(require("steward"))]
)

# kind -> (what `value` means, min, max)
LIMITS = {"time": ("seconds added to the race time", 1, 3600), "position": ("places lost", 1, 30),
          "grid": ("places lost on the next race's grid", 1, 30), "points": ("championship points taken", 1, 100),
          "dsq": ("unused", 0, 0)}


class PenaltyIn(BaseModel):
    driver_guid: str = Field(pattern=r"^\d{17}$")
    kind: Literal["time", "position", "dsq", "grid", "points"]
    value: int = 0  # meaning depends on `kind`, see LIMITS
    reason: str = Field(min_length=3, max_length=300)  # a steward always says why


class PenaltyOut(BaseModel):
    id: int
    driver_guid: str
    kind: str
    value: int
    reason: str
    created_by: str


def _out(p: Penalty) -> PenaltyOut:
    # time is stored in ms, handed out in seconds like it was given
    return PenaltyOut(id=p.id, driver_guid=p.driver_guid, kind=p.kind, reason=p.reason, created_by=p.created_by,
                      value=p.value // 1000 if p.kind == "time" else p.value)


def _existing_result(sess: SessionDep, server_id: int, filename: str) -> dict:
    _get(sess, server_id)
    path = result_path(server_id, filename)
    if not path.is_file():
        raise HTTPException(404, "result not found")
    return parse_result_file(path)


@router.get("", response_model=list[PenaltyOut])
def list_penalties(server_id: int, filename: str, sess: SessionDep) -> list[PenaltyOut]:
    _existing_result(sess, server_id, filename)
    return [_out(p) for p in penalties_for(sess, server_id, filename)]


@router.post("", response_model=PenaltyOut, status_code=201)
def add_penalty(server_id: int, filename: str, body: PenaltyIn, sess: SessionDep, user: CurrentUser) -> PenaltyOut:
    parsed = _existing_result(sess, server_id, filename)
    what, lo, hi = LIMITS[body.kind]
    if not lo <= body.value <= hi:
        raise HTTPException(422, f"{body.kind}: value is {what}, between {lo} and {hi}" if hi else f"{body.kind} takes no value")
    guids = {e["driver_guid"] for e in parsed["classification"]} | {lap["driver_guid"] for lap in parsed["laps"]}
    if body.driver_guid not in guids:
        raise HTTPException(400, "that driver is not in this result")
    p = Penalty(server_id=server_id, filename=filename, driver_guid=body.driver_guid, kind=body.kind,
                value=body.value * 1000 if body.kind == "time" else body.value, reason=body.reason.strip(),
                created_by=user.username)
    sess.add(p)
    sess.commit()
    sess.refresh(p)
    return _out(p)


@router.delete("/{penalty_id}", status_code=204)
def remove_penalty(server_id: int, filename: str, penalty_id: int, sess: SessionDep) -> None:
    p = sess.get(Penalty, penalty_id)
    if not p or (p.server_id, p.filename) != (server_id, filename):
        raise HTTPException(404, "penalty not found")
    sess.delete(p)
    sess.commit()
