"""Warnings for the race director from a counted Race's result file (no live incidents involved: acServer already writes `Cuts` for every lap).

`cut_seconds` is the rule (a rulebook's `detectors.cuts`, app/rulebook.py); `cut_drafts` is the pure calculation over a parsed result, which the dry-run (calibration) also uses;
`cut_notices` writes a `Notice` per driver who reached it, when the league counts the file. It never penalises: the steward confirms the notice in the Sancionar panel (it
becomes a time `Penalty`) or dismisses it, and closing the result deletes what is left (app/penalties.py). One notice per (file, driver, reason) -- the unique constraint --
so counting the file in another league does not duplicate it. Automatic time proposals and give-back sanctions use the applied neutralization cap; the reason records any reduction.
Contacts and track limits are still evidence (see README.md).
"""

from __future__ import annotations

from sqlalchemy import update
from sqlmodel import Session, select

from app.live.raceaudit import record
from app.models import Championship, ChampionshipEvent, CutDue, DrsDue, Event, FlagDue, GiveBackDue, NeutralDue, Notice, Penalty, ResultClose, Server
from app.results import non_racing_for
from app.rulebook import CutRule, Rulebook, effective


def _event_rules(c: Championship, event: Event | None) -> Rulebook:
    rb = effective(c).model_copy(deep=True)
    if event and "cuts_rule" in event.data:
        rb.detectors.cuts = CutRule.model_validate(event.data["cuts_rule"]) if event.data["cuts_rule"] is not None else None
    return rb


def _applied_rules(sess: Session, c: Championship, server_id: int, filename: str | None = None) -> Rulebook:
    """Use the result's frozen snapshot; legacy rows have only today's league/event settings."""
    counted = sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.server_id == server_id,
                        ChampionshipEvent.filename == filename, ChampionshipEvent.championship_id == c.id)).first() if filename else None
    if counted and counted.rulebook_snapshot is not None:
        return Rulebook.model_validate(counted.rulebook_snapshot)
    event = sess.get(Event, counted.event_id) if counted and counted.event_id else None
    return _event_rules(c, event)


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
    """[{driver_guid, driver_name, cuts, seconds}] for the drivers of this Race who reach the rule. Pure: nothing is read or written.
    A rule with a ladder gives none here: its penalties are the live rounds (`cut_penalties`), and `ladder_drafts` only previews them."""
    if parsed["type"] != "Race" or rule.ladder is not None:
        return []
    return [d | {"seconds": s} for d in _cuts_per_driver(parsed, racing) if (s := cut_seconds(d["cuts"], rule))]


def _cuts_per_driver(parsed: dict, racing: set[str]) -> list[dict]:
    cuts: dict[str, int] = {}
    for lap in parsed["laps"]:
        if lap["driver_guid"] and (lap["lap_time_ms"] or 0) > 0:   # a lap with no time is an unfinished one
            cuts[lap["driver_guid"]] = cuts.get(lap["driver_guid"], 0) + (lap["cuts"] or 0)
    names = {e["driver_guid"]: e["driver_name"] for e in parsed["classification"]}
    return [{"driver_guid": g, "driver_name": names.get(g) or g, "cuts": n} for g, n in cuts.items() if g in racing]


def ladder_drafts(parsed: dict, rule: CutRule, racing: set[str], cap: int) -> list[dict]:
    """Calibration preview of a ladder rule over a finished Race (the lap cuts only, no live reports): every `tolerance + 1` cuts is a round
    that adds its ladder step (the last repeats), each step limited by `cap` like the real penalty. Pure."""
    if parsed["type"] != "Race" or not rule.ladder:
        return []
    size = rule.tolerance + 1
    out = []
    for d in _cuts_per_driver(parsed, racing):
        rounds = d["cuts"] // size
        if rounds:
            steps = [rule.ladder[min(r, len(rule.ladder) - 1)] for r in range(rounds)]
            out.append(d | {"seconds": sum(min(s, cap) for s in steps), "drive_throughs": steps.count(0)})
    return out


