"""Championship engine: counts Race results into points standings.

A championship just points at a set of already-written result files
(one per counted race); standings are computed on read, not stored.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import select

from app import content, requirements
from app.db import SessionDep
from app.leaguelock import serialized
from app.models import DEFAULT_POINTS_SYSTEM, Championship, ChampionshipArchive, ChampionshipEvent, LeagueMember, LeagueSuspension
from app.results import apply_penalties, non_racing_for, parse_result_file, penalties_for
from app.rulebook import PRESETS, Rulebook, effective
from app.schemas.servers import _folder_name, _setup_name
from app.stewards.auto import cut_drafts
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


class AutoRules(BaseModel):
    """Automatic time penalty for track cuts in a counted Race (app/stewards/auto.py): every `cuts_per_step` cuts add `step_seconds`, up to `max_seconds`."""

    cuts_per_step: int = Field(ge=1, le=100)
    step_seconds: int = Field(ge=1, le=60)
    max_seconds: int = Field(ge=1, le=600)


class ChampionshipIn(BaseModel):
    name: str
    points_system: list[int] = list(DEFAULT_POINTS_SYSTEM)
    practice_required: bool = False  # roster drivers need valid practice laps to be on the entry list (app/league.py)
    practice_laps: int = Field(default=5, ge=1, le=999)
    practice_days: int = Field(default=7, ge=1, le=60)
    penalties: list[PenaltyItem] = []
    auto_rules: AutoRules | None = None  # None = no automatic penalties
    car_setups: dict[str, str] | None = None  # {car model: uploaded setup name}: the league's setup rule per car (the roster can override it per driver)

    @field_validator("car_setups")
    @classmethod
    def _setups_ok(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        for car, name in (v or {}).items():
            _folder_name(car)
            if not car or not name:
                raise ValueError("car_setups: a car and a setup name for each")
            _setup_name(name)
        return v


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
    c = Championship(**{**body.model_dump(), "penalties": [p.model_dump() for p in body.penalties], "car_setups": body.car_setups or {}})
    missing = sorted({n for n in c.car_setups.values() if not (content.setups_dir() / f"{n}.ini").is_file()})
    if missing:
        raise HTTPException(400, f"setups not uploaded: {', '.join(missing)}")
    sess.add(c)
    sess.commit()
    sess.refresh(c)
    return c


@router.get("", response_model=list[Championship])
def list_championships(sess: SessionDep) -> list[Championship]:
    return list(sess.exec(select(Championship)).all())


@router.get("/rulebook/presets")
def rulebook_presets() -> dict[str, Rulebook]:
    """Templates to copy into a league (they are not kept in sync with it)."""
    return PRESETS


@router.get("/{championship_id}", response_model=Championship)
def get(championship_id: int, sess: SessionDep) -> Championship:
    return _get(sess, championship_id)


@router.patch("/{championship_id}", response_model=Championship)
@serialized
def update(championship_id: int, body: ChampionshipIn, sess: SessionDep) -> Championship:
    c = get_open(sess, championship_id)
    sent = body.model_dump(exclude_unset=True)
    if c.rulebook and {"points_system", "penalties", "auto_rules"} & sent.keys():
        raise HTTPException(409, "this league has a rulebook: its points, catalogue and detectors are edited there (PUT /championships/{id}/rulebook)")
    missing = sorted({n for n in (sent.get("car_setups") or {}).values() if not (content.setups_dir() / f"{n}.ini").is_file()})
    if missing:
        raise HTTPException(400, f"setups not uploaded: {', '.join(missing)}")
    if "car_setups" in sent and sent["car_setups"] is None:
        sent["car_setups"] = {}
    for k, v in sent.items():   # only what was sent: an older client that sends name + points leaves the league rules alone
        setattr(c, k, [p.model_dump() for p in body.penalties] if k == "penalties" else v)
    sess.add(c)
    sess.commit()
    sess.refresh(c)
    return c


@router.get("/{championship_id}/requirements")
def get_requirements(championship_id: int, sess: SessionDep) -> dict:
    """What players must install for this league: its own list when it has one (`own`), else the global one."""
    c = _get(sess, championship_id)
    return {"own": c.requirements is not None, **requirements.current(c.id)}


@router.put("/{championship_id}/requirements")
@serialized
def put_requirements(championship_id: int, body: requirements.RequirementsIn, sess: SessionDep) -> dict:
    c = get_open(sess, championship_id)
    c.requirements = body.model_dump()
    sess.add(c)
    sess.commit()
    return {"own": True, **requirements.current(c.id)}


@router.delete("/{championship_id}/requirements")
@serialized
def reset_requirements(championship_id: int, sess: SessionDep) -> dict:
    """Back to the global list."""
    c = get_open(sess, championship_id)
    c.requirements = None
    sess.add(c)
    sess.commit()
    return {"own": False, **requirements.current(c.id)}


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

    table = effective(c).scoring.table
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
            points = table[pos - 1] if counts and pos and pos <= len(table) else 0
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


# --- the rulebook (app/rulebook.py) ---

@router.get("/{championship_id}/rulebook")
def get_rulebook(championship_id: int, sess: SessionDep) -> dict:
    c = _get(sess, championship_id)
    return {"source": "rulebook" if c.rulebook else "legacy", "rulebook": effective(c)}


@router.put("/{championship_id}/rulebook", response_model=Rulebook)
@serialized
def put_rulebook(championship_id: int, body: Rulebook, sess: SessionDep) -> Rulebook:
    c = get_open(sess, championship_id)
    c.rulebook = body.model_dump()
    sess.add(c)
    sess.commit()
    return body


class DryRunIn(BaseModel):
    server_id: int
    filename: str
    rulebook: Rulebook | None = None   # try one that is not saved; omitted = the league's own


# What the rules say that no result file can show: said out loud so nobody reads "no warnings" as "no infractions".
NOT_EVALUATED = ["contactos y de quién es la culpa", "ventaja obtenida en un exceso de límites", "devolver tiempo o posición", "velocidad y línea de boxes", "banderas"]


@router.post("/{championship_id}/rulebook/dry-run")
def dry_run(championship_id: int, body: DryRunIn, sess: SessionDep) -> dict:
    """Calibration: what a rulebook WOULD warn about in a result that already exists. Reads the result and the league; writes nothing."""
    c = _get(sess, championship_id)
    path = result_path(body.server_id, body.filename)
    if not path.is_file():
        raise HTTPException(404, "result not found")
    rb = body.rulebook or effective(c)
    parsed = parse_result_file(path)
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, body.server_id, body.filename)
    return {"type": parsed["type"], "cuts_rule": rb.detectors.cuts, "notices": cut_drafts(parsed, rb.detectors.cuts, racing) if rb.detectors.cuts else [], "not_evaluated": NOT_EVALUATED}
