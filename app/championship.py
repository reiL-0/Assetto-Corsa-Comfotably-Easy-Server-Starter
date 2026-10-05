"""Championship engine: counts Race results into points standings.

A championship just points at a set of already-written result files
(one per counted race); standings are computed on read, not stored.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlmodel import select

from app.db import SessionDep
from app.models import DEFAULT_POINTS_SYSTEM, Championship, ChampionshipEvent, LeagueMember
from app.results import apply_penalties, parse_result_file, penalties_for
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
def update(championship_id: int, body: ChampionshipIn, sess: SessionDep) -> Championship:
    c = _get(sess, championship_id)
    for k, v in body.model_dump(exclude_unset=True).items():   # only what was sent: an older client that sends name + points leaves the league rules alone
        setattr(c, k, [p.model_dump() for p in body.penalties] if k == "penalties" else v)
    sess.add(c)
    sess.commit()
    sess.refresh(c)
    return c


@router.delete("/{championship_id}", status_code=204)
def delete(championship_id: int, sess: SessionDep) -> None:
    c = _get(sess, championship_id)
    events = sess.exec(
        select(ChampionshipEvent).where(ChampionshipEvent.championship_id == championship_id)
    )
    for e in events:
        sess.delete(e)
    for m in sess.exec(select(LeagueMember).where(LeagueMember.championship_id == championship_id)):
        sess.delete(m)
    sess.flush()  # children out before the FK-checked parent delete
    sess.delete(c)
    sess.commit()


class EventIn(BaseModel):
    server_id: int
    filename: str


@router.post("/{championship_id}/events", response_model=ChampionshipEvent, status_code=201)
def add_event(championship_id: int, body: EventIn, sess: SessionDep) -> ChampionshipEvent:
    _get(sess, championship_id)
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
def remove_event(championship_id: int, event_id: int, sess: SessionDep) -> None:
    e = sess.get(ChampionshipEvent, event_id)
    if not e or e.championship_id != championship_id:
        raise HTTPException(404, "event not found")
    sess.delete(e)
    sess.commit()


@router.get("/{championship_id}/events", response_model=list[ChampionshipEvent])
def list_events(championship_id: int, sess: SessionDep) -> list[ChampionshipEvent]:
    _get(sess, championship_id)
    return list(
        sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == championship_id))
    )


@router.get("/{championship_id}/standings")
def standings(championship_id: int, sess: SessionDep) -> list[dict]:
    c = _get(sess, championship_id)
    events = sess.exec(
        select(ChampionshipEvent).where(ChampionshipEvent.championship_id == championship_id)
    ).all()

    totals: dict[str, dict] = {}
    for e in events:
        p = result_path(e.server_id, e.filename)
        if not p.is_file():
            continue
        parsed = apply_penalties(parse_result_file(p), penalties_for(sess, e.server_id, e.filename))
        if parsed["type"] != "Race":
            continue  # ponytail: only Race sessions score; add a per-event flag if a league wants qualy points
        for entry in parsed["classification"]:
            guid = entry["driver_guid"]
            if not guid:
                continue
            pos = entry["position"]  # after penalties; None = disqualified, scores nothing
            points = c.points_system[pos - 1] if pos and pos <= len(c.points_system) else 0
            row = totals.setdefault(
                guid, {"driver_guid": guid, "driver_name": entry["driver_name"], "points": 0, "wins": 0, "penalty_points": 0}
            )
            row["driver_name"] = entry["driver_name"]
            row["points"] += points - entry["points_penalty"]
            row["penalty_points"] += entry["points_penalty"]
            row["wins"] += pos == 1

    ranked = sorted(totals.values(), key=lambda r: (-r["points"], -r["wins"]))
    for i, row in enumerate(ranked):
        row["position"] = i + 1
    return ranked