def give_back_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """The time penalties of the advantage gained off the track (app/live/giveback.py): every `GiveBackDue` of this server that fell due while this schedule ran (`since`, its start, up to the end of this Race) and is
    still due becomes a time `Penalty` on this result, once (`auto_key` = giveback:<id>), with its original seconds limited by the applied cap when automatic; director sanctions retain their value and author. A due of a driver who is not in the result is `expired`: that race is over,
    it must not wait for the next one. Does not commit; returns how many penalties were made."""
    rb = _applied_rules(sess, c, server_id, filename)
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


def neutral_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """The time penalties of the VSC / FCY of this Race (`NeutralDue`, written while it ran: `since` is the start of the schedule, `finished_at` the end of this Race) written straight onto the result: ONE time
    `Penalty` per driver (`auto_key` = neutral:<guid>) whose seconds are the sum of his dues, limited once by the applied cap (`neutralization.max_auto_time_penalty_s`), the reasons kept; the director can
    remove it in the Sancionar panel like any other. A driver who is not in the classification expires his dues. Does not commit; returns how many penalties were made."""
    rb = _applied_rules(sess, c, server_id, filename)
    if parsed["type"] != "Race" or sess.get(ResultClose, (server_id, filename)):
        return 0
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    by_driver: dict[str, list[NeutralDue]] = {}
    for due in sess.exec(select(NeutralDue).where(NeutralDue.server_id == server_id, NeutralDue.state == "due", NeutralDue.ts >= since, NeutralDue.ts <= finished_at).order_by(NeutralDue.ts)).all():
        if due.driver_guid not in racing:
            due.state = "expired"
            sess.add(due)
            continue
        by_driver.setdefault(due.driver_guid, []).append(due)
    made = 0
    for guid, dues in by_driver.items():
        total = sum(d.seconds for d in dues)
        seconds, reason = capped_auto_time(total, rb.neutralization.max_auto_time_penalty_s, "; ".join(d.reason for d in dues if d.reason) or "VSC / FCY: delta no respetado")
        key = f"neutral:{guid}"
        if not sess.exec(select(Penalty.id).where(Penalty.server_id == server_id, Penalty.filename == filename, Penalty.driver_guid == guid, Penalty.auto_key == key)).first():
            sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=guid, kind="time", value=seconds * 1000, created_by="auto", auto_key=key, reason=reason))
            _audit_proposal(sess, server_id, "neutral_penalty", rb, driver_guid=guid, filename=filename, original_seconds=total, final_seconds=seconds, cap=rb.neutralization.max_auto_time_penalty_s)
            made += 1
        for d in dues:
            d.state = "applied"
            sess.add(d)
    return made


def cut_notices(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict) -> int:
    """Add the cut warnings of this Race result under the league's rulebook; returns how many were created. Does not commit (the caller counts the file in the same transaction)."""
    rb = _applied_rules(sess, c, server_id, filename)
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


def stage_cut_dues(sess: Session, rows: list[dict], served: set[tuple] = frozenset()) -> None:
    """Idempotent inserts, then the drive-throughs served (a `due` one becomes `served`, never back); caller owns the transaction and acknowledges after commit."""
    from sqlalchemy.dialects.sqlite import insert
    for row in rows:
        sess.execute(insert(CutDue).values(**row, state="due").on_conflict_do_nothing(
            index_elements=["server_id", "session_key", "driver_guid", "round"]))
    for server_id, session_key, guid, round_no in served:
        sess.execute(update(CutDue).where(CutDue.server_id == server_id, CutDue.session_key == session_key, CutDue.driver_guid == guid,
                                          CutDue.round == round_no, CutDue.state == "due").values(state="served"))


