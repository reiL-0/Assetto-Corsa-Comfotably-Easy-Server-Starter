"""The reference laps of the VSC, kept in the database: ONE row per (track, layout, car model): the window of its last clean laps and the reference chosen from it, the quickest lap that is no outlier (app/models.py `VscRef`).

The live learning is app/live/sectors.py `RefLaps` (memory, per session); this keeps it between sessions, days and restarts, so a VSC in a race knows the laps of earlier practice and qualifying too.
Blocking database work: `sync` runs in a worker thread (the monitor), never on the event loop."""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app import csp
from app.db import engine
from app.live.sectors import KEEP_LAPS, reference
from app.models import VscRef

log = logging.getLogger("acm.vscrefs")


def load(track: str, config: str) -> dict[str, list[tuple[float, list[float]]]]:
    """The window of recent laps of every car model on this layout (a row from before the windows: its reference lap alone)."""
    with Session(engine) as s:
        rows = s.exec(select(VscRef).where(VscRef.track == track, VscRef.track_config == config)).all()
    return {r.car_model: [(float(lap), [float(x) for x in secs]) for lap, secs in r.recent_laps] or [(r.lap_s, [float(x) for x in r.sectors])] for r in rows}


def save(track: str, config: str, fresh: dict[str, list[tuple[float, list[float]]]]) -> None:
    """Add the laps learned since the last save to the stored window of each car model (the last KEEP_LAPS) and choose the reference again: the quickest lap that is no outlier. One row per combination."""
    with Session(engine) as s:
        for model, laps in fresh.items():
            row = s.get(VscRef, (track, config, model))
            if row is None:
                row = VscRef(track=track, track_config=config, car_model=model, lap_s=0.0)
            old = [(float(lap), list(secs)) for lap, secs in row.recent_laps] or ([(row.lap_s, list(row.sectors))] if row.lap_s else [])
            window = (old + [(lap, list(secs)) for lap, secs in laps])[-KEEP_LAPS:]
            ref = reference(window)
            if ref:
                row.lap_s, row.sectors, row.recent_laps = ref[0], list(ref[1]), [[lap, list(secs)] for lap, secs in window]
                s.add(row)
        s.commit()


def key_of(session: dict | None) -> tuple[str, str] | None:
    """(track, layout) of a session as the table keeps them; None when the session does not say."""
    track = csp.clean_track((session or {}).get("track") or "")
    return (track, (session or {}).get("track_config") or "") if track else None
