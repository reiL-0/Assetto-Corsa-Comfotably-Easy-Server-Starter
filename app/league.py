"""Leagues: the roster of a championship (by Steam ID), the practice requirement and the closed entry list built from them.

A league is a `Championship` (points table in app/championship.py, penalty catalogue in app/penalties.py) plus a roster of
`LeagueMember`s. An `Event` with `league_id` runs with the roster as its entry list, locked, whoever the preset had: `session_for`
builds it each time the event is loaded (schedule start, wake, manual run), so the practice requirement is judged at that moment.
A driver can be suspended (`LeagueSuspension`): banned until lifted, out for some days, out for the next N races, kept from qualifying
for N races (kicked while the server is in qualifying, `qualy_banned`, so they start last), or sent back N grid places counted from
where they qualified (`count_results` turns each counted Qualify result into a `Penalty` of kind grid; what does not fit, because the
driver is already near the back, is carried to the next race). Cars that do not race (`LeagueMember.non_racing`: safety car, race
director, caster) never count for any of this.

Practice = valid laps (no track cuts, `Activity.cuts == 0`) logged for the driver's Steam ID in the last `practice_days` days, on any
server and track. Laps logged before `cuts` was recorded have none and do not count.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading

import time
from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import Session, func, select

from app import content, discord, metrics
from app.db import SessionDep
from app.config import settings
from app.auth import CurrentUser
from app.db import engine
from app.leaguelock import lock as league_lock, serialized
from app.stewards.auto import _event_rules, cut_notices, cut_penalties, drs_penalties, flag_penalties, give_back_penalties, neutral_penalties
from app.live.raceaudit import record
from app.models import Activity, Championship, ChampionshipEvent, Event, LeagueMember, LeagueSuspension, Penalty, Schedule, Server
from app.results import parse_result_file
from app.schemas.servers import EntryIn, SessionIn, _folder_name, _setup_name

log = logging.getLogger("acmanager.league")

router = APIRouter(prefix="/championships/{championship_id}/members", tags=["leagues"])


class MemberIn(BaseModel):
    guid: str = Field(pattern=r"^\d{17}$")
    name: str = Field(default="", max_length=60)
    team: str = Field(default="", max_length=60)
    car: str = Field(default="", max_length=80)
    exempt: bool = False
    non_racing: bool = False  # safety car, race director, caster: on the entry list, but not racing
    in_pits: bool | None = None   # with non_racing: director / caster, spectator slot (start in the pits, not on the grid); False = the pace car, in front; None = keep what is saved (an older panel does not send it)
    number: str | None = Field(default=None, pattern=r"^[0-9]{0,3}$")   # the driver's number, 0-3 ASCII digits as text; None = keep what is saved (an older panel does not send it); "" = clear it
    skin: str | None = Field(default=None, max_length=80)   # None = keep what is saved (an older panel does not send it); "" = clear it
    fixed_setup: str | None = Field(default=None, max_length=80)   # same: None keeps, "" clears

    _skin = field_validator("skin")(lambda v: v if v is None else _folder_name(v))
    _setup = field_validator("fixed_setup")(lambda v: v if v is None else _setup_name(v))


class MemberOut(MemberIn):
    in_pits: bool = False
    number: str = ""
    skin: str = ""
    fixed_setup: str = ""
    laps: int  # valid laps in the practice window, now
    eligible: bool


async def settle_metrics() -> None:
    """Before an async caller decides eligibility from the lap log: the laps received through the UDP writer thread (app/metrics.py) are written first.
    Runs the wait in a thread (the event loop never blocks) and says so if the writer could not catch up (the counts may then miss the newest laps)."""
    if not await asyncio.to_thread(metrics.flush, 2):
        log.warning("eligibility decided with %s metric event(s) still unwritten", metrics.stats()["queued"])


def valid_laps(sess: Session, guid: str, since: float, until: float) -> int:
    return sess.exec(select(func.count()).select_from(Activity).where(
        Activity.kind == "lap", Activity.guid == guid, Activity.cuts == 0, Activity.ts >= since, Activity.ts <= until)).one()


def is_eligible(c: Championship, m: LeagueMember, laps: int) -> bool:
    return not c.practice_required or m.exempt or m.non_racing or laps >= c.practice_laps


def _league(sess: Session, championship_id: int, writing: bool = False) -> Championship:
    c = sess.get(Championship, championship_id)
    if not c:
        raise HTTPException(404, "championship not found")
    if writing and c.archived_at:
        raise HTTPException(409, "league archived: it no longer takes changes")
    return c


def _status(sess: Session, c: Championship, m: LeagueMember, at: float) -> MemberOut:
    laps = valid_laps(sess, m.guid, at - c.practice_days * 86400, at)
    return MemberOut(guid=m.guid, name=m.name, team=m.team, car=m.car, skin=m.skin, fixed_setup=m.fixed_setup, number=m.number, exempt=m.exempt, non_racing=m.non_racing, in_pits=m.in_pits, laps=laps, eligible=is_eligible(c, m, laps))


@router.get("", response_model=list[MemberOut])
def list_members(championship_id: int, sess: SessionDep) -> list[MemberOut]:
    """The roster with each driver's valid laps in the window and whether they would be let in if the event started now."""
    c, now = _league(sess, championship_id), time.time()
    rows = sess.exec(select(LeagueMember).where(LeagueMember.championship_id == championship_id).order_by(LeagueMember.name)).all()
    return [_status(sess, c, m, now) for m in rows]


