"""Stewards' penalties on a result file: stored apart, applied when the result is read (app.results.apply_penalties)."""

from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlmodel import select

from app import discord
from app.auth import CurrentUser, require
from app.db import SessionDep
from app.models import Championship, ChampionshipEvent, Notice, Penalty, ResultClose, _now
from app.results import TIMED, parse_result_file, penalties_for
from app.rulebook import effective, legacy_catalogue
from app.servers import _get, result_path

# stewards write (the router default would make every write admin-only)
router = APIRouter(
    prefix="/servers/{server_id}/results/{filename}/penalties", tags=["penalties"], dependencies=[Depends(require("steward"))]
)

# kind -> (what `value` means, min, max)
LIMITS = {"time": ("seconds added to the race time", 1, 3600), "stopgo": ("seconds of the stop-and-go, added to the race time", 1, 60), "position": ("places lost", 1, 30),
          "grid": ("places lost on the next race's grid", 1, 30), "points": ("championship points taken", 1, 100),
          "dsq": ("unused", 0, 0)}


class PenaltyIn(BaseModel):
    driver_guid: str = Field(pattern=r"^\d{17}$")
    kind: Literal["time", "stopgo", "position", "dsq", "grid", "points"] | None = None  # not needed with `item`
    value: int = 0  # meaning depends on `kind`, see LIMITS
    item: str | None = None  # a name from the result's league catalogue (Championship.penalties): sets kind and value
    reason: str = Field(default="", max_length=300)  # a steward always says why (with `item`, the item's name is enough)


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
                      value=p.value // 1000 if p.kind in TIMED else p.value)


def _existing_result(sess: SessionDep, server_id: int, filename: str) -> dict:
    _get(sess, server_id)
    path = result_path(server_id, filename)
    if not path.is_file():
        raise HTTPException(404, "result not found")
    return parse_result_file(path)


def _driver_name(parsed: dict, guid: str) -> str:
    return next((x["driver_name"] for x in parsed["classification"] + parsed["laps"] if x["driver_guid"] == guid and x["driver_name"]), guid)


def _league_of(sess: SessionDep, server_id: int, filename: str) -> Championship | None:
    """The league this result counts for, if any."""
    row = sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.server_id == server_id, ChampionshipEvent.filename == filename)).first()
    return sess.get(Championship, row.championship_id) if row else None


_write = threading.Lock()   # check "is it closed?" and write under one candado: a close cannot land between them (one manager process, as app/leaguelock.py)


def _open(sess: SessionDep, server_id: int, filename: str) -> None:
    if sess.get(ResultClose, (server_id, filename)):
        raise HTTPException(409, "this result is closed: it takes no more penalties")


def _new_penalty(sess: SessionDep, server_id: int, filename: str, parsed: dict, guid: str, kind: str, value: int, reason: str, who: str) -> PenaltyOut:
    """Store a penalty (`value` as Penalty.value: time in ms) and announce it; the caller has already validated it."""
    p = Penalty(server_id=server_id, filename=filename, driver_guid=guid, kind=kind, value=value, reason=reason, created_by=who)
    sess.add(p)
    sess.commit()
    sess.refresh(p)
    out = _out(p)
    discord.announce(discord.penalty_message(_get(sess, server_id).name, parsed, _driver_name(parsed, guid), out.kind, out.value, out.reason))
    return out


@router.get("/catalogue")
def catalogue(server_id: int, filename: str, sess: SessionDep) -> list[dict]:
    """The penalties the steward can pick for this result: its league's catalogue (empty when it is not a league's)."""
    _existing_result(sess, server_id, filename)
    league = _league_of(sess, server_id, filename)
    return legacy_catalogue(effective(league)) if league else []


@router.get("", response_model=list[PenaltyOut])
def list_penalties(server_id: int, filename: str, sess: SessionDep) -> list[PenaltyOut]:
    _existing_result(sess, server_id, filename)
    return [_out(p) for p in penalties_for(sess, server_id, filename)]


