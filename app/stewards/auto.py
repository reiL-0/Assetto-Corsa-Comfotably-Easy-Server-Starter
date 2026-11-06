"""Warnings for the race director from a counted Race's result file (no live incidents involved: acServer already writes `Cuts` for every lap).

`cut_seconds` is the rule (a rulebook's `detectors.cuts`, app/rulebook.py); `cut_drafts` is the pure calculation over a parsed result, which the dry-run (calibration) also uses;
`cut_notices` writes a `Notice` per driver who reached it, when the league counts the file. It never penalises: the steward confirms the notice in the Sancionar panel (it
becomes a time `Penalty`) or dismisses it, and closing the result deletes what is left (app/penalties.py). One notice per (file, driver, reason) -- the unique constraint --
so counting the file in another league does not duplicate it. Automatic time proposals and give-back sanctions use the applied neutralization cap; the reason records any reduction.
Contacts and track limits are still evidence (see README.md).
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.live.raceaudit import record
from app.models import Championship, CutDue, GiveBackDue, Notice, Penalty, ResultClose, Server
from app.results import non_racing_for
from app.rulebook import CutRule, Rulebook, effective


def _applied_rules(sess: Session, c: Championship, server_id: int) -> Rulebook:
    """Use the applied session snapshot, including when the league has since been edited."""
    server = sess.get(Server, server_id)
    applied = (server.session or {}) if server else {}
    snapshot = applied.get("rulebook_snapshot")
    # ponytail: Server.session stores no application timestamp or result binding.
    # Same-league results from an older event cannot be distinguished here; only
    # league identity can currently be verified, not temporal compatibility.
    return (Rulebook.model_validate(snapshot)
            if snapshot and c.id is not None and applied.get("rulebook_league_id") == c.id
            else effective(c))


def _audit_proposal(sess, server_id, kind, rb, **fields):
    server = sess.get(Server, server_id)
    applied = (server.session or {}) if server else {}
    fields.update(session={k: applied.get(k) for k in ("session_type", "name")},
                  rulebook_revision=applied.get("rulebook_revision"), mode=rb.neutralization.mode)
    record(server_id, kind + "_staged", **fields, executed=False, persistence="pending_commit")
    if "race_audit_proposals" in sess.info:
        sess.info["race_audit_proposals"].append((server_id, kind + "_created", fields))


def capped_auto_time(seconds: int, cap: int, reason: str) -> tuple[int, str]:
    """Time sanctions only; automatic callers retain the original decision in the record."""
    if seconds > cap:
        return cap, f"{reason} (recortada de {seconds} a {cap} s)"
    return seconds, reason


def cut_seconds(cuts: int, rule: CutRule) -> int:
    """Seconds for `cuts` cuts: the first `tolerance` are free, then `step_seconds` per full `cuts_per_step`, capped at `max_seconds`."""
    return min(rule.max_seconds, max(0, cuts - rule.tolerance) // rule.cuts_per_step * rule.step_seconds)


def cut_drafts(parsed: dict, rule: CutRule, racing: set[str]) -> list[dict]:
    """[{driver_guid, driver_name, cuts, seconds}] for the drivers of this Race who reach the rule. Pure: nothing is read or written."""
    if parsed["type"] != "Race" or rule.ladder is not None:
        return []
    cuts: dict[str, int] = {}
    for lap in parsed["laps"]:
        if lap["driver_guid"] and (lap["lap_time_ms"] or 0) > 0:   # a lap with no time is an unfinished one
            cuts[lap["driver_guid"]] = cuts.get(lap["driver_guid"], 0) + (lap["cuts"] or 0)
    names = {e["driver_guid"]: e["driver_name"] for e in parsed["classification"]}
    return [{"driver_guid": g, "driver_name": names.get(g) or g, "cuts": n, "seconds": s} for g, n in cuts.items() if g in racing and (s := cut_seconds(n, rule))]


def give_back_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """The time penalties of the advantage gained off the track (app/live/giveback.py): every `GiveBackDue` of this server that fell due while this schedule ran (`since`, its start, up to the end of this Race) and is
    still due becomes a time `Penalty` on this result, once (`auto_key` = giveback:<id>), with its original seconds limited by the applied cap when automatic; director sanctions retain their value and author. A due of a driver who is not in the result is `expired`: that race is over,
    it must not wait for the next one. Does not commit; returns how many penalties were made."""
    rb = _applied_rules(sess, c, server_id)
    if parsed["type"] != "Race" or not rb.detectors.give_back.enabled or sess.get(ResultClose, (server_id, filename)):
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
            seconds = due.seconds
            reason = f"Ventaja fuera de pista, no devolvió la posición{f' ({due.creditors})' if due.creditors else ''}"
            if due.created_by == "auto":
                seconds, reason = capped_auto_time(seconds, rb.neutralization.max_auto_time_penalty_s, reason)
            sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=due.driver_guid, kind="time", value=seconds * 1000,
                             created_by=due.created_by, auto_key=key, reason=reason))
            _audit_proposal(sess, server_id, "give_back_penalty", rb, driver_guid=due.driver_guid, filename=filename,
                   original_seconds=due.seconds, final_seconds=seconds, cap=rb.neutralization.max_auto_time_penalty_s,
                   source=due.created_by)
            made += 1
        due.state = "applied"
        sess.add(due)
    return made


def cut_notices(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict) -> int:
    """Add the cut warnings of this Race result under the league's rulebook; returns how many were created. Does not commit (the caller counts the file in the same transaction)."""
    rb = _applied_rules(sess, c, server_id)
    rule = rb.detectors.cuts
    if parsed["type"] != "Race" or not rule or sess.get(ResultClose, (server_id, filename)):
        return 0
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for d in cut_drafts(parsed, rule, racing):
        if sess.exec(select(Notice.id).where(Notice.server_id == server_id, Notice.filename == filename, Notice.driver_guid == d["driver_guid"], Notice.kind == "cuts")).first():
            continue
        seconds, reason = capped_auto_time(d["seconds"], rb.neutralization.max_auto_time_penalty_s, f"Cortes de pista: {d['cuts']}")
        sess.add(Notice(server_id=server_id, filename=filename, driver_guid=d["driver_guid"], kind="cuts", penalty_kind="time", value=seconds * 1000, reason=reason))
        _audit_proposal(sess, server_id, "cuts_notice", rb, driver_guid=d["driver_guid"], filename=filename,
               cuts=d["cuts"], original_seconds=d["seconds"], final_seconds=seconds,
               cap=rb.neutralization.max_auto_time_penalty_s)
        made += 1
    return made