@router.post("", response_model=MemberOut)
@serialized
def add_member(championship_id: int, body: MemberIn, sess: SessionDep) -> MemberOut:
    """Add a driver or update the one with that Steam ID."""
    c = _league(sess, championship_id, writing=True)
    fields, old = body.model_dump(), sess.get(LeagueMember, (championship_id, body.guid))
    for k, empty in (("skin", ""), ("fixed_setup", ""), ("in_pits", False), ("number", "")):   # not sent = what was saved stays
        if fields[k] is None:
            fields[k] = getattr(old, k) if old else empty
    if fields["fixed_setup"] and not (content.setups_dir() / f"{fields['fixed_setup']}.ini").is_file():
        raise HTTPException(400, f"setup not uploaded: {fields['fixed_setup']}")
    m = sess.merge(LeagueMember(championship_id=championship_id, **fields))
    sess.commit()
    return _status(sess, c, m, time.time())


@router.delete("/{guid}", status_code=204)
@serialized
def remove_member(championship_id: int, guid: str, sess: SessionDep) -> None:
    _league(sess, championship_id, writing=True)
    m = sess.get(LeagueMember, (championship_id, guid))
    if not m:
        raise HTTPException(404, "driver not on the roster")
    sess.delete(m)
    sess.commit()


def session_for(sess: Session, ev: Event, at: float) -> SessionIn:
    """The event's session as it must be loaded at `at` (unix s): for a league event, the roster's eligible drivers as a locked
    entry list; for any other event, the saved session untouched. 409 when nobody is eligible or the roster does not fit."""
    s = SessionIn(**{k: v for k, v in ev.data.items() if k not in ("cuts_rule", "drs_rule")})
    from app.rulebook import CutRule, DrsRule
    c = sess.get(Championship, ev.league_id) if ev.league_id else None
    if not c:
        if "cuts_rule" not in ev.data and not ev.data.get("drs_rule"):
            return s
        from app.rulebook import Rulebook
        snapshot = Rulebook()
        snapshot.detectors.cuts = CutRule.model_validate(ev.data["cuts_rule"]) if ev.data.get("cuts_rule") is not None else None
        snapshot.detectors.drs = DrsRule.model_validate(ev.data["drs_rule"]) if ev.data.get("drs_rule") else None
        snapshot.detectors.give_back.enabled = False
        return s.model_copy(update={"rulebook_league_id": None, "rulebook_event_id": ev.id, "rulebook_snapshot": snapshot,
                                    "rulebook_revision": hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()})
    if c.archived_at:
        raise HTTPException(409, f"league {c.name!r} is archived: its events can no longer run")
    ok, out, suspended = [], [], []
    out_of_races = {x.guid: x for x in active_suspensions(sess, c.id, ("ban", "time", "races"), at)}
    for m in sess.exec(select(LeagueMember).where(LeagueMember.championship_id == c.id).order_by(LeagueMember.name)).all():
        st = _status(sess, c, m, at)
        if m.guid in out_of_races and not m.non_racing:
            suspended.append(st)
        else:
            (ok if st.eligible else out).append(st)
    if not ok:
        raise HTTPException(409, f"league {c.name!r}: no driver is eligible (roster empty or practice requirement not met)")
    if len(ok) > 50:
        raise HTTPException(409, f"league {c.name!r}: {len(ok)} drivers; a server takes 50")
    cars = s.cars or sorted({e.model for e in s.entries})
    if not cars:
        raise HTTPException(422, "a league event needs cars")
    if suspended:
        discord.alert(f"⛔ **{c.name}** · {ev.title}: suspendidos, quedan fuera de la lista: " + ", ".join(m.name or m.guid for m in suspended))
    if out:
        discord.alert(f"⚠️ **{c.name}** · {ev.title}: quedan fuera de la lista por no cumplir la práctica ({c.practice_laps} vueltas válidas en {c.practice_days} días): "
                      + ", ".join(f"{m.name or m.guid} ({m.laps}/{c.practice_laps})" for m in out))
    ok.sort(key=lambda m: (m.non_racing and m.in_pits, not m.non_racing))   # (the entry list is the grid when there is no qualifying: the pace car goes in front, the casters at the back (they sit in the pits, no gap at the front), not wherever their name falls; stable, so the rest keep their order)
    entries = []
    for m in ok:   # the driver's own setup, else the league's rule for the car, else the event's
        model = m.car if m.car in cars else cars[0]
        setup = m.fixed_setup or (c.car_setups or {}).get(model) or s.car_setups.get(model, "")
        entries.append(EntryIn(model=model, driver_name=m.name, team=m.team, guid=m.guid, skin=m.skin if model == m.car else "", fixed_setup=setup, spectator=m.non_racing and m.in_pits))   # a skin belongs to the driver's own car
    from app.rulebook import effective
    snapshot = effective(c).model_copy(deep=True)
    if "cuts_rule" in ev.data:
        snapshot.detectors.cuts = CutRule.model_validate(ev.data["cuts_rule"]) if ev.data["cuts_rule"] is not None else None
    if ev.data.get("drs_rule"):   # the event's own DRS rules win over the league's
        snapshot.detectors.drs = DrsRule.model_validate(ev.data["drs_rule"])
    revision = hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()
    return s.model_copy(update={"rulebook_revision": revision, "rulebook_league_id": c.id, "rulebook_event_id": ev.id, "rulebook_snapshot": snapshot, "entries": entries, "locked": True, "pickup": False, "cars": cars})


