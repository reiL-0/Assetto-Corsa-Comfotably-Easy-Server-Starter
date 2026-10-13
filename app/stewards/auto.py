"""Warnings for the race director from a counted Race's result file (no live incidents involved: acServer already writes `Cuts` for every lap).

`cut_seconds` is the rule; `cut_notices` writes a `Notice` per driver who reached it, when the league counts the file. It never penalises: the steward confirms the
notice in the Sancionar panel (it becomes a time `Penalty`) or dismisses it, and closing the result deletes what is left (app/penalties.py). One notice per
(file, driver, reason) -- the unique constraint -- so counting the file in another league does not duplicate it. Only cuts for now: contacts and track limits
are still evidence (see README.md).
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import Championship, Notice, ResultClose
from app.results import non_racing_for


def cut_seconds(cuts: int, rules: dict) -> int:
    """Seconds for `cuts` cuts under a league's rules: 0 until `cuts_per_step` is reached, then `step_seconds` per full step, capped at `max_seconds`."""
    return min(rules["max_seconds"], cuts // rules["cuts_per_step"] * rules["step_seconds"])


def cut_notices(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict) -> int:
    """Add the cut warnings of this Race result under the league's `auto_rules`; returns how many were created. Does not commit (the caller counts the file in the same transaction)."""
    if parsed["type"] != "Race" or not c.auto_rules or sess.get(ResultClose, (server_id, filename)):
        return 0
    cuts: dict[str, int] = {}
    for lap in parsed["laps"]:
        if lap["driver_guid"] and (lap["lap_time_ms"] or 0) > 0:   # a lap with no time is an unfinished one
            cuts[lap["driver_guid"]] = cuts.get(lap["driver_guid"], 0) + (lap["cuts"] or 0)
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for guid, n in cuts.items():
        seconds = cut_seconds(n, c.auto_rules)
        if not seconds or guid not in racing:
            continue
        if sess.exec(select(Notice.id).where(Notice.server_id == server_id, Notice.filename == filename, Notice.driver_guid == guid, Notice.kind == "cuts")).first():
            continue
        sess.add(Notice(server_id=server_id, filename=filename, driver_guid=guid, kind="cuts", penalty_kind="time", value=seconds * 1000, reason=f"Cortes de pista: {n}"))
        made += 1
    return made
