"""Saved events: a full session (the new-session form) kept under a title and run on a server with one call."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlmodel import select

from app.db import SessionDep
from app.models import Event
from app.servers import AppliedOut, SessionIn, _get, apply_to_server

router = APIRouter(prefix="/events", tags=["events"])


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=500)
    session: SessionIn  # same shape the server's /apply takes; its `restart` is chosen when the event is run
    derived: bool = False  # the calendar sync's per-event copy of a preset; can be set, never cleared

    @model_validator(mode="after")
    def _entries_fit(self) -> EventIn:
        """An entry list saved in a preset must be usable: each Steam ID once, each car one the session enables.
        (Not on SessionIn: presets saved before this check must still load.)"""
        s, seen = self.session, set()
        for i, e in enumerate(s.entries, 1):
            if s.cars and e.model not in s.cars:
                raise ValueError(f"entry {i}: car {e.model!r} is not one of the session's cars")
            for g in filter(None, e.guid.split(";")):
                if g in seen:
                    raise ValueError(f"entry {i}: Steam ID {g} appears twice")
                seen.add(g)
        if s.locked and not seen:
            raise ValueError("a locked entry list needs at least one Steam ID")
        return self


class EventOut(BaseModel):
    id: int
    title: str
    notes: str
    session: SessionIn
    is_default: bool
    derived: bool
    updated_at: datetime


class RunIn(BaseModel):
    server_id: int
    restart: bool = True


def _out(e: Event) -> EventOut:
    return EventOut(id=e.id, title=e.title, notes=e.notes, session=SessionIn(**e.data), is_default=e.is_default,
                    derived=e.derived, updated_at=e.updated_at)


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
    e = Event(title=body.title, notes=body.notes, data=body.session.model_dump(), derived=body.derived)
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
    e.derived = e.derived or body.derived  # a calendar copy made before the flag existed gets it on its next sync; never cleared
    e.updated_at = datetime.now(UTC)
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return _out(e)


@router.post("/{event_id}/default", response_model=EventOut)
def set_default(event_id: int, sess: SessionDep) -> EventOut:
    """Make this preset the default one (the calendar starts new events from it); the previous default stops being it."""
    e = _get_event(sess, event_id)
    if e.derived:
        raise HTTPException(400, "a calendar event's own copy cannot be the default preset")
    for other in sess.exec(select(Event).where(Event.is_default)).all():
        other.is_default = False
        sess.add(other)
    e.is_default = True
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
