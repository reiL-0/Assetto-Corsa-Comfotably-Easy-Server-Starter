"""The practice survives an idle stop: acServer is killed after ACM_IDLE_STOP_SECONDS without cars and its table and its track grip go with it.

On every practice lap the best laps of the board and the grip (ACSP `grip_level`, 0..1) are saved, one row per server. When the server wakes up the
board starts with those drivers (disconnected, with their best lap: the panel lists them like any driver who left) and `DYNAMIC_TRACK.SESSION_START`
is the saved grip. The row goes when a session of another kind begins (qualify / race) or when a new set-up is applied. acServer cannot be loaded with
times, so its own in-game table starts empty.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.db import engine
from app.live.board import NO_TIME, Driver, LiveBoard
from app.models import PracticeKeep

log = logging.getLogger("acm.practicekeep")
_last: dict[int, tuple] = {}   # server id -> what was written last: a lap that changes nothing writes nothing


def snapshot(board: LiveBoard) -> list[dict]:
    return [{"guid": d.guid, "name": d.name, "model": d.model, "skin": d.skin, "best_ms": d.best_ms, "best_valid_ms": d.best_valid_ms,
             "laps": d.laps, "valid_laps": d.valid_laps}
            for d in board.drivers if d.guid and 0 < d.best_ms < NO_TIME]


def seed(board: LiveBoard, rows: list[dict]) -> None:
    """Put the kept drivers on a fresh board, disconnected; they take their place back when they join (same Steam ID)."""
    valid = [r for r in rows if 0 < r.get("best_ms", 0) < NO_TIME]
    board.keep_next = bool(valid)
    for r in valid:
        board.drivers.append(Driver(car_id=-1, name=r["name"], guid=r["guid"], model=r["model"], skin=r["skin"], connected=False, best_ms=r["best_ms"],
                                    kept_ms=r["best_ms"], best_valid_ms=r.get("best_valid_ms") or 0, laps=r.get("laps", 0), valid_laps=r.get("valid_laps")))


def save(server_id: int, board: LiveBoard, grip: float | None) -> None:
    rows = snapshot(board)
    key = (grip, tuple((r["guid"], r["best_ms"], r["laps"]) for r in rows))
    if _last.get(server_id) == key:
        return
    try:
        with Session(engine) as s:
            s.merge(PracticeKeep(server_id=server_id, grip=grip, drivers=rows))
            s.commit()
    except Exception:   # a lost save must never cost the lap that triggered it its other handlers
        log.exception("could not keep the practice of server %s", server_id)
        return
    _last[server_id] = key


def load(server_id: int) -> PracticeKeep | None:
    with Session(engine) as s:
        return s.get(PracticeKeep, server_id)


def clear(server_id: int) -> None:
    _last.pop(server_id, None)
    with Session(engine) as s:
        row = s.get(PracticeKeep, server_id)
        if row:
            s.delete(row)
            s.commit()


def with_grip(config: dict, grip: float | None) -> dict:
    """`config` as acServer must read it to start where the track was left: SESSION_START = the kept grip (only if the server has a dynamic track and a practice)."""
    if grip is None or "DYNAMIC_TRACK" not in config or "PRACTICE" not in config:
        return config
    return {**config, "DYNAMIC_TRACK": {**config["DYNAMIC_TRACK"], "SESSION_START": max(0, min(100, round(grip * 100)))}}
