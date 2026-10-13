"""Championship engine: counts Race results into points standings.

A championship just points at a set of already-written result files
(one per counted race); standings are computed on read, not stored.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlmodel import select

from app.db import SessionDep
from app.leaguelock import serialized
from app.models import DEFAULT_POINTS_SYSTEM, Championship, ChampionshipArchive, ChampionshipEvent, LeagueMember, LeagueSuspension
from app.results import apply_penalties, non_racing_for, parse_result_file, penalties_for
from app.servers import result_path

router = APIRouter(prefix="/championships", tags=["championships"])


class PenaltyItem(BaseModel):
    """One entry of a league's penalty catalogue: what a steward can pick. Time is added to the race time; `dsq` disqualifies."""

    name: str = Field(min_length=1, max_length=40)
    seconds: int = Field(default=0, ge=0, le=3600)
    dsq: bool = False

    @model_validator(mode="after")
    def _does_something(self) -> PenaltyItem:
        if not self.dsq and not self.seconds:
            raise ValueError("a penalty needs seconds or dsq")
        return self


class ChampionshipIn(BaseModel):
    name: str
    points_system: list[int] = list(DEFAULT_POINTS_SYSTEM)
    practice_required: bool = False  # roster drivers need valid practice laps to be on the entry list (app/league.py)
    practice_laps: int = Field(default=5, ge=1, le=999)
    practice_days: int = Field(default=7, ge=1, le=60)
    penalties: list[PenaltyItem] = []


def _get(sess: SessionDep, championship_id: int) -> Championship:
    c = sess.get(Championship, championship_id)
    if not c:
        raise HTTPException(404, "championship not found")
    return c


def get_open(sess: SessionDep, championship_id: int) -> Championship:
    """The league for a CHANGE: an archived league (app/archive.py) is history and takes none."""
    c = _get(sess, championship_id)
    if c.archived_at:
        raise HTTPException(409, "league archived: it no longer takes changes")
    return c


@router.post("", response_model=Championship, status_code=201)
def create(body: ChampionshipIn, sess: SessionDep) -> Championship:
    c = Championship(**{**body.model_dump(), "penalties": [p.model_dump() for p in body.penalties]})
    sess.add(c)
    sess.commit()
    sess.refresh(c)
    return c


@router.get("", response_model=list[Championship])
def list_championships(sess: SessionDep) -> list[Championship]:
    return list(sess.exec(select(Championship)).all())


@router.get("/{championship_id}", response_model=Championship)
def get(championship_id: int, sess: SessionDep) -> Championship:
    return _get(sess, championship_id)


@router.patch("/{championship_id}", response_model=Championship)
@serialized
def update(championship_id: int, body: ChampionshipIn, sess: SessionDep) -> Championship:
    c = get_open(sess, championship_id)
    for k, v in body.model_dump(exclude_unset=True).items():   # only what was sent: an older client that sends name + points leaves the league rules alone
        setattr(c, k, [p.model_dump() for p in body.penalties] if k == "penalties" else v)
    sess.add(c)
    sess.commit()
    sess.refresh(c)
    return c


@router.delete("/{championship_id}", status_code=204)
@serialized
def delete(championship_id: int, sess: SessionDep) -> None:
    c = get_open(sess, championship_id)
    events = sess.exec(
        select(ChampionshipEvent).where(ChampionshipEvent.championship_id == championship_id)
    )
    for e in events:
        sess.delete(e)
    for m in sess.exec(select(LeagueMember).where(LeagueMember.championship_id == championship_id)):
        sess.delete(m)
    for x in sess.exec(select(LeagueSuspension).where(LeagueSuspension.championship_id == championship_id)):
        sess.delete(x)
    sess.flush()  # children out before the FK-checked parent delete
    sess.delete(c)
    sess.commit()


class EventIn(BaseModel):
    server_id: int
    filename: str


@router.post("/{championship_id}/events", response_model=ChampionshipEvent, status_code=201)
@serialized
def add_event(championship_id: int, body: EventIn, sess: SessionDep) -> ChampionshipEvent:
    get_open(sess, championship_id)
    if not result_path(body.server_id, body.filename).is_file():
        raise HTTPException(404, "result not found")
    e = ChampionshipEvent(
        championship_id=championship_id, server_id=body.server_id, filename=body.filename
    )
    sess.add(e)
    sess.commit()
    sess.refresh(e)
    return e


@router.delete("/{championship_id}/events/{event_id}", status_code=204)
@serialized
def remove_event(championship_id: int, event_id: int, sess: SessionDep) -> None:
    get_open(sess, championship_id)
    e = sess.get(ChampionshipEvent, event_id)
    if not e or e.championship_id != championship_id:
        raise HTTPException(404, "event not found")
    sess.delete(e)
    sess.commit()


