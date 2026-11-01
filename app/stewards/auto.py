"""Warnings for the race director from a counted Race's result file (no live incidents involved: acServer already writes `Cuts` for every lap).

`cut_seconds` is the rule (a rulebook's `detectors.cuts`, app/rulebook.py); `cut_drafts` is the pure calculation over a parsed result, which the dry-run (calibration) also uses;
`cut_notices` writes a `Notice` per driver who reached it, when the league counts the file. It never penalises: the steward confirms the notice in the Sancionar panel (it
becomes a time `Penalty`) or dismisses it, and closing the result deletes what is left (app/penalties.py). One notice per (file, driver, reason) -- the unique constraint --
so counting the file in another league does not duplicate it. Only cuts for now: contacts and track limits are still evidence (see README.md).
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import Championship, GiveBackDue, Notice, Penalty, ResultClose
from app.results import non_racing_for
from app.rulebook import CutRule, effective


def cut_seconds(cuts: int, rule: CutRule) -> int:
    """Seconds for `cuts` cuts: the first `tolerance` are free, then `step_seconds` per full `cuts_per_step`, capped at `max_seconds`."""
    return min(rule.max_seconds, max(0, cuts - rule.tolerance) // rule.cuts_per_step * rule.step_seconds)


def cut_drafts(parsed: dict, rule: CutRule, racing: set[str]) -> list[dict]:
    """[{driver_guid, driver_name, cuts, seconds}] for the drivers of this Race who reach the rule. Pure: nothing is read or written."""
    if parsed["type"] != "Race":
        return []
    cuts: dict[str, int] = {}
    for lap in parsed["laps"]:
        if lap["driver_guid"] and (lap["lap_time_ms"] or 0) > 0:   # a lap with no time is an unfinished one
            cuts[lap["driver_guid"]] = cuts.get(lap["driver_guid"], 0) + (lap["cuts"] or 0)
    names = {e["driver_guid"]: e["driver_name"] for e in parsed["classification"]}
    return [{"driver_guid": g, "driver_name": names.get(g) or g, "cuts": n, "seconds": s} for g, n in cuts.items() if g in racing and (s := cut_seconds(n, rule))]


def give_back_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """The time penalties of the advantage gained off the track (app/live/giveback.py): every `GiveBackDue` of this server that fell due while this schedule ran (`since`, its start, up to the end of this Race) and is
    still due becomes a time `Penalty` on this result, once (`auto_key` = giveback:<id>), with the seconds the rule said when it fell due. A due of a driver who is not in the result is `expired`: that race is over,
    it must not wait for the next one. Does not commit; returns how many penalties were made."""
    if parsed["type"] != "Race" or not effective(c).detectors.give_back.enabled or sess.get(ResultClose, (server_id, filename)):
        return 0
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for due in sess.exec(select(GiveBackDue).where(GiveBackDue.server_id == server_id, GiveBackDue.state == "due", GiveBackDue.ts >= since, GiveBackDue.ts <= finished_at).order_by(GiveBackDue.ts)).all():
        if due.driver_guid not in racing:
            due.state = "expired"
            sess.add(due)
            continue
        key = f"giveback:{due.id}"
        if not sess.exec(select(Penalty.id).where(Penalty.server_id == server_id, Penalty.filename == filename, Penalty.driver_guid == due.driver_guid, Penalty.auto_key == key)).first():
            sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=due.driver_guid, kind="time", value=due.seconds * 1000, created_by="auto", auto_key=key,
                             reason=f"Ventaja fuera de pista, no devolvió la posición{f' ({due.creditors})' if due.creditors else ''}"))
            made += 1
        due.state = "applied"
        sess.add(due)
    return made


def cut_notices(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict) -> int:
    """Add the cut warnings of this Race result under the league's rulebook; returns how many were created. Does not commit (the caller counts the file in the same transaction)."""
    rule = effective(c).detectors.cuts
    if parsed["type"] != "Race" or not rule or sess.get(ResultClose, (server_id, filename)):
        return 0
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for d in cut_drafts(parsed, rule, racing):
        if sess.exec(select(Notice.id).where(Notice.server_id == server_id, Notice.filename == filename, Notice.driver_guid == d["driver_guid"], Notice.kind == "cuts")).first():
            continue
        sess.add(Notice(server_id=server_id, filename=filename, driver_guid=d["driver_guid"], kind="cuts", penalty_kind="time", value=d["seconds"] * 1000, reason=f"Cortes de pista: {d['cuts']}"))
        made += 1
    return made