@router.post("", response_model=PenaltyOut, status_code=201)
def add_penalty(server_id: int, filename: str, body: PenaltyIn, sess: SessionDep, user: CurrentUser) -> PenaltyOut:
    parsed = _existing_result(sess, server_id, filename)
    _open(sess, server_id, filename)
    league = _league_of(sess, server_id, filename)
    if body.item:
        found = next((i for i in legacy_catalogue(effective(league)) if i["name"] == body.item), None) if league else None
        if not found:
            raise HTTPException(422, f"{body.item!r} is not in the penalty catalogue of this result's league")
        body.kind, body.value = ("dsq", 0) if found["dsq"] else ("stopgo" if found["kind"] == "stopgo" else "time", found["seconds"])   # a drive-through is charged as time (app/rulebook.py)
        body.reason = f"{found['name']}: {body.reason.strip()}" if body.reason.strip() else found["name"]
    if body.kind is None:
        raise HTTPException(422, "kind or item is required")
    if len(body.reason.strip()) < 3:
        raise HTTPException(422, "a steward always says why (reason, at least 3 characters)")
    if league and effective(league).catalogue and body.kind == "points":   # a league with a catalogue penalises with race time
        raise HTTPException(422, "leagues penalise with race time, not championship points")
    what, lo, hi = LIMITS[body.kind]
    if not lo <= body.value <= hi:
        raise HTTPException(422, f"{body.kind}: value is {what}, between {lo} and {hi}" if hi else f"{body.kind} takes no value")
    guids = {e["driver_guid"] for e in parsed["classification"]} | {lap["driver_guid"] for lap in parsed["laps"]}
    if body.driver_guid not in guids:
        raise HTTPException(400, "that driver is not in this result")
    with _write:
        _open(sess, server_id, filename)
        return _new_penalty(sess, server_id, filename, parsed, body.driver_guid, body.kind, body.value * 1000 if body.kind in TIMED else body.value, body.reason.strip(), user.username)


@router.delete("/{penalty_id}", status_code=204)
def remove_penalty(server_id: int, filename: str, penalty_id: int, sess: SessionDep) -> None:
    p = sess.get(Penalty, penalty_id)
    if not p or (p.server_id, p.filename) != (server_id, filename):
        raise HTTPException(404, "penalty not found")
    out, parsed = _out(p), _existing_result(sess, server_id, filename)
    with _write:
        _open(sess, server_id, filename)
        sess.delete(p)
        sess.commit()
    discord.announce(discord.penalty_message(_get(sess, server_id).name, parsed, _driver_name(parsed, p.driver_guid), out.kind, out.value))


# --- warnings for the race director (app/stewards/auto.py) and closing the result ---

class NoticeOut(BaseModel):
    id: int
    driver_guid: str
    driver_name: str
    kind: str
    penalty_kind: str
    value: int  # as the API hands penalties out (time: seconds)
    reason: str


class NoticesOut(BaseModel):
    closed: bool
    notices: list[NoticeOut]


def _notice(sess: SessionDep, server_id: int, filename: str, notice_id: int) -> Notice:
    n = sess.get(Notice, notice_id)
    if not n or (n.server_id, n.filename) != (server_id, filename):
        raise HTTPException(404, "notice not found")
    return n


@router.get("/notices", response_model=NoticesOut)
def list_notices(server_id: int, filename: str, sess: SessionDep) -> NoticesOut:
    parsed = _existing_result(sess, server_id, filename)
    rows = sess.exec(select(Notice).where(Notice.server_id == server_id, Notice.filename == filename).order_by(Notice.id)).all()
    return NoticesOut(closed=bool(sess.get(ResultClose, (server_id, filename))), notices=[
        NoticeOut(id=n.id, driver_guid=n.driver_guid, driver_name=_driver_name(parsed, n.driver_guid), kind=n.kind, penalty_kind=n.penalty_kind,
                  value=n.value // 1000 if n.penalty_kind in TIMED else n.value, reason=n.reason) for n in rows])


@router.post("/notices/{notice_id}/confirm", response_model=PenaltyOut, status_code=201)
def confirm_notice(server_id: int, filename: str, notice_id: int, sess: SessionDep, user: CurrentUser) -> PenaltyOut:
    """The race director agrees: the warning becomes a penalty under their name and the warning is gone."""
    parsed = _existing_result(sess, server_id, filename)
    _open(sess, server_id, filename)   # (early, for the plain answer; the one under the candado is the one that counts)
    n = _notice(sess, server_id, filename, notice_id)
    with _write:
        _open(sess, server_id, filename)
        if sess.execute(delete(Notice).where(Notice.id == notice_id)).rowcount != 1:   # a second click, or another steward, got there first
            sess.rollback()
            raise HTTPException(404, "notice not found")
        return _new_penalty(sess, server_id, filename, parsed, n.driver_guid, n.penalty_kind, n.value, f"{n.reason} (aviso confirmado)", user.username)


@router.delete("/notices/{notice_id}", status_code=204)
def dismiss_notice(server_id: int, filename: str, notice_id: int, sess: SessionDep) -> None:
    n = _notice(sess, server_id, filename, notice_id)
    sess.delete(n)
    sess.commit()


@router.post("/close", status_code=204)
def close_result(server_id: int, filename: str, sess: SessionDep, user: CurrentUser) -> None:
    """Close the result: the warnings left are deleted and it takes no more penalties. Closing twice is fine."""
    _existing_result(sess, server_id, filename)
    with _write:
        sess.execute(delete(Notice).where(Notice.server_id == server_id, Notice.filename == filename))
        if not sess.get(ResultClose, (server_id, filename)):
            sess.add(ResultClose(server_id=server_id, filename=filename, closed_by=user.username))
        sess.commit()
