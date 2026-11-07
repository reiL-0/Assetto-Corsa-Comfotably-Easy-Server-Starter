"""Saved events: a full session (the new-session form) kept under a title and run on a server with one call."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlmodel import select

from app.db import SessionDep
from app.league import session_for, settle_metrics
from app.leaguelock import serialized
from app.models import Championship, Event
from app.rulebook import CutRule
from app.servers import AppliedOut, SessionIn, _get, apply_to_server

router = APIRouter(prefix="/events", tags=["events"])


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=500)
    session: SessionIn  # same shape the server's /apply takes; its `restart` is chosen when the event is run
    derived: bool = False  # the calendar sync's per-event copy of a preset; can be set, never cleared
    league_id: int | None = None  # run with this league's eligible roster as the (locked) entry list (app/league.py)
    count_only_league_id: int | None = None  # a practice: its result is listed in this league's results with no points; the entry list stays open
    cuts_rule: CutRule | None = None  # omitted keeps the saved choice on PUT; explicit null switches cuts off
    inherit_cuts_rule: bool = False  # true removes an override so the event inherits league rulebook

    @model_validator(mode="after")
    def _cut_choice(self) -> EventIn:
        if self.inherit_cuts_rule and "cuts_rule" in self.model_fields_set:
            raise ValueError("inherit_cuts_rule and cuts_rule cannot be sent together")
        return self

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
    league_id: int | None
    count_only_league_id: int | None = None
    cuts_rule: CutRule | None = None
    updated_at: datetime


class RunIn(BaseModel):
    server_id: int
    restart: bool = True


def _out(e: Event) -> EventOut:
    return EventOut(id=e.id, title=e.title, notes=e.notes, session=SessionIn(**e.data), is_default=e.is_default,
                    derived=e.derived, league_id=e.league_id, count_only_league_id=e.count_only_league_id,
                    updated_at=e.updated_at, **({"cuts_rule": e.data["cuts_rule"]} if "cuts_rule" in e.data else {}))


def _check_league(sess: SessionDep, league_id: int | None) -> None:
    if league_id is None:
        return
    c = sess.get(Championship, league_id)
    if not c:
        raise HTTPException(404, "league not found")
    if c.archived_at:
        raise HTTPException(409, "league archived: no event can be tied to it")


def _get_event(sess: SessionDep, event_id: int) -> Event:
    e = sess.get(Event, event_id)
    if not e:
        raise HTTPException(404, "event not found")
    return e


@router.get("", response_model=list[EventOut], response_model_exclude_unset=True)
def list_events(sess: SessionDep) -> list[EventOut]:
    return [_out(e) for e in sess.exec(select(Event).order_by(Event.title)).all()]


@router.post("", response_model=EventOut, response_model_exclude_unset=True, status_code=201)
@serialized
def create_event(body: EventIn, sess: SessionDep) -> EventOut:
    _check_league(sess, body.league_id)
    _check_league(sess, body.count_only_league_id)
    if body.league_id is not None and body.count_only_league_id is not None:
        raise HTTPException(422, "league_id and count_only_league_id are not used together")
    data = body.session.model_dump()
    if "cuts_rule" in body.model_fields_set:
        data["cuts_rule"] = body.cuts_rule.model_dump() if body.cuts_rule else None
    e = Event(title=body.title, notes=body.notes, data=data, derived=body.derived, league_id=body.league_id, count_only_league_id=body.count_only_league_id)
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return _out(e)


@router.get("/{event_id}", response_model=EventOut, response_model_exclude_unset=True)
def get_event(event_id: int, sess: SessionDep) -> EventOut:
    return _out(_get_event(sess, event_id))


@router.put("/{event_id}", response_model=EventOut, response_model_exclude_unset=True)
@serialized
def update_event(event_id: int, body: EventIn, sess: SessionDep) -> EventOut:
    e = _get_event(sess, event_id)
    _check_league(sess, body.league_id)
    _check_league(sess, body.count_only_league_id)
    if body.league_id is not None and body.count_only_league_id is not None:
        raise HTTPException(422, "league_id and count_only_league_id are not used together")
    data = body.session.model_dump()
    if "cuts_rule" in body.model_fields_set:
        data["cuts_rule"] = body.cuts_rule.model_dump() if body.cuts_rule else None
    elif not body.inherit_cuts_rule and "cuts_rule" in e.data:  # omission keeps an older client's saved choice; explicit inherit clears it
        data["cuts_rule"] = e.data["cuts_rule"]
    e.title, e.notes, e.data, e.league_id = body.title, body.notes, data, body.league_id
    if "count_only_league_id" in body.model_fields_set:   # an older client does not send it: the saved one stays, unless it names a roster league (then the two cannot both be set)
        e.count_only_league_id = body.count_only_league_id
    elif body.league_id is not None:
        e.count_only_league_id = None
    e.derived = e.derived or body.derived  # a calendar copy made before the flag existed gets it on its next sync; never cleared
    e.updated_at = datetime.now(UTC)
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return _out(e)


@router.post("/{event_id}/default", response_model=EventOut, response_model_exclude_unset=True)
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


@router.post("/{event_id}/duplicate", response_model=EventOut, response_model_exclude_unset=True, status_code=201)
@serialized
def duplicate_event(event_id: int, sess: SessionDep) -> EventOut:
    src = _get_event(sess, event_id)
    league = sess.get(Championship, src.league_id) if src.league_id else None
    e = Event(title=f"{src.title} (copia)"[:80], notes=src.notes, data=dict(src.data), league_id=None if league is None or league.archived_at else league.id, count_only_league_id=src.count_only_league_id if src.count_only_league_id and (c2 := sess.get(Championship, src.count_only_league_id)) and not c2.archived_at else None)   # (a copy never joins an archived league)
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return _out(e)


@router.post("/{event_id}/run", response_model=AppliedOut)
async def run_event(event_id: int, body: RunIn, sess: SessionDep) -> AppliedOut:
    """Load the event onto a server (checked against the content installed right now) and, by default, restart it."""
    await settle_metrics()
    session = session_for(sess, _get_event(sess, event_id), time.time()).model_copy(update={"restart": body.restart})
    return await apply_to_server(sess, _get(sess, body.server_id), session)