def stage_drs_dues(sess: Session, rows: list[dict], served: set[tuple] = frozenset()) -> None:
    """Idempotent inserts of the DRS offences, then the drive-throughs served (a `due` one becomes `served`, never back); the caller owns the transaction and acknowledges after commit."""
    from sqlalchemy.dialects.sqlite import insert
    for row in rows:
        sess.execute(insert(DrsDue).values(**row, state="due").on_conflict_do_nothing(index_elements=["server_id", "session_key", "driver_guid", "period"]))
    for server_id, session_key, guid, period in served:
        sess.execute(update(DrsDue).where(DrsDue.server_id == server_id, DrsDue.session_key == session_key, DrsDue.driver_guid == guid, DrsDue.period == period, DrsDue.state == "due").values(state="served"))


def stage_flag_dues(sess: Session, rows: list[dict], served: set[tuple] = frozenset()) -> None:
    """Idempotent inserts of the sanctions for ignored flags, then the drive-throughs served (a `due` one becomes `served`, never back); the caller owns the transaction and acknowledges after commit."""
    from sqlalchemy.dialects.sqlite import insert
    for row in rows:
        sess.execute(insert(FlagDue).values(**row, state="due").on_conflict_do_nothing(index_elements=["server_id", "session_key", "driver_guid", "kind", "seq"]))
    for server_id, session_key, guid, kind, seq in served:
        sess.execute(update(FlagDue).where(FlagDue.server_id == server_id, FlagDue.session_key == session_key, FlagDue.driver_guid == guid, FlagDue.kind == kind,
                                           FlagDue.seq == seq, FlagDue.state == "due").values(state="served"))


FLAG_REASONS = {"blue": "No cedió el paso a la bandera azul"}


def flag_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """Sanctions for ignored flags (`FlagDue`, app/live/racecontrol.py): every one of this server that fell due while this schedule ran (`since` up to the end of this Race) and is still due becomes a time `Penalty`
    on this result, once (`auto_key` = flag:<id>); a drive-through that was served only closes. The seconds are the ones the rule's ladder gave (or `unserved_s` for a drive-through nobody served); a driver who is not
    in the result: `expired`. Does not commit; returns how many penalties were made."""
    rb = _applied_rules(sess, c, server_id, filename)
    if parsed["type"] != "Race" or sess.get(ResultClose, (server_id, filename)):
        return 0
    from app.live.racecontrol import pending_flag_dues, pending_flag_served
    stage_flag_dues(sess, [r for r in pending_flag_dues() if r["server_id"] == server_id], {k for k in pending_flag_served() if k[0] == server_id})   # (the last ones, even if the monitor has not written them yet)
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for due in sess.exec(select(FlagDue).where(FlagDue.server_id == server_id, FlagDue.state.in_(("due", "served")), FlagDue.ts >= since, FlagDue.ts <= finished_at).order_by(FlagDue.ts)).all():
        if due.driver_guid not in racing:
            due.state = "expired"
        else:
            if due.state == "due":
                key = f"flag:{due.id}"
                if not sess.exec(select(Penalty.id).where(Penalty.auto_key == key)).first():
                    sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=due.driver_guid, kind="time", value=due.seconds * 1000, auto_key=key, created_by="auto",
                                     reason=f"{FLAG_REASONS.get(due.kind, due.kind)} (infracción {due.seq})"))
                    _audit_proposal(sess, server_id, "flag_penalty", rb, driver_guid=due.driver_guid, filename=filename, flag=due.kind, seq=due.seq, final_seconds=due.seconds, source="auto")
                    made += 1
            due.state = "applied"
        sess.add(due)
    return made


