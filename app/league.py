"""Leagues: the roster of a championship (by Steam ID), the practice requirement and the closed entry list built from them.

A league is a `Championship` (points table in app/championship.py, penalty catalogue in app/penalties.py) plus a roster of
`LeagueMember`s. An `Event` with `league_id` runs with the roster as its entry list, locked, whoever the preset had: `session_for`
builds it each time the event is loaded (schedule start, wake, manual run), so the practice requirement is judged at that moment.
A driver can be suspended (`LeagueSuspension`): banned until lifted, out for some days, out for the next N races, kept from qualifying
for N races (kicked while the server is in qualifying, `qualy_banned`, so they start last), or sent back N grid places counted from
where they qualified (`count_results` turns each counted Qualify result into a `Penalty` of kind grid; what does not fit, because the
driver is already near the back, is carried to the next race). Cars that do not race (`LeagueMember.non_racing`: safety car, race
director, caster) never count for any of this.

Practice = valid laps (no track cuts, `Activity.cuts == 0`) logged for the driver's Steam ID in the last `practice_days` days, on any
server and track. Laps logged before `cuts` was recorded have none and do not count.
"""

from __future__ import annotations

import time
from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlmodel import Session, func, select

from app import discord
from app.db import SessionDep
from app.config import settings
from app.auth import CurrentUser
from app.db import engine
from app.models import Activity, Championship, ChampionshipEvent, Event, LeagueMember, LeagueSuspension, Penalty, Schedule
from app.results import parse_result_file
from app.schemas.servers import EntryIn, SessionIn

router = APIRouter(prefix="/championships/{championship_id}/members", tags=["leagues"])


class MemberIn(BaseModel):
    guid: str = Field(pattern=r"^\d{17}$")
    name: str = Field(default="", max_length=60)
    team: str = Field(default="", max_length=60)
    car: str = Field(default="", max_length=80)
    exempt: bool = False
    non_racing: bool = False  # safety car, race director, caster: on the entry list, but not racing


class MemberOut(MemberIn):
    laps: int  # valid laps in the practice window, now
    eligible: bool


def valid_laps(sess: Session, guid: str, since: float, until: float) -> int:
    return sess.exec(select(func.count()).select_from(Activity).where(
        Activity.kind == "lap", Activity.guid == guid, Activity.cuts == 0, Activity.ts >= since, Activity.ts <= until)).one()


def is_eligible(c: Championship, m: LeagueMember, laps: int) -> bool:
    return not c.practice_required or m.exempt or m.non_racing or laps >= c.practice_laps


def _league(sess: Session, championship_id: int) -> Championship:
    c = sess.get(Championship, championship_id)
    if not c:
        raise HTTPException(404, "championship not found")
    return c


def _status(sess: Session, c: Championship, m: LeagueMember, at: float) -> MemberOut:
    laps = valid_laps(sess, m.guid, at - c.practice_days * 86400, at)
    return MemberOut(guid=m.guid, name=m.name, team=m.team, car=m.car, exempt=m.exempt, non_racing=m.non_racing, laps=laps, eligible=is_eligible(c, m, laps))


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
    ok, out, suspended = [], [], []
    out_of_races = {x.guid: x for x in active_suspensions(sess, c.id, ("ban", "time", "races"), at)}
    for m in sess.exec(select(LeagueMember).where(LeagueMember.championship_id == c.id).order_by(LeagueMember.name)).all():
        st = _status(sess, c, m, at)
        if m.guid in out_of_races and not m.non_racing:
            suspended.append(st)
        else:
            (ok if st.eligible else out).append(st)
    if not ok:
        raise HTTPException(409, f"league {c.name!r}: no driver is eligible (roster empty or practice requirement not met)")
    if len(ok) > 50:
        raise HTTPException(409, f"league {c.name!r}: {len(ok)} drivers; a server takes 50")
    cars = s.cars or sorted({e.model for e in s.entries})
    if not cars:
        raise HTTPException(422, "a league event needs cars")
    if suspended:
        discord.alert(f"⛔ **{c.name}** · {ev.title}: suspendidos, quedan fuera de la lista: " + ", ".join(m.name or m.guid for m in suspended))
    if out:
        discord.alert(f"⚠️ **{c.name}** · {ev.title}: quedan fuera de la lista por no cumplir la práctica ({c.practice_laps} vueltas válidas en {c.practice_days} días): "
                      + ", ".join(f"{m.name or m.guid} ({m.laps}/{c.practice_laps})" for m in out))
    entries = [EntryIn(model=m.car if m.car in cars else cars[0], driver_name=m.name, team=m.team, guid=m.guid) for m in ok]
    return s.model_copy(update={"entries": entries, "locked": True, "pickup": False, "cars": cars})


