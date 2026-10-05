"""Kick and ban by Steam ID, from every server at once (stewards).

A ban is a row in `Ban`: whoever has that Steam ID is kicked the moment they connect, on any server (`acsp.ACSPClient._apply` asks
`is_banned`), and kicked right away if they are on one now. It does not depend on acServer's own blacklist, so it also holds for a
server woken later, and removing the row lifts it at once. ponytail: the banned player does connect for an instant before the kick;
a blacklist.txt written at start would refuse them at the door, add it if that flicker matters.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import metrics, supervisor
from app.auth import CurrentUser, require
from app.db import SessionDep, engine
from app.live import acsp
from app.models import Ban

router = APIRouter(tags=["bans"], dependencies=[Depends(require("steward"))])


class BanIn(BaseModel):
    guid: str = Field(pattern=r"^\d{17}$")
    name: str = Field(default="", max_length=80)
    reason: str = Field(min_length=3, max_length=300)


def is_banned(guid: str) -> bool:
    with Session(engine) as s:
        return s.get(Ban, guid) is not None


def kick_everywhere(guid: str) -> int:
    """Kick this Steam ID from every running server it is on; returns how many cars were kicked."""
    n = 0
    for inst in supervisor.live():
        for car_id, car in list(inst.acsp.cars.items()):
            if car.get("driver_guid") == guid:
                inst.acsp.send(acsp.encode_kick_user(car_id))
                n += 1
    return n


@router.get("/bans", response_model=list[Ban])
def list_bans(sess: SessionDep) -> list[Ban]:
    return list(sess.exec(select(Ban).order_by(Ban.created_at.desc())).all())


@router.post("/bans", response_model=dict, status_code=201)
def add_ban(body: BanIn, sess: SessionDep, user: CurrentUser) -> dict:
    sess.merge(Ban(guid=body.guid, name=body.name, reason=body.reason.strip(), created_by=user.username))
    sess.commit()
    n = kick_everywhere(body.guid)
    metrics.log(0, "ban", guid=body.guid, name=body.name or body.guid)
    return {"banned": True, "kicked": n}


@router.delete("/bans/{guid}", status_code=204)
def remove_ban(guid: str, sess: SessionDep) -> None:
    b = sess.get(Ban, guid)
    if not b:
        raise HTTPException(404, "not banned")
    sess.delete(b)
    sess.commit()


@router.post("/players/{guid}/kick")
def kick_player(guid: str) -> dict:
    """Kick the player with this Steam ID from whatever server they are on (they can reconnect: that is a kick, not a ban)."""
    if not (guid.isdigit() and len(guid) == 17):
        raise HTTPException(422, "a Steam ID has 17 digits")
    return {"kicked": kick_everywhere(guid)}