def active_suspensions(sess: Session, championship_id: int, kinds: tuple[str, ...], at: float, guid: str | None = None) -> list[LeagueSuspension]:
    q = select(LeagueSuspension).where(LeagueSuspension.championship_id == championship_id, LeagueSuspension.active, LeagueSuspension.kind.in_(kinds))
    if guid:
        q = q.where(LeagueSuspension.guid == guid)
    return [x for x in sess.exec(q).all() if x.kind != "time" or (x.until or 0) > at]


def qualy_banned(server_id: int, guid: str) -> bool:
    """Is this driver kept from qualifying on this server right now? The server must be running a league event (its schedule is running)."""
    with Session(engine) as s:
        sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
        ev = s.get(Event, sc.event_id) if sc else None
        return bool(ev and ev.league_id and active_suspensions(s, ev.league_id, ("qualy",), time.time(), guid))


def _applied_rulebook(sess: Session, server_id: int, c: Championship | None, ev: Event | None = None):
    from app.rulebook import Rulebook
    server = sess.get(Server, server_id)
    applied = (server.session or {}) if server else {}
    snapshot = applied.get("rulebook_snapshot")
    if c is None and ev is None:
        return None
    if snapshot and ((c and applied.get("rulebook_league_id") in (None, c.id) and
                      (ev is None or applied.get("rulebook_event_id") in (None, ev.id))) or
                     (ev and c is None and applied.get("rulebook_event_id") == ev.id and applied.get("rulebook_league_id") is None)):
        return Rulebook.model_validate(snapshot)
    if c:
        return _event_rules(c, ev)
    return None


class _ManualCutRule(dict):
    """Server-only rule snapshot: metadata stays outside the hub JSON keys."""

    live_only = True


class _ManualDrsRule(dict):
    """A manual session's DRS rule: the drive-through is live only (no result penalty), like its cuts."""

    live_only = True