def active_suspensions(sess: Session, championship_id: int, kinds: tuple[str, ...], at: float, guid: str | None = None) -> list[LeagueSuspension]:
    q = select(LeagueSuspension).where(LeagueSuspension.championship_id == championship_id, LeagueSuspension.active, LeagueSuspension.kind.in_(kinds))
    if guid:
        q = q.where(LeagueSuspension.guid == guid)
    return [x for x in sess.exec(q).all() if x.kind != "time" or (x.until or 0) > at]


def qualy_banned(server_id: int, guid: str) -> bool:
    """Is this driver kept from qualifying on this server right now? The server must be running a league event (its schedule is running)."""
    with Session(engine) as s:
        sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
        ev = s.get(Event, sc.event_id) if sc else None
        return bool(ev and ev.league_id and active_suspensions(s, ev.league_id, ("qualy",), time.time(), guid))


def _qualy_grid(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict) -> None:
    """Turn the league's grid suspensions into a grid Penalty on this qualifying result: the places are counted from where each driver
    qualified among the cars that race, and what does not fit (already near the back) stays for the next qualifying."""
    nonrun = set(sess.exec(select(LeagueMember.guid).where(LeagueMember.championship_id == c.id, LeagueMember.non_racing)).all())
    order = [e["driver_guid"] for e in parsed["classification"] if e["driver_guid"] and e["driver_guid"] not in nonrun]
    for x in active_suspensions(sess, c.id, ("grid",), time.time()):
        if filename in x.served or x.guid not in order or x.guid in nonrun:
            continue   # did not qualify in this one: the places wait for the next
        moved = min(x.places_left, len(order) - 1 - order.index(x.guid))
        x.served = [*x.served, filename]
        x.places_left -= moved
        x.active = x.places_left > 0
        if moved:
            sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=x.guid, kind="grid", value=moved, created_by=x.created_by,
                             reason=f"Suspensión de liga: pierde {moved} lugares en la parrilla" + (f" (le quedan {x.places_left} para la siguiente)" if x.places_left else "")))
            discord.announce(f"⛔ **{c.name}**: {x.name or x.guid} pierde {moved} lugares en la parrilla" + (f"; {x.places_left} más en la siguiente carrera" if x.places_left else ""))
        sess.add(x)


def _race_served(sess: Session, c: Championship, finished_at: float) -> None:
    """A league race was counted: one less race for every suspension measured in races (the ones put before it ended)."""
    for x in active_suspensions(sess, c.id, ("races", "qualy"), finished_at):
        if x.created_at.replace(tzinfo=timezone.utc).timestamp() <= finished_at:
            x.races_left -= 1
            x.active = x.races_left > 0
            sess.add(x)


def count_results(sess: Session, sc: Schedule, ev: Event, until: float) -> int:
    """Count, for the league of `ev`, the Race and Qualify results its schedule produced: files in the server's results folder written
    between the schedule's start and `until`. Idempotent; returns how many Race results were added. A calendar event is thus a counted
    race of its league (draft or published) without anyone picking files. A Race scores (app/championship.py) and uses up the
    suspensions measured in races; a Qualify result feeds the grid suspensions; practice results are skipped."""
    if not ev.league_id or not (c := sess.get(Championship, ev.league_id)):
        return 0
    d, added = Path(settings.data_dir) / "instances" / str(sc.server_id) / "results", 0
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        if not sc.start_at <= f.stat().st_mtime <= until:
            continue
        if sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == c.id, ChampionshipEvent.server_id == sc.server_id,
                                                      ChampionshipEvent.filename == f.name)).first():
            continue
        parsed = parse_result_file(f)
        if parsed["type"] not in ("Race", "Qualify"):
            continue
        sess.add(ChampionshipEvent(championship_id=c.id, server_id=sc.server_id, filename=f.name, event_id=ev.id, session_type=parsed["type"]))
        if parsed["type"] == "Race":
            added += 1
            _race_served(sess, c, f.stat().st_mtime)
        else:
            _qualy_grid(sess, c, sc.server_id, f.name, parsed)
    sess.commit()
    return added


