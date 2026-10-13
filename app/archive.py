"""Archiving a league (the owner's rule, 2026-10-12): while a league runs, everything about its races is kept; when it ends and is archived, only the TABLES and
the STANDINGS stay (final table; per counted result the classification, points, best laps and the penalties with their reason) and the fine detail goes: the result
files (every lap and every cut) and the stewards' penalty rows. Nothing about laps or cuts is kept.

`preview` says what would happen without touching anything. `archive` does it once: the snapshot is built from the files first, then ONE database transaction
claims the league (UPDATE ... WHERE archived_at IS NULL: a second archiver gets 409), stores the snapshot and removes the league's event rows and the penalty rows of
the files that no other running league counts; only after the commit are those files deleted (best effort: what cannot be deleted is reported, never retried
silently; an undeleted file is just an unused result). The roster, the suspensions, the stewards' incidents and the activity log are NOT touched: they have
their own life. The nightly backup still holds the result files for as long as its copies are kept (the only way back). Reads of an archived league come from here
(app/championship.py); every write to it answers 409 (championship.get_open).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session, select

from app.auth import CurrentUser
from app.championship import _get, get_open, score
from app.db import SessionDep
from app.leaguelock import serialized
from app.rulebook import effective
from app.models import Championship, ChampionshipArchive, ChampionshipEvent, LeagueMember, Notice, Penalty, ResultClose, _now
from app.servers import result_path

log = logging.getLogger("acmanager.archive")
router = APIRouter(prefix="/championships/{championship_id}/archive", tags=["archive"])
VERSION = 1


def _own_files(sess: Session, c: Championship) -> list[tuple[int, str]]:
    """(server_id, filename) of the league's counted results that NO OTHER running league also counts: the only ones that may be deleted."""
    mine = {(e.server_id, e.filename) for e in sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == c.id))}
    shared = set()
    for e in sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id != c.id)):
        if (e.server_id, e.filename) not in mine:
            continue
        other = sess.get(Championship, e.championship_id)
        if other is not None and not other.archived_at:   # (an archived league keeps no event rows, so this is a running one)
            shared.add((e.server_id, e.filename))
    return sorted(mine - shared)


def _stamp(sess: Session, c: Championship) -> tuple:
    """What the snapshot was built from: the league's event rows, its newest penalty and the files' modification times. If it differs when the claim is made, the
    snapshot is stale (a result or a penalty changed meanwhile) and nothing is archived."""
    events = sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == c.id).order_by(ChampionshipEvent.id)).all()
    files = tuple((e.server_id, e.filename) for e in events)
    newest = max((sess.exec(select(Penalty.id).where(Penalty.server_id == s, Penalty.filename == f).order_by(Penalty.id.desc())).first() or 0 for s, f in files), default=0)
    count = sum(len(sess.exec(select(Penalty.id).where(Penalty.server_id == s, Penalty.filename == f)).all()) for s, f in files)
    mtimes = tuple(result_path(s, f).stat().st_mtime if result_path(s, f).is_file() else None for s, f in files)
    roster = tuple(sorted(sess.exec(select(LeagueMember.guid).where(LeagueMember.championship_id == c.id, LeagueMember.non_racing)).all()))
    return tuple(e.id for e in events), newest, count, mtimes, c.name, tuple(effective(c).scoring.table), roster


def snapshot(sess: Session, c: Championship) -> dict:
    standings, races = score(sess, c)
    return {"version": VERSION, "name": c.name, "points_system": list(effective(c).scoring.table), "standings": standings, "races": races}


class ArchiveIn(BaseModel):
    confirm: str   # the exact name of the league: archiving is not undone from here


@router.get("/preview")
def preview(championship_id: int, sess: SessionDep) -> dict:
    c = _get(sess, championship_id)
    if c.archived_at:
        raise HTTPException(409, "league already archived")
    files = _own_files(sess, c)
    present = [(s, f) for s, f in files if result_path(s, f).is_file()]
    penalties = sum(len(sess.exec(select(Penalty.id).where(Penalty.server_id == s, Penalty.filename == f)).all()) for s, f in files)
    snap = snapshot(sess, c)
    return {"name": c.name, "races": len(snap["races"]), "files": len(present), "bytes": sum(result_path(s, f).stat().st_size for s, f in present),
            "penalty_rows": penalties, "standings": snap["standings"]}


@router.get("")
def read(championship_id: int, sess: SessionDep) -> dict:
    c = _get(sess, championship_id)
    arch = sess.get(ChampionshipArchive, championship_id) if c.archived_at else None
    if not arch:
        raise HTTPException(404, "league not archived")
    return {"archived_at": arch.archived_at, "archived_by": arch.archived_by, "data": arch.data}


@router.post("")
@serialized
def archive(championship_id: int, body: ArchiveIn, sess: SessionDep, user: CurrentUser) -> dict:
    c = get_open(sess, championship_id)   # 409 if it is already archived
    if body.confirm != c.name:
        raise HTTPException(422, "type the exact name of the league to confirm")
    before = _stamp(sess, c)
    data = snapshot(sess, c)   # reads the result files: done before any write lock is taken
    files = _own_files(sess, c)
    now = _now()
    claimed = sess.execute(update(Championship).where(Championship.id == championship_id, Championship.archived_at.is_(None)).values(archived_at=now, archived_by=user.username)).rowcount
    if claimed != 1:
        sess.rollback()
        raise HTTPException(409, "league already archived")
    if _stamp(sess, c) != before:   # a result or a penalty changed while the snapshot was being built
        sess.rollback()
        raise HTTPException(409, "the league changed while it was being archived: try again")
    files = _own_files(sess, c)   # again, now that this transaction holds the write lock: another league may have started counting one of the files meanwhile
    sess.add(ChampionshipArchive(championship_id=championship_id, archived_at=now, archived_by=user.username, data=data))
    for s, f in files:
        for p in sess.exec(select(Penalty).where(Penalty.server_id == s, Penalty.filename == f)):
            sess.delete(p)
        for x in (*sess.exec(select(Notice).where(Notice.server_id == s, Notice.filename == f)), *sess.exec(select(ResultClose).where(ResultClose.server_id == s, ResultClose.filename == f))):
            sess.delete(x)   # the warnings and the closing mark go with the result file
    for e in sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == championship_id)):
        sess.delete(e)
    sess.commit()
    deleted, failed = 0, []
    for s, f in files:   # after the commit: the archive no longer needs them; a failure leaves an unused file, never a half-archived league
        path = result_path(s, f)
        if sess.exec(select(ChampionshipEvent.id).where(ChampionshipEvent.server_id == s, ChampionshipEvent.filename == f)).first():
            continue   # another league has taken it up since: it stays
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError as e:
            log.error("could not delete %s of archived league %s: %s", path, championship_id, e)
            failed.append(f"{s}/{f}")
    log.info("league %s (%s) archived by %s: %d races, %d files deleted, %d not deleted", championship_id, c.name, user.username, len(data["races"]), deleted, len(failed))
    return {"archived_at": now, "archived_by": user.username, "races": len(data["races"]), "files_deleted": deleted, "files_not_deleted": failed}
