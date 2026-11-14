"""The best valid laps ever driven on a track with a car, kept apart from the live board: a panel on the site shows them (stopped server, or a track nobody
has driven yet) until someone sets a real time. One row per (track, layout, car, driver); a lap with cuts, without a Steam ID or without a real time is never kept."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlmodel import Session

from app import csp
from app.db import engine
from app.live.board import NO_TIME

log = logging.getLogger("acm.records")
_best: dict[tuple, int] = {}   # (track, layout, car, guid) -> best written by this process: a lap that does not beat it never touches the database
TOP = 10

_UPSERT = text("""INSERT INTO track_records (track, track_config, car_model, guid, name, best_ms, set_at, s1_ms, s2_ms, s3_ms)
                  VALUES (:t, :c, :m, :g, :n, :ms, CURRENT_TIMESTAMP, :s1, :s2, :s3)
                  ON CONFLICT (track, track_config, car_model, guid)
                  DO UPDATE SET best_ms = excluded.best_ms, name = excluded.name, set_at = excluded.set_at,
                                s1_ms = excluded.s1_ms, s2_ms = excluded.s2_ms, s3_ms = excluded.s3_ms WHERE excluded.best_ms < track_records.best_ms""")


def submit(track: str | None, config: str | None, car: str, guid: str, name: str, ms: int, cuts: int = 0, splits: list[int] | None = None) -> bool:
    """Keep the lap if it is valid and beats this driver's record; True when it was written. Never raises: a lost record must not cost the lap its other handlers.
    `splits` are the three sectors of THIS lap; they go with the time (a record without them stores NULL, never the sectors of an older lap)."""
    track = csp.clean_track(track or "")
    config = config or ""
    if cuts or not (track and car and guid and 0 < ms < NO_TIME):
        return False
    key = (track, config, car, guid)
    s = list(splits) if splits and len(splits) == 3 and all(isinstance(x, int) and x > 0 for x in splits) else [None] * 3
    if (_best.get(key) or NO_TIME) <= ms:
        return False
    try:
        with Session(engine) as db:
            written = db.execute(_UPSERT, {"t": track, "c": config, "m": car, "g": guid, "n": name, "ms": ms, "s1": s[0], "s2": s[1], "s3": s[2]}).rowcount > 0   # (only writes if it beats what is stored: other processes, old laps)
            db.commit()
    except Exception:
        log.exception("could not keep the record of %s on %s", guid, track)
        return False
    _best[key] = ms   # (written or not, a lap this slow will not write either)
    return written


def top(track: str, config: str, n: int = TOP) -> list[dict]:
    """The `n` quickest drivers of each car on this layout, quickest first (ties: the one who set it first)."""
    with Session(engine) as s:
        rows = s.execute(text("""SELECT car_model, guid, name, best_ms, set_at, s1_ms, s2_ms, s3_ms FROM track_records WHERE track = :t AND track_config = :c
                                 ORDER BY car_model, best_ms, set_at"""), {"t": csp.clean_track(track), "c": config or ""}).all()
    out: dict[str, list[dict]] = {}
    for car, guid, name, ms, at, *sec in rows:
        if len(out.setdefault(car, [])) < n:
            out[car].append({"guid": guid, "name": name or "—", "best_ms": ms, "set_at": str(at), "splits": sec if all(sec) else None})
    return [{"car_model": c, "laps": v} for c, v in out.items()]