# --- suspensions ---

susp_router = APIRouter(prefix="/championships/{championship_id}/suspensions", tags=["leagues"])


class SuspensionIn(BaseModel):
    guid: str = Field(pattern=r"^\d{17}$")
    kind: Literal["ban", "time", "races", "qualy", "grid"]
    days: int | None = Field(default=None, ge=1, le=365)  # time
    races: int | None = Field(default=None, ge=1, le=50)  # races, qualy
    places: int | None = Field(default=None, ge=1, le=100)  # grid
    reason: str = Field(min_length=3, max_length=300)

    @model_validator(mode="after")
    def _has_its_amount(self) -> SuspensionIn:
        need = {"time": self.days, "races": self.races, "qualy": self.races, "grid": self.places}
        if self.kind in need and need[self.kind] is None:
            raise ValueError({"time": "days", "races": "races", "qualy": "races", "grid": "places"}[self.kind] + " is required for this kind")
        return self


def _describe(x: LeagueSuspension) -> str:
    return {"ban": "baneo de la liga", "time": f"suspendido hasta <t:{int(x.until or 0)}:F>", "races": f"suspendido {x.races_left} carrera(s)",
            "qualy": f"sin clasificar {x.races_left} carrera(s)", "grid": f"pierde {x.places_left} lugares en la parrilla"}[x.kind]


@susp_router.get("")
def list_suspensions(championship_id: int, sess: SessionDep) -> list[dict]:
    """Active ones first, then the 20 most recent finished."""
    _league(sess, championship_id)
    rows = sess.exec(select(LeagueSuspension).where(LeagueSuspension.championship_id == championship_id).order_by(LeagueSuspension.id.desc())).all()
    now = time.time()
    live = [x for x in rows if x.active and not (x.kind == "time" and (x.until or 0) <= now)]
    old = [x for x in rows if x not in live][:20]
    return [{"id": x.id, "guid": x.guid, "name": x.name, "kind": x.kind, "detail": _describe(x), "until": x.until, "races_left": x.races_left,
             "places_left": x.places_left, "reason": x.reason, "created_by": x.created_by, "active": x in live} for x in live + old]


@susp_router.post("", status_code=201)
def add_suspension(championship_id: int, body: SuspensionIn, sess: SessionDep, user: CurrentUser) -> dict:
    c = _league(sess, championship_id)
    m = sess.get(LeagueMember, (championship_id, body.guid))
    if not m:
        raise HTTPException(404, "driver not on the roster")
    if m.non_racing:
        raise HTTPException(422, "a car that does not race cannot be suspended")
    x = LeagueSuspension(championship_id=championship_id, guid=body.guid, name=m.name, kind=body.kind, reason=body.reason.strip(), created_by=user.username,
                         until=time.time() + body.days * 86400 if body.kind == "time" else None,
                         races_left=body.races or 0 if body.kind in ("races", "qualy") else 0, places_left=body.places or 0 if body.kind == "grid" else 0)
    sess.add(x)
    sess.commit()
    sess.refresh(x)
    discord.announce(f"⛔ **{c.name}**: {m.name or m.guid} — {_describe(x)}. Motivo: {x.reason}")
    return {"id": x.id, "detail": _describe(x)}


@susp_router.delete("/{suspension_id}", status_code=204)
def lift_suspension(championship_id: int, suspension_id: int, sess: SessionDep) -> None:
    x = sess.get(LeagueSuspension, suspension_id)
    if not x or x.championship_id != championship_id:
        raise HTTPException(404, "suspension not found")
    x.active = False
    sess.add(x)
    sess.commit()