DEFAULT_CUT_RULE = {"max": 2, "per": 1, "step": 5, "cap": 600, "ladder": [0, 5, 5, 10]}


_CUT_RULES: dict[int, tuple[float, dict | None]] = {}   # server id -> (when it was read, the rule)
_GIVE_BACK_RULES: dict[int, tuple[float, dict | None]] = {}
_NEUTRALIZATION_RULES: dict[int, tuple[float, dict]] = {}
_DRS_RULES: dict[int, tuple[float, dict | None]] = {}
_BLUE_RULES: dict[int, tuple[float, dict]] = {}
_FLAG_MODES: dict[int, tuple[float, dict]] = {}
_cut_rule_lock = threading.Lock()
CUT_RULE_TTL = 30.0


def invalidate_rule_cache(server_id: int) -> None:
    """An applied session has a new snapshot; discard the previous session's rules."""
    with _cut_rule_lock:
        for cache in (_CUT_RULES, _GIVE_BACK_RULES, _FLAG_MODES, _NEUTRALIZATION_RULES, _DRS_RULES, _BLUE_RULES):
            cache.pop(server_id, None)


def cut_rule(server_id: int) -> dict | None:
    """Running event rules take precedence; absent rules use the live-only default.
    Cached under the rule lock; refreshed by request threads, never the live event loop.
    """
    hit = _CUT_RULES.get(server_id)
    if hit and time.time() - hit[0] < CUT_RULE_TTL:
        return hit[1]
    with _cut_rule_lock:   # (one read at a time: requests that find the cache stale together do not all go to the database, and an older read never replaces a newer one)
        hit = _CUT_RULES.get(server_id)
        if hit and time.time() - hit[0] < CUT_RULE_TTL:
            return hit[1]
        rule = _ManualCutRule(DEFAULT_CUT_RULE)
        with Session(engine) as s:
            sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
            ev = s.get(Event, sc.event_id) if sc else None
            c = s.get(Championship, ev.league_id) if ev and ev.league_id else None
            rb = _applied_rulebook(s, server_id, c, ev)
            cut = rb.detectors.cuts if rb else None
            if sc is None:
                from app.rulebook import CutRule

                server = s.get(Server, server_id)
                raw = (server.session or {}).get("cuts_rule") if server else None
                cut = CutRule.model_validate(raw) if raw is not None else None
            if cut:
                rule = {"max": cut.tolerance, "per": cut.cuts_per_step, "step": cut.step_seconds, "cap": min(cut.max_seconds, rb.neutralization.max_auto_time_penalty_s) if rb else cut.max_seconds}
                if cut.ladder is not None:
                    rule["ladder"] = list(cut.ladder)
                if sc is None:
                    rule = _ManualCutRule(rule)
        if len(_CUT_RULES) > 64:
            _CUT_RULES.clear()
        _CUT_RULES[server_id] = (time.time(), rule)
        return rule


def give_back_rule(server_id: int) -> dict | None:
    """The rule of the advantage gained off the track of the league event running on this server: {seconds, window_s}; None when no league event runs or the league switched it off. Cached like `cut_rule`
    (CUT_RULE_TTL, one read at a time): from a request thread, never from the event loop."""
    hit = _GIVE_BACK_RULES.get(server_id)
    if hit and time.time() - hit[0] < CUT_RULE_TTL:
        return hit[1]
    with _cut_rule_lock:
        hit = _GIVE_BACK_RULES.get(server_id)
        if hit and time.time() - hit[0] < CUT_RULE_TTL:
            return hit[1]
        rule = None
        with Session(engine) as s:
            sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
            ev = s.get(Event, sc.event_id) if sc else None
            c = s.get(Championship, ev.league_id) if ev and ev.league_id else None
            rb = _applied_rulebook(s, server_id, c, ev)
            gb = rb.detectors.give_back if rb else None
            if gb and gb.enabled:
                rule = {"seconds": gb.seconds, "window_s": gb.window_s}
        if len(_GIVE_BACK_RULES) > 64:
            _GIVE_BACK_RULES.clear()
        _GIVE_BACK_RULES[server_id] = (time.time(), rule)
        return rule


