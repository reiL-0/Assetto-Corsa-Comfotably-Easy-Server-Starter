"""Incident list and the on/off switch of the automatic stewards (steward reads, admin writes: the router guard in api/v1)."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import select

from app.db import SessionDep
from app.models import Incident
from app.servers import ServerOut, _get, _out

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
