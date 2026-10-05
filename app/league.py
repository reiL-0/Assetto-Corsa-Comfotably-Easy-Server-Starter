"""Leagues: the roster of a championship (by Steam ID), the practice requirement and the closed entry list built from them.

A league is a `Championship` (points table in app/championship.py, penalty catalogue in app/penalties.py) plus a roster of
`LeagueMember`s. An `Event` with `league_id` runs with the roster as its entry list, locked, whoever the preset had: `session_for`
builds it each time the event is loaded (schedule start, wake, manual run), so the practice requirement is judged at that moment.
Practice = valid laps (no track cuts, `Activity.cuts == 0`) logged for the driver's Steam ID in the last `practice_days` days, on any
server and track. Laps logged before `cuts` was recorded have none and do not count.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from app import discord
from app.db import SessionDep
from app.models import Activity, Championship, Event, LeagueMember
from app.servers import EntryIn, SessionIn

router = APIRouter(prefix="/championships/{championship_id}/members", tags=["leagues"])


class MemberIn(BaseModel):
    guid: str = Field(pattern=r"^\d{17}$")
    name: str = Field(default="", max_length=60)
    team: str = Field(default="", max_length=60)
    car: str = Field(default="", max_length=80)
    exempt: bool = False


class MemberOut(MemberIn):
    laps: int  # valid laps in the practice window, now
    eligible: bool


def valid_laps(sess: Session, guid: str, since: float, until: float) -> int:
    return sess.exec(select(func.count()).select_from(Activity).where(
        Activity.kind == "lap", Activity.guid == guid, Activity.cuts == 0, Activity.ts >= since, Activity.ts <= until)).one()


def is_eligible(c: Championship, m: LeagueMember, laps: int) -> bool:
    return not c.practice_required or m.exempt or laps >= c.practice_laps


def _league(sess: Session, championship_id: int) -> Championship:
    c = sess.get(Championship, championship_id)
    if not c:
        raise HTTPException(404, "championship not found")
    return c


def _status(sess: Session, c: Championship, m: LeagueMember, at: float) -> MemberOut:
    laps = valid_laps(sess, m.guid, at - c.practice_days * 86400, at)
    return MemberOut(guid=m.guid, name=m.name, team=m.team, car=m.car, exempt=m.exempt, laps=laps, eligible=is_eligible(c, m, laps))


@router.get("", response_model=list[MemberOut])
def list_members(championship_id: int, sess: SessionDep) -> list[MemberOut]:
    """The roster with each driver's valid laps in the window and whether they would be let in if the event started now."""
    c, now = _league(sess, championship_id), time.time()
    rows = sess.exec(select(LeagueMember).where(LeagueMember.championship_id == championship_id).order_by(LeagueMember.name)).all()
    return [_status(sess, c, m, now) for m in rows]


@router.post("", response_model=MemberOut)
def add_member(championship_id: int, body: MemberIn, sess: SessionDep) -> MemberOut:
    """Add a driver or update the one with that Steam ID."""
    c = _league(sess, championship_id)
    m = sess.merge(LeagueMember(championship_id=championship_id, **body.model_dump()))
    sess.commit()
    return _status(sess, c, m, time.time())


@router.delete("/{guid}", status_code=204)
def remove_member(championship_id: int, guid: str, sess: SessionDep) -> None:
    m = sess.get(LeagueMember, (championship_id, guid))
    if not m:
        raise HTTPException(404, "driver not on the roster")
    sess.delete(m)
    sess.commit()


def session_for(sess: Session, ev: Event, at: float) -> SessionIn:
    """The event's session as it must be loaded at `at` (unix s): for a league event, the roster's eligible drivers as a locked
    entry list; for any other event, the saved session untouched. 409 when nobody is eligible or the roster does not fit."""
    s = SessionIn(**ev.data)
    c = sess.get(Championship, ev.league_id) if ev.league_id else None
    if not c:
        return s
    ok, out = [], []
    for m in sess.exec(select(LeagueMember).where(LeagueMember.championship_id == c.id).order_by(LeagueMember.name)).all():
        st = _status(sess, c, m, at)
        (ok if st.eligible else out).append(st)
    if not ok:
        raise HTTPException(409, f"league {c.name!r}: no driver is eligible (roster empty or practice requirement not met)")
    if len(ok) > 50:
        raise HTTPException(409, f"league {c.name!r}: {len(ok)} drivers; a server takes 50")
    cars = s.cars or sorted({e.model for e in s.entries})
    if not cars:
        raise HTTPException(422, "a league event needs cars")
    if out:
        discord.alert(f"⚠️ **{c.name}** · {ev.title}: quedan fuera de la lista por no cumplir la práctica ({c.practice_laps} vueltas válidas en {c.practice_days} días): "
                      + ", ".join(f"{m.name or m.guid} ({m.laps}/{c.practice_laps})" for m in out))
    entries = [EntryIn(model=m.car if m.car in cars else cars[0], driver_name=m.name, team=m.team, guid=m.guid) for m in ok]
    return s.model_copy(update={"entries": entries, "locked": True, "pickup": False, "cars": cars})
