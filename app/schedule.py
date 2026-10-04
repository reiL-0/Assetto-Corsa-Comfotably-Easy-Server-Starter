"""Scheduled starts: a saved event runs on a server at a set time, with Discord reminders before it.

`run_forever` ticks every TICK seconds from the app lifespan. Each tick, for every pending schedule: post the nearest
reminder that has come due (older missed ones are marked sent, not posted), and at `start_at` load the event onto the
server and restart it (`servers.apply_to_server`). A start that is more than LATE seconds overdue (the manager was down)
is marked `missed` instead of surprising the players. Starting restarts the server: whoever is on it is disconnected.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import discord
from app.db import SessionDep, engine
from app.models import Event, Schedule, Server
from app.servers import SessionIn, apply_to_server

log = logging.getLogger("acmanager.schedule")
router = APIRouter(prefix="/schedules", tags=["schedules"])
TICK = 20
LATE = 600


class ScheduleIn(BaseModel):
    event_id: int
    server_id: int
    start_at: float  # unix seconds
    reminders: list[int] = Field(default=[60, 10], max_length=5)  # minutes before; 1..1440


class ScheduleOut(BaseModel):
    id: int
    event_id: int
    event_title: str
    server_id: int
    server_name: str
    start_at: float
    reminders: list[int]
    sent: list[int]
    state: str
    result: str


def _out(sess: Session, sc: Schedule) -> ScheduleOut:
    ev, srv = sess.get(Event, sc.event_id), sess.get(Server, sc.server_id)
    return ScheduleOut(id=sc.id, event_id=sc.event_id, event_title=ev.title if ev else "(borrado)", server_id=sc.server_id,
                       server_name=srv.name if srv else "(borrado)", start_at=sc.start_at, reminders=sc.reminders, sent=sc.sent,
                       state=sc.state, result=sc.result)


@router.get("", response_model=list[ScheduleOut])
def list_schedules(sess: SessionDep) -> list[ScheduleOut]:
    """Pending ones first (soonest on top), then the 10 most recent finished ones."""
    rows = sess.exec(select(Schedule).order_by(Schedule.start_at)).all()
    pending = [r for r in rows if r.state == "pending"]
    done = [r for r in rows if r.state != "pending"][-10:]
    return [_out(sess, r) for r in pending + done[::-1]]


@router.post("", response_model=ScheduleOut, status_code=201)
def create(body: ScheduleIn, sess: SessionDep) -> ScheduleOut:
    if not sess.get(Event, body.event_id) or not sess.get(Server, body.server_id):
        raise HTTPException(404, "event or server not found")
    if body.start_at <= time.time():
        raise HTTPException(422, "start_at must be in the future")
    if any(not 1 <= m <= 1440 for m in body.reminders):
        raise HTTPException(422, "reminders are minutes between 1 and 1440")
    sc = Schedule(event_id=body.event_id, server_id=body.server_id, start_at=body.start_at, reminders=sorted(set(body.reminders), reverse=True))
    sess.add(sc)
    sess.commit()
    sess.refresh(sc)
    return _out(sess, sc)


@router.delete("/{schedule_id}", status_code=204)
def delete(schedule_id: int, sess: SessionDep) -> None:
    sc = sess.get(Schedule, schedule_id)
    if not sc:
        raise HTTPException(404, "schedule not found")
    sess.delete(sc)
    sess.commit()


def _when(sc: Schedule) -> str:
    return f"<t:{int(sc.start_at)}:F> (<t:{int(sc.start_at)}:R>)"  # Discord shows it in each reader's own time zone


async def tick(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    with Session(engine) as sess:
        for sc in sess.exec(select(Schedule).where(Schedule.state == "pending")).all():
            ev, srv = sess.get(Event, sc.event_id), sess.get(Server, sc.server_id)
            if not ev or not srv:
                sc.state, sc.result = "failed", "event or server was deleted"
            else:
                due = [m for m in sc.reminders if m not in sc.sent and now >= sc.start_at - m * 60]
                if due and now < sc.start_at:
                    discord.announce(f"⏰ **{ev.title}** en {srv.name}: empieza {_when(sc)}")
                    sc.sent = [*sc.sent, *due]
                if now >= sc.start_at:
                    if now - sc.start_at > LATE:
                        sc.state, sc.result = "missed", "the manager was not running at the start time"
                    else:
                        try:
                            await apply_to_server(sess, srv, SessionIn(**ev.data).model_copy(update={"restart": True}))
                            sc.state = "done"
                            discord.announce(f"🏁 **{ev.title}** ya está en marcha en {srv.name}")
                        except HTTPException as e:
                            sc.state, sc.result = "failed", str(e.detail)
                            discord.announce(f"⚠️ **{ev.title}** no pudo iniciarse en {srv.name}: {e.detail}")
            sess.add(sc)
            sess.commit()


async def run_forever() -> None:
    while True:
        try:
            await tick()
        except Exception:
            log.exception("schedule tick failed")
        await asyncio.sleep(TICK)