def drs_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """DRS used while it was off (app/live/drs.py): every `DrsDue` of this server that fell due while this schedule ran (`since` up to the end of this Race) and was never served becomes a time `Penalty` of the
    rule's `unserved_s` seconds on this result, once (`auto_key` = drs:<id>); a served one only closes. Not capped by the rulebook's automatic limit: the seconds are the ones the admin chose for DRS.
    A due of a driver who is not in the result is `expired`. Does not commit; returns how many penalties were made."""
    rb = _applied_rules(sess, c, server_id, filename)
    if parsed["type"] != "Race" or sess.get(ResultClose, (server_id, filename)):
        return 0
    from app.live.racecontrol import pending_drs_dues, pending_drs_served
    stage_drs_dues(sess, [r for r in pending_drs_dues() if r["server_id"] == server_id], {k for k in pending_drs_served() if k[0] == server_id})   # (the last offences, even if the monitor has not written them yet)
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for due in sess.exec(select(DrsDue).where(DrsDue.server_id == server_id, DrsDue.state.in_(("due", "served")), DrsDue.ts >= since, DrsDue.ts <= finished_at).order_by(DrsDue.ts)).all():
        if due.driver_guid not in racing:
            due.state = "expired"
        else:
            if due.state == "due":
                key = f"drs:{due.id}"
                if not sess.exec(select(Penalty.id).where(Penalty.auto_key == key)).first():
                    sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=due.driver_guid, kind="time", value=due.seconds * 1000, auto_key=key, created_by="auto",
                                     reason="DRS usado con el DRS inactivo, drive-through sin cumplir"))
                    _audit_proposal(sess, server_id, "drs_penalty", rb, driver_guid=due.driver_guid, filename=filename, period=due.period, final_seconds=due.seconds, source="auto")
                    made += 1
            due.state = "applied"
        sess.add(due)
    return made


def cut_penalties(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict, finished_at: float, since: float) -> int:
    """Convert live rounds using tolerance/ladder, capped per penalty at the applied
    rulebook's neutralization.max_auto_time_penalty_s (default 10 s, configurable
    in that same rulebook). The required legacy cut fields do not set these seconds.
    """
    rb = _applied_rules(sess, c, server_id, filename)
    rule = rb.detectors.cuts
    if parsed["type"] != "Race" or not rule or rule.ladder is None or sess.get(ResultClose, (server_id, filename)):
        return 0
    # Include the last lap even if the monitor has not persisted its queue yet.
    from app.live.racecontrol import pending_cut_dues, pending_cut_served
    stage_cut_dues(sess, [r for r in pending_cut_dues() if r["server_id"] == server_id], {k for k in pending_cut_served() if k[0] == server_id})
    racing = {e["driver_guid"] for e in parsed["classification"]} - non_racing_for(sess, server_id, filename)
    made = 0
    for due in sess.exec(select(CutDue).where(CutDue.server_id == server_id, CutDue.state == "due", CutDue.ts >= since, CutDue.ts <= finished_at).order_by(CutDue.ts, CutDue.round)).all():
        if due.driver_guid not in racing:
            due.state = "expired"
        else:
            key = f"cuts:{due.id}"
            if not sess.exec(select(Penalty.id).where(Penalty.auto_key == key)).first():
                if due.seconds:
                    seconds, reason = capped_auto_time(due.seconds, rb.neutralization.max_auto_time_penalty_s, f"Cortes de pista: ronda {due.round}")
                else:   # a drive-through nobody served: the rule's fixed seconds instead
                    seconds, reason = capped_auto_time((rule.dt_fixed_s or 10), rb.neutralization.max_auto_time_penalty_s, f"Cortes de pista: ronda {due.round}, drive-through sin cumplir")
                sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=due.driver_guid, kind="time",
                                 value=seconds * 1000, auto_key=key, created_by="auto", reason=reason))
                _audit_proposal(sess, server_id, "cuts_penalty", rb, driver_guid=due.driver_guid, filename=filename,
                                round=due.round, original_seconds=due.seconds or (rule.dt_fixed_s or 10), final_seconds=seconds,
                                cap=rb.neutralization.max_auto_time_penalty_s, source="auto")
                made += 1
            due.state = "applied"
        sess.add(due)
    return made