@router.get("/{championship_id}/events", response_model=list[ChampionshipEvent])
def list_events(championship_id: int, sess: SessionDep) -> list[ChampionshipEvent]:
    c = _get(sess, championship_id)
    if c.archived_at:   # the result files are gone: the counted races are listed from the archive (negative ids: they are not rows any more)
        arch = sess.get(ChampionshipArchive, championship_id)
        return [ChampionshipEvent(id=-(i + 1), championship_id=championship_id, server_id=r["server_id"], filename=r["filename"], session_type=r["session_type"],
                                  created_at=c.archived_at) for i, r in enumerate(arch.data.get("races", []) if arch else [])]
    return list(
        sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == championship_id))
    )


def score(sess, c: Championship) -> tuple[list[dict], list[dict]]:
    """(standings, races) of a running league, computed from its result files and the stewards' penalties. `standings` is the ranked table; `races` is, for every
    counted result in the order they were added, a summary the archive keeps (app/archive.py): the classification after penalties with the points each driver
    got, and the penalties with their reason. No laps and no cuts."""
    events = sess.exec(
        select(ChampionshipEvent).where(ChampionshipEvent.championship_id == c.id).order_by(ChampionshipEvent.id)
    ).all()

    totals: dict[str, dict] = {}
    races: list[dict] = []
    for e in events:
        p = result_path(e.server_id, e.filename)
        if not p.is_file():
            continue
        nonrun = non_racing_for(sess, e.server_id, e.filename)
        pens = penalties_for(sess, e.server_id, e.filename)
        parsed = apply_penalties(parse_result_file(p), pens, nonrun)
        scoring = parsed["type"] == "Race"   # ponytail: only Race sessions score; add a per-event flag if a league wants qualy points
        rows = []
        for entry in parsed["classification"]:
            guid = entry["driver_guid"]
            pos = entry["position"]  # after penalties; None = disqualified, scores nothing
            counts = scoring and guid and guid not in nonrun   # the safety car, the race director and the caster do not score
            points = c.points_system[pos - 1] if counts and pos and pos <= len(c.points_system) else 0
            rows.append({"position": pos, "original_position": entry["original_position"], "driver_guid": guid, "driver_name": entry["driver_name"], "car_model": entry["car_model"],
                         "best_lap_ms": entry["best_lap_ms"], "total_time_ms": entry["total_time_ms"], "adjusted_total_ms": entry["adjusted_total_ms"], "gap_ms": entry["gap_ms"],
                         "laps": entry["laps"], "disqualified": entry["disqualified"], "points": points, "points_penalty": entry["points_penalty"] if counts else 0})
            if not counts:
                continue
            row = totals.setdefault(
                guid, {"driver_guid": guid, "driver_name": entry["driver_name"], "points": 0, "wins": 0, "podiums": 0, "races": 0, "penalty_points": 0}
            )
            row["driver_name"] = entry["driver_name"]
            row["points"] += points - entry["points_penalty"]
            row["penalty_points"] += entry["points_penalty"]
            row["wins"] += pos == 1
            row["podiums"] += bool(pos and pos <= 3)
            row["races"] += 1
        races.append({"filename": e.filename, "server_id": e.server_id, "session_type": e.session_type or parsed["type"], "type": parsed["type"], "name": parsed["name"],
                      "track": parsed["track"], "track_config": parsed["track_config"], "classification": rows,
                      "penalties": [{"driver_guid": x.driver_guid, "kind": x.kind, "value": x.value, "reason": x.reason, "created_by": x.created_by} for x in pens]})

    ranked = sorted(totals.values(), key=lambda r: (-r["points"], -r["wins"]))
    for i, row in enumerate(ranked):
        row["position"] = i + 1
    return ranked, races


@router.get("/{championship_id}/races")
def races(championship_id: int, sess: SessionDep) -> list[dict]:
    """Every counted result with its classification after the stewards' penalties (position, original_position, adjusted time, points) and the penalties
    with their reason: what the public site shows. An archived league answers from its snapshot. Who penalised is not given out."""
    c = _get(sess, championship_id)
    arch = sess.get(ChampionshipArchive, championship_id) if c.archived_at else None
    out = (arch.data.get("races", []) if arch else []) if c.archived_at else score(sess, c)[1]
    return [{**r, "penalties": [{k: v for k, v in p.items() if k != "created_by"} for p in r.get("penalties", [])]} for r in out]


@router.get("/{championship_id}/standings")
def standings(championship_id: int, sess: SessionDep) -> list[dict]:
    c = _get(sess, championship_id)
    if c.archived_at:   # history: the table as it was when the league ended
        arch = sess.get(ChampionshipArchive, championship_id)
        return arch.data.get("standings", []) if arch else []
    return score(sess, c)[0]
