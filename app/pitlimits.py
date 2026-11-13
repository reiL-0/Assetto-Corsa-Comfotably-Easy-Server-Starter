"""The pit-lane speed limit per track layout (app/models.py `PitLimit`): what the director set for the track, the rule the pit-speed reports of the hubs are judged by.
Blocking database work: called from request threads, never from the event loop."""
from __future__ import annotations

from sqlmodel import Session

from app.db import engine
from app.models import PitLimit


def get(track: str, config: str) -> dict | None:
    with Session(engine) as s:
        row = s.get(PitLimit, (track, config))
        return {"limit_kmh": row.limit_kmh, "tolerance_kmh": row.tolerance_kmh, "penalty_s": row.penalty_s} if row and row.limit_kmh > 0 else None


def put(track: str, config: str, limit_kmh: int, tolerance_kmh: int, penalty_s: int) -> None:
    """limit 0 removes the rule of the track."""
    with Session(engine) as s:
        row = s.get(PitLimit, (track, config))
        if limit_kmh <= 0:
            if row:
                s.delete(row)
        elif row:
            row.limit_kmh, row.tolerance_kmh, row.penalty_s = limit_kmh, tolerance_kmh, penalty_s
            s.add(row)
        else:
            s.add(PitLimit(track=track, track_config=config, limit_kmh=limit_kmh, tolerance_kmh=tolerance_kmh, penalty_s=penalty_s))
        s.commit()