def drs_rule(server_id: int) -> dict | None:
    """The DRS rule (app/rulebook.py `DrsRule`, as a dict) of the league event running on this server, or of the manual session's own `drs_rule`; None = no DRS rules. Cached like `give_back_rule`:
    from a request thread or a worker, never from the event loop."""
    hit = _DRS_RULES.get(server_id)
    if hit and time.time() - hit[0] < CUT_RULE_TTL:
        return hit[1]
    with _cut_rule_lock:
        hit = _DRS_RULES.get(server_id)
        if hit and time.time() - hit[0] < CUT_RULE_TTL:
            return hit[1]
        rule = None
        with Session(engine) as s:
            sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
            ev = s.get(Event, sc.event_id) if sc else None
            c = s.get(Championship, ev.league_id) if ev and ev.league_id else None
            rb = _applied_rulebook(s, server_id, c, ev)
            drs = rb.detectors.drs if rb else None
            if sc is None:   # a manual session (no schedule): the rule the panel put in its own settings
                from app.rulebook import DrsRule
                server = s.get(Server, server_id)
                raw = (server.session or {}).get("drs_rule") if server else None
                drs = DrsRule.model_validate(raw) if raw is not None else None
            if drs:
                rule = _ManualDrsRule(drs.model_dump()) if sc is None else drs.model_dump()
        if len(_DRS_RULES) > 64:
            _DRS_RULES.clear()
        _DRS_RULES[server_id] = (time.time(), rule)
        return rule


def blue_rule(server_id: int) -> dict:
    """The blue-flag rule (app/rulebook.py `BlueRule`, as a dict) of the league event running on this server; the defaults when no rulebook applies. Cached like `drs_rule`: from a request thread or a worker."""
    hit = _BLUE_RULES.get(server_id)
    if hit and time.time() - hit[0] < CUT_RULE_TTL:
        return hit[1]
    with _cut_rule_lock:
        hit = _BLUE_RULES.get(server_id)
        if hit and time.time() - hit[0] < CUT_RULE_TTL:
            return hit[1]
        from app.rulebook import BlueRule
        rule = BlueRule().model_dump()
        with Session(engine) as s:
            sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
            ev = s.get(Event, sc.event_id) if sc else None
            c = s.get(Championship, ev.league_id) if ev and ev.league_id else None
            rb = _applied_rulebook(s, server_id, c, ev)
            if rb:
                rule = rb.detectors.blue.model_dump()
        if len(_BLUE_RULES) > 64:
            _BLUE_RULES.clear()
        _BLUE_RULES[server_id] = (time.time(), rule)
        return rule


def blue_rule_cached(server_id: int) -> dict | None:
    hit = _BLUE_RULES.get(server_id)
    return hit[1] if hit and time.time() - hit[0] < 3 * CUT_RULE_TTL else None


def blue_rule_stale(server_id: int) -> bool:
    hit = _BLUE_RULES.get(server_id)
    return not hit or time.time() - hit[0] >= CUT_RULE_TTL


def drs_rule_cached(server_id: int) -> dict | None:
    """What `drs_rule` read last (no database, no waiting): for the monitor, which runs on the event loop."""
    hit = _DRS_RULES.get(server_id)
    return hit[1] if hit and time.time() - hit[0] < 3 * CUT_RULE_TTL else None


def drs_rule_stale(server_id: int) -> bool:
    hit = _DRS_RULES.get(server_id)
    return not hit or time.time() - hit[0] >= CUT_RULE_TTL


def flag_modes(server_id: int) -> dict:
    """{blue, danger}: off | shadow | live, from the rulebook of the league event running on this server; both `shadow` when no league event runs. Cached like `give_back_rule`: from a request thread, never from the event loop."""
    hit = _FLAG_MODES.get(server_id)
    if hit and time.monotonic() - hit[0] < CUT_RULE_TTL:
        return hit[1]
    with _cut_rule_lock:
        hit = _FLAG_MODES.get(server_id)
        if hit and time.monotonic() - hit[0] < CUT_RULE_TTL:
            return hit[1]
        modes = {"blue": "shadow", "danger": "shadow"}
        with Session(engine) as s:
            sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
            ev = s.get(Event, sc.event_id) if sc else None
            c = s.get(Championship, ev.league_id) if ev and ev.league_id else None
            rb = _applied_rulebook(s, server_id, c, ev)
            if rb:
                modes = rb.detectors.flags.model_dump()
        if len(_FLAG_MODES) > 64:
            _FLAG_MODES.clear()
        _FLAG_MODES[server_id] = (time.monotonic(), modes)
        return modes


