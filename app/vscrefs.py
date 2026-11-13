"""The reference laps of the VSC, kept in the database: ONE row per (track, layout, car model), the fastest clean lap ever seen (app/models.py `VscRef`).

The live learning is app/live/sectors.py `RefLaps` (memory, per session); this keeps it between sessions, days and restarts, so a VSC in a race knows the laps of earlier practice and qualifying too.
Blocking database work: `sync` runs in a worker thread (the monitor), never on the event loop."""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app import csp
from app.db import engine
from app.models import VscRef

log = logging.getLogger("acm.vscrefs")


def load(track: str, config: str) -> dict[str, tuple[float, list[float]]]:
    with Session(engine) as s:
        rows = s.exec(select(VscRef).where(VscRef.track == track, VscRef.track_config == config)).all()
    return {r.car_model: (r.lap_s, [float(x) for x in r.sectors]) for r in rows}


def save(track: str, config: str, items: dict[str, tuple[float, list[float]]]) -> None:
    """Keep each lap only if it beats the one stored for that car model on that layout."""
    with Session(engine) as s:
        for model, (lap, secs) in items.items():
            row = s.get(VscRef, (track, config, model))
            if row is None:
                s.add(VscRef(track=track, track_config=config, car_model=model, lap_s=lap, sectors=list(secs)))
            elif lap < row.lap_s:
                row.lap_s, row.sectors = lap, list(secs)
        s.commit()


def key_of(session: dict | None) -> tuple[str, str] | None:
    """(track, layout) of a session as the table keeps them; None when the session does not say."""
    track = csp.clean_track((session or {}).get("track") or "")
    return (track, (session or {}).get("track_config") or "") if track else None