def stage_cut_dues(sess: Session, rows: list[dict]) -> None:
    """Idempotent inserts; caller owns the transaction and acknowledges after commit."""
    from sqlalchemy.dialects.sqlite import insert
    for row in rows:
        sess.execute(insert(CutDue).values(**row, state="due").on_conflict_do_nothing(
            index_elements=["server_id", "session_key", "driver_guid", "round"]))


def cut_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """Convert live rounds using tolerance/ladder, capped per penalty at the applied
    rulebook's neutralization.max_auto_time_penalty_s (default 10 s, configurable
    in that same rulebook). The required legacy cut fields do not set these seconds.
    """
    rb = _applied_rules(sess, c, server_id)
    rule = rb.detectors.cuts
    if parsed["type"] != "Race" or not rule or rule.ladder is None or sess.get(ResultClose, (server_id, filename)):
        return 0
    # Include the last lap even if the monitor has not persisted its queue yet.
    from app.live.racecontrol import pending_cut_dues
    stage_cut_dues(sess, [r for r in pending_cut_dues() if r["server_id"] == server_id])
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for due in sess.exec(select(CutDue).where(CutDue.server_id == server_id, CutDue.state == "due", CutDue.ts >= since, CutDue.ts <= finished_at).order_by(CutDue.ts, CutDue.round)).all():
        if due.driver_guid not in racing:
            due.state = "expired"
        else:
            key = f"cuts:{due.id}"
            if not sess.exec(select(Penalty.id).where(Penalty.auto_key == key)).first():
                seconds, reason = capped_auto_time(due.seconds, rb.neutralization.max_auto_time_penalty_s, f"Cortes de pista: ronda {due.round}")
                sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=due.driver_guid, kind="time",
                                 value=seconds * 1000, auto_key=key, created_by="auto", reason=reason))
                _audit_proposal(sess, server_id, "cuts_penalty", rb, driver_guid=due.driver_guid, filename=filename,
                                round=due.round, original_seconds=due.seconds, final_seconds=seconds,
                                cap=rb.neutralization.max_auto_time_penalty_s, source="auto")
                made += 1
            due.state = "applied"
        sess.add(due)
    return made