def flag_modes_cached(server_id: int) -> dict | None:
    """What `flag_modes` read last (no database, no waiting) or None when it is missing or old (3 x the TTL): the monitor then does NOT show anything live."""
    hit = _FLAG_MODES.get(server_id)
    return hit[1] if hit and time.monotonic() - hit[0] < 3 * CUT_RULE_TTL else None


def flag_modes_stale(server_id: int) -> bool:
    hit = _FLAG_MODES.get(server_id)
    return not hit or time.monotonic() - hit[0] >= CUT_RULE_TTL


def neutralization_rule(server_id: int) -> dict:
    """Applied neutralization thresholds and category, cached like flags; no event means off."""
    hit = _NEUTRALIZATION_RULES.get(server_id)
    if hit and time.monotonic() - hit[0] < CUT_RULE_TTL:
        return hit[1]
    with _cut_rule_lock:
        hit = _NEUTRALIZATION_RULES.get(server_id)
        if hit and time.monotonic() - hit[0] < CUT_RULE_TTL:
            return hit[1]
        modes = {"mode": "off"}
        with Session(engine) as s:
            sc = s.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state == "running")).first()
            ev = s.get(Event, sc.event_id) if sc else None
            c = s.get(Championship, ev.league_id) if ev and ev.league_id else None
            rb = _applied_rulebook(s, server_id, c, ev)
            if rb:
                modes = rb.neutralization.model_dump()
                server = s.get(Server, server_id)
                modes["category"] = (server.session or {}).get("category", "") if server else ""
                modes["rulebook_revision"] = (server.session or {}).get("rulebook_revision") if server else None
        if len(_NEUTRALIZATION_RULES) > 64:
            _NEUTRALIZATION_RULES.clear()
        _NEUTRALIZATION_RULES[server_id] = (time.monotonic(), modes)
        return modes


def neutralization_rule_cached(server_id: int) -> dict | None:
    """What `neutralization_rule` read last (no database, no waiting) or None when it is missing or old (3 x the TTL): the monitor then does NOT show anything live."""
    hit = _NEUTRALIZATION_RULES.get(server_id)
    return hit[1] if hit and time.monotonic() - hit[0] < 3 * CUT_RULE_TTL else None


def neutralization_rule_stale(server_id: int) -> bool:
    hit = _NEUTRALIZATION_RULES.get(server_id)
    return not hit or time.monotonic() - hit[0] >= CUT_RULE_TTL


def give_back_rule_cached(server_id: int) -> dict | None:
    """What `give_back_rule` read last (no database, no waiting): for the monitor, which runs on the event loop."""
    hit = _GIVE_BACK_RULES.get(server_id)
    return hit[1] if hit and time.time() - hit[0] < 3 * CUT_RULE_TTL else None   # (never an old league's rule: the monitor refreshes it every CUT_RULE_TTL)


def give_back_rule_stale(server_id: int) -> bool:
    """Whether the cached rule of this server is missing or due to be read again (the monitor then reads it in a worker thread)."""
    hit = _GIVE_BACK_RULES.get(server_id)
    return not hit or time.time() - hit[0] >= CUT_RULE_TTL


def cut_rule_stale(server_id: int) -> bool:
    hit = _CUT_RULES.get(server_id)
    return not hit or time.time() - hit[0] >= CUT_RULE_TTL


def cut_rule_cached(server_id: int) -> dict | None:
    """What `cut_rule` read last, with no database and no waiting (live-only default before the first read): for the places that must not do I/O, as the state snapshot taken under the race-control lock."""
    hit = _CUT_RULES.get(server_id)
    return hit[1] if hit and hit[1] is not None else _ManualCutRule(DEFAULT_CUT_RULE)


