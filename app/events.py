"""Saved events: a full session (the new-session form) kept under a title and run on a server with one call."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from app.db import SessionDep
from app.models import Event
from app.servers import AppliedOut, SessionIn, _get, apply_to_server

router = APIRouter(prefix="/events", tags=["events"])


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=500)
    session: SessionIn  # same shape the server's /apply takes; its `restart` is chosen when the event is run


class EventOut(BaseModel):
    id: int
    title: str
    notes: str
    session: SessionIn
    updated_at: datetime


class RunIn(BaseModel):
    server_id: int
    restart: bool = True


def _out(e: Event) -> EventOut:
    return EventOut(id=e.id, title=e.title, notes=e.notes, session=SessionIn(**e.data), updated_at=e.updated_at)


def _get_event(sess: SessionDep, event_id: int) -> Event:
    e = sess.get(Event, event_id)
    if not e:
        raise HTTPException(404, "event not found")
    return e


@router.get("", response_model=list[EventOut])
def list_events(sess: SessionDep) -> list[EventOut]:
    return [_out(e) for e in sess.exec(select(Event).order_by(Event.title)).all()]


@router.post("", response_model=EventOut, status_code=201)
def create_event(body: EventIn, sess: SessionDep) -> EventOut:
    e = Event(title=body.title, notes=body.notes, data=body.session.model_dump())
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return _out(e)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, sess: SessionDep) -> EventOut:
    return _out(_get_event(sess, event_id))


@router.put("/{event_id}", response_model=EventOut)
def update_event(event_id: int, body: EventIn, sess: SessionDep) -> EventOut:
    e = _get_event(sess, event_id)
    e.title, e.notes, e.data = body.title, body.notes, body.session.model_dump()
    e.updated_at = datetime.now(UTC)
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return _out(e)


@router.delete("/{event_id}", status_code=204)
def delete_event(event_id: int, sess: SessionDep) -> None:
    sess.delete(_get_event(sess, event_id))
    sess.commit()


@router.post("/{event_id}/duplicate", response_model=EventOut, status_code=201)
def duplicate_event(event_id: int, sess: SessionDep) -> EventOut:
    src = _get_event(sess, event_id)
    e = Event(title=f"{src.title} (copia)"[:80], notes=src.notes, data=dict(src.data))
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return _out(e)


@router.post("/{event_id}/run", response_model=AppliedOut)
async def run_event(event_id: int, body: RunIn, sess: SessionDep) -> AppliedOut:
    """Load the event onto a server (checked against the content installed right now) and, by default, restart it."""
    session = SessionIn(**_get_event(sess, event_id).data).model_copy(update={"restart": body.restart})
    return await apply_to_server(sess, _get(sess, body.server_id), session)