def _qualy_grid(sess: Session, c: Championship, server_id: int, filename: str, parsed: dict) -> None:
    """Turn the league's grid suspensions into a grid Penalty on this qualifying result: the places are counted from where each driver
    qualified among the cars that race, and what does not fit (already near the back) stays for the next qualifying."""
    nonrun = set(sess.exec(select(LeagueMember.guid).where(LeagueMember.championship_id == c.id, LeagueMember.non_racing)).all())
    order = [e["driver_guid"] for e in parsed["classification"] if e["driver_guid"] and e["driver_guid"] not in nonrun]
    for x in active_suspensions(sess, c.id, ("grid",), time.time()):
        if filename in x.served or x.guid not in order or x.guid in nonrun:
            continue   # did not qualify in this one: the places wait for the next
        moved = min(x.places_left, len(order) - 1 - order.index(x.guid))
        x.served = [*x.served, filename]
        x.places_left -= moved
        x.active = x.places_left > 0
        if moved:
            sess.add(Penalty(server_id=server_id, filename=filename, driver_guid=x.guid, kind="grid", value=moved, created_by=x.created_by,
                             reason=f"Suspensión de liga: pierde {moved} lugares en la parrilla" + (f" (le quedan {x.places_left} para la siguiente)" if x.places_left else "")))
            discord.announce(f"⛔ **{c.name}**: {x.name or x.guid} pierde {moved} lugares en la parrilla" + (f"; {x.places_left} más en la siguiente carrera" if x.places_left else ""))
        sess.add(x)


def _race_served(sess: Session, c: Championship, finished_at: float) -> None:
    """A league race was counted: one less race for every suspension measured in races (the ones put before it ended)."""
    for x in active_suspensions(sess, c.id, ("races", "qualy"), finished_at):
        if x.created_at.replace(tzinfo=timezone.utc).timestamp() <= finished_at:
            x.races_left -= 1
            x.active = x.races_left > 0
            sess.add(x)


def count_results(sess: Session, sc: Schedule, ev: Event, until: float) -> int:
    """Count, for the league of `ev`, the Race and Qualify results its schedule produced: files in the server's results folder written
    between the schedule's start and `until`. Idempotent; returns how many Race results were added. A calendar event is thus a counted
    race of its league (draft or published) without anyone picking files. A Race scores (app/championship.py) and uses up the
    suspensions measured in races; a Qualify result feeds the grid suspensions; practice results are skipped."""
    if not (ev.league_id or ev.count_only_league_id) or not (c := sess.get(Championship, ev.league_id or ev.count_only_league_id)) or c.archived_at:   # an archived league counts no more results
        return 0
    if not league_lock.acquire(blocking=False):   # a league is being archived or edited right now: this runs on the event loop, so it does not wait; the next tick counts (idempotent)
        return 0
    try:
        sess.refresh(c)   # (archived while this waited for the lock?)
        return 0 if c.archived_at else _count_results(sess, sc, ev, c, until)
    finally:
        league_lock.release()


def _count_results(sess: Session, sc: Schedule, ev: Event, c: Championship, until: float) -> int:
    sess.info["race_audit_proposals"] = []
    d, added = Path(settings.data_dir) / "instances" / str(sc.server_id) / "results", 0
    last_end = sc.start_at   # where the race being counted began, for what fell due in it: the end of the one counted before (or the schedule's start)
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        if not sc.start_at <= f.stat().st_mtime <= until:
            continue
        if sess.exec(select(ChampionshipEvent).where(ChampionshipEvent.championship_id == c.id, ChampionshipEvent.server_id == sc.server_id,
                                                      ChampionshipEvent.filename == f.name)).first():
            last_end = max(last_end, f.stat().st_mtime)
            continue
        parsed = parse_result_file(f)
        if parsed["type"] not in ("Race", "Qualify"):
            continue
        scores = ev.league_id is not None   # a practice (count_only_league_id) is listed but adds nothing: no points, no suspension served, no grid, no cut warnings
        sess.add(ChampionshipEvent(championship_id=c.id, server_id=sc.server_id, filename=f.name, event_id=ev.id, session_type=parsed["type"], scores=scores,
                                   rulebook_snapshot=_event_rules(c, ev).model_dump(mode="json")))
        sess.flush()  # sanctions read this row in the same transaction
        if not scores:
            continue
        if parsed["type"] == "Race":
            added += 1
            _race_served(sess, c, f.stat().st_mtime)
            cut_notices(sess, c, sc.server_id, f.name, parsed)
            cut_penalties(sess, c, sc.server_id, f.name, parsed, f.stat().st_mtime, last_end)
            drs_penalties(sess, c, sc.server_id, f.name, parsed, f.stat().st_mtime, last_end)
            flag_penalties(sess, c, sc.server_id, f.name, parsed, f.stat().st_mtime, last_end)
            give_back_penalties(sess, c, sc.server_id, f.name, parsed, f.stat().st_mtime, last_end)
            neutral_penalties(sess, c, sc.server_id, f.name, parsed, f.stat().st_mtime, last_end)
            last_end = f.stat().st_mtime
        else:
            _qualy_grid(sess, c, sc.server_id, f.name, parsed)
    sess.commit()
    for server_id, kind, fields in sess.info.pop("race_audit_proposals", []):
        record(server_id, kind, **fields, executed=True, persistence="committed")
    return added


# --- suspensions ---

susp_router = APIRouter(prefix="/championships/{championship_id}/suspensions", tags=["leagues"])


class SuspensionIn(BaseModel):
    guid: str = Field(pattern=r"^\d{17}$")
    kind: Literal["ban", "time", "races", "qualy", "grid"]
    days: int | None = Field(default=None, ge=1, le=365)  # time
    races: int | None = Field(default=None, ge=1, le=50)  # races, qualy
    places: int | None = Field(default=None, ge=1, le=100)  # grid
    reason: str = Field(min_length=3, max_length=300)

    @model_validator(mode="after")
    def _has_its_amount(self) -> SuspensionIn:
        need = {"time": self.days, "races": self.races, "qualy": self.races, "grid": self.places}
        if self.kind in need and need[self.kind] is None:
            raise ValueError({"time": "days", "races": "races", "qualy": "races", "grid": "places"}[self.kind] + " is required for this kind")
        return self


def _describe(x: LeagueSuspension) -> str:
    return {"ban": "baneo de la liga", "time": f"suspendido hasta <t:{int(x.until or 0)}:F>", "races": f"suspendido {x.races_left} carrera(s)",
            "qualy": f"sin clasificar {x.races_left} carrera(s)", "grid": f"pierde {x.places_left} lugares en la parrilla"}[x.kind]


@susp_router.get("")
def list_suspensions(championship_id: int, sess: SessionDep) -> list[dict]:
    """Active ones first, then the 20 most recent finished."""
    _league(sess, championship_id)
    rows = sess.exec(select(LeagueSuspension).where(LeagueSuspension.championship_id == championship_id).order_by(LeagueSuspension.id.desc())).all()
    now = time.time()
    live = [x for x in rows if x.active and not (x.kind == "time" and (x.until or 0) <= now)]
    old = [x for x in rows if x not in live][:20]
    return [{"id": x.id, "guid": x.guid, "name": x.name, "kind": x.kind, "detail": _describe(x), "until": x.until, "races_left": x.races_left,
             "places_left": x.places_left, "reason": x.reason, "created_by": x.created_by, "active": x in live} for x in live + old]


@susp_router.post("", status_code=201)
@serialized
def add_suspension(championship_id: int, body: SuspensionIn, sess: SessionDep, user: CurrentUser) -> dict:
    c = _league(sess, championship_id, writing=True)
    m = sess.get(LeagueMember, (championship_id, body.guid))
    if not m:
        raise HTTPException(404, "driver not on the roster")
    if m.non_racing:
        raise HTTPException(422, "a car that does not race cannot be suspended")
    x = LeagueSuspension(championship_id=championship_id, guid=body.guid, name=m.name, kind=body.kind, reason=body.reason.strip(), created_by=user.username,
                         until=time.time() + body.days * 86400 if body.kind == "time" else None,
                         races_left=body.races or 0 if body.kind in ("races", "qualy") else 0, places_left=body.places or 0 if body.kind == "grid" else 0)
    sess.add(x)
    sess.commit()
    sess.refresh(x)
    discord.announce(f"⛔ **{c.name}**: {m.name or m.guid} — {_describe(x)}. Motivo: {x.reason}")
    return {"id": x.id, "detail": _describe(x)}


@susp_router.delete("/{suspension_id}", status_code=204)
@serialized
def lift_suspension(championship_id: int, suspension_id: int, sess: SessionDep) -> None:
    _league(sess, championship_id, writing=True)
    x = sess.get(LeagueSuspension, suspension_id)
    if not x or x.championship_id != championship_id:
        raise HTTPException(404, "suspension not found")
    x.active = False
    sess.add(x)
    sess.commit()
