"""Race control: the director's flag, the virtual safety car (VSC) and drive-throughs to serve, for the CSP hub script on every client (app/live/racecontrol.py).

  GET    /servers/{id}/race-state               what the hub reads (the website serves it as /api/csp/state): the website's token (role reporter) and everyone above
  POST   /servers/{id}/race-control/pit         {car_id, name, pit}: the hub of a car says whether it is in the pit lane (a VSC exemption; reporter and above)
  POST   /servers/{id}/race-control             {flag: green|yellow|vsc|fcy|white|checkered, ...}: the director (steward). A yellow is only a flag; a `vsc` or `fcy` needs pace_kmh and may carry
                                                grace_s (20), tolerance_kmh (5) and penalty {kind: none|pits, laps}
  POST   /servers/{id}/race-control/penalties   {car_id, laps}: a drive-through to serve, the game enforces it (steward)
  DELETE /servers/{id}/race-control/penalties/{pid}
  POST/PATCH /servers/{id}/race-control/nascar {total, stages?: cumulative ends, shares?: positive weights, next_delay_min?: integer 0..60} (steward): arm; total=0 disarms.
An armed engine accepts distance changes through set_total; explicit stages reject distance changes. Stage definitions stay frozen otherwise.
The NEXT_SESSION delay starts at 10 min and can be edited atomically with total, armed or disarmed; omitted/null keeps it. The same total allows a delay-only edit even after the finish.
The leaderboard maximum feeds NASCAR; director flags feed it once. Engine flags never reenter it. At the finish connected cars cross once more
(the triggering car has already crossed), or 90 s elapse; the monitor calls done and captures the delay in next_at (0 sends NEXT_SESSION immediately, once).
Later delay edits do not move an existing next_at. race-state adds nascar (snapshot with next_delay_min, or null) and always nascar_next_delay_min at the root.
Everything is in memory in one process; session_info preserves the engine, new_session resets it (a race with a preset's nascar block arms a fresh engine). Every state change raises rev.
Each change is also said on screen: `notices` in the race state (the hub draws them; there is no chat line any more). The state is in memory and goes with the server or the session. `monitor_forever` (started by the app's
lifespan) watches a VSC: it samples the speeds and judges the drivers when the grace ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app import supervisor
from app.auth import CurrentUser, require, require_any
from app.config import settings
from app.db import SessionDep, engine
from app.models import GiveBackDue, LeagueMember, NeutralDue, Server
from app.results import FINISH_SUFFIX, STAGES_PENDING, STAGES_SUFFIX, claim_orphan, pending_is_unclaimed
from app.live import acsp, fieldrec, raceaudit
from app.live.nascar import CAUTION, DRAINING, MAX_SHARE, MAX_SHARES, MAX_TOTAL
from app.live.racecontrol import RaceControl, GRACE_DEFAULT, GRACE_MAX, GRACE_MIN, LAPS_MAX, NEXT_SESSION_DELAY, PACE_CAR_KMH, PACE_MAX, PACE_MIN, RACE_TYPE, TOL_DEFAULT, TOL_MAX, _lock, flag_rules_of, flag_rules_peek, flag_rules_stale, cut_rule_of, drs_rule_of, drs_rule_peek, drs_rule_stale, flag_modes_of, flag_modes_peek, flag_modes_stale, give_back_rule_of, give_back_rule_peek, give_back_rule_stale, speed_kmh, state_of, tick
from app.servers import _get

log = logging.getLogger("acmanager.racecontrol")
# the hub's token is the lowest role: it reads the state and reports its pit-lane status, nothing else of the manager
state_router = APIRouter(prefix="/servers/{server_id}", tags=["race-control"], dependencies=[Depends(require_any("reporter", "driver", "steward", "admin"))])
control_router = APIRouter(prefix="/servers/{server_id}/race-control", tags=["race-control"], dependencies=[Depends(require("steward"))])

SAY = {"green": "BANDERA VERDE: se reanuda la carrera", "yellow": "BANDERA AMARILLA: sin adelantar", "white": "BANDERA BLANCA: última vuelta", "checkered": "BANDERA DE CUADROS"}
# the flags a director shows by hand to one driver (`car_id`) or to everybody: only a notice on the hub's screen, they change no rule or penalty (the website gives each its banner by text: OPR WP/banner_i18n.py)
FLAG_NOTICES = {"blue": "BANDERA AZUL: deja pasar al más rápido", "bw-warning": "BANDERA NEGRA Y BLANCA: advertencia de conducta",
                "meatball": "BANDERA NEGRA CON DISCO NARANJA: problema mecánico, entra a boxes", "black": "BANDERA NEGRA: entra a boxes",
                "yellow-double": "DOBLE AMARILLA: peligro en pista, prepárate para parar", "red": "BANDERA ROJA: vuelve a boxes",
                "track-limits": "LÍMITES DE PISTA: aviso por salirte de la pista"}
PER_DRIVER = ("blue", "bw-warning", "meatball", "black", "track-limits")   # these are for somebody: a flag to everybody would be a mistake
MONITOR_EVERY = 0.25   # s; ACSP positions arrive about every 0.2 s


class PenaltyCfg(BaseModel):
    kind: Literal["none", "pits"] = "pits"
    laps: int = Field(default=3, ge=1, le=LAPS_MAX)
    warnings: int = Field(default=2, ge=1, le=5)      # a VSC by sectors: red sectors in a row that make a breach (the first one: the drive-through; as many more: the severe level)
    severe_s: int = Field(default=10, ge=0, le=60)    # the severe penalty in seconds added to the race result (a proposal for the director); 0 = none. The FCY uses it for who did not respect the delta when the green comes


class FlagIn(BaseModel):
    flag: Literal["green", "yellow", "vsc", "fcy", "white", "checkered"]
    pace_kmh: int | None = Field(default=None, ge=PACE_MIN, le=PACE_MAX)   # a VSC only (a yellow is only a flag)
    grace_s: int = Field(default=GRACE_DEFAULT, ge=GRACE_MIN, le=GRACE_MAX)   # a VSC: seconds every driver gets to slow down by themselves
    tolerance_kmh: int = Field(default=TOL_DEFAULT, ge=0, le=TOL_MAX)         # a VSC: km/h over the pace that are forgiven when the grace ends
    penalty: PenaltyCfg = PenaltyCfg()                                        # a VSC: what whoever is still over gets
    mode: Literal["fixed", "delta"] = "delta"                                  # VSC and FCY are judged by delta (the default); `fixed` is only the old single pace limit, kept for an API client that asks for it


class PenaltyIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    laps: int = Field(default=3, ge=1, le=LAPS_MAX)


class PitIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    name: str = Field(max_length=80)
    pit: bool


def _live(server_id: int):
    inst = supervisor.get(server_id)
    return inst if inst and inst.running and inst.acsp else None


def _name(inst, car_id: int) -> str:
    return inst.acsp.cars.get(car_id, {}).get("driver_name") or f"#{car_id}"


def caution_tick(now: float) -> None:
    """The caution procedures of every running server (nobody passes, the free pass lost to the pits). Runs in the monitor, on the event loop that also feeds the field."""
    for inst in supervisor.live():
        rc = state_of(inst.server_id, True, inst.acsp.session)
        if not rc or not rc.caution:
            continue
        speeds = {i: s for i, c in inst.acsp.cars.items() if (s := speed_kmh(c)) is not None}
        actions = rc.caution_tick(inst.acsp.field.snapshot(), speeds, now)
        if actions:
            nascar_actions(inst.server_id, rc, actions)


def _due_row(inst, car: int, creditors: list[int], seconds: int, now: float, by: str = "auto", owner: str | None = None) -> dict | None:
    """What is saved for a driver who did not give the places back: None when there is no driver to write it for (the car has no Steam ID, or the slot changed hands since the obligation opened)."""
    info = inst.acsp.cars.get(car) or {}
    guid = str(owner or info.get("driver_guid") or "")
    if not guid or (owner and info.get("driver_guid") and str(info["driver_guid"]) != owner):
        log.warning("give-back: car %s has no Steam ID, or its driver changed: no penalty written", car)
        return None
    return {"server_id": inst.server_id, "driver_guid": guid, "driver_name": info.get("driver_name") or "", "ts": now, "seconds": seconds, "created_by": by,
            "creditors": ", ".join(_name(inst, b) for b in creditors)}


def _due_notices(inst, rc, car: int, seconds: int) -> None:
    _say(rc, f"{_name(inst, car)}: no devolvió la posición · +{seconds} s", "penalty")
    _say(rc, f"PENALIZACIÓN +{seconds} s: ventaja fuera de pista", "penalty", car)


_unsaved_dues: list[dict] = []   # penalties that fell due and could not be written yet: tried again at every tick
MAX_UNSAVED_DUES = 1000


def _flag_messages(inst, rc) -> None:
    """Tell the drivers what the flags cost them (offences) and who they must give a place back to (a yellow). The discs, the black and the red flags are not notices: the hub draws them from its own state."""
    for o in rc.take_flag_messages():
        car, kind, step = o["car"], o.get("kind"), o.get("step")
        if o["msg"] == "yellow_open" and o["live"]:
            names = ", ".join(_name(inst, c) for c in o["passed"])
            secs = o["seconds"]
            _say(rc, f"ADELANTASTE BAJO AMARILLA: devuelve la posición a {names} · {secs} s", "flag", car)
        elif o["msg"] != "offence":
            continue
        elif kind == "blue" and step == 0:
            laps = o["laps"]
            _say(rc, f"PENALIZACIÓN: no cediste el paso a la bandera azul · drive-through ({laps} vueltas)", "penalty", car)
        elif kind == "blue":
            _say(rc, f"PENALIZACIÓN: no cediste el paso a la bandera azul · +{step} s", "penalty", car)
        elif kind == "yellow" and step == 0:
            laps = o["laps"]
            _say(rc, f"PENALIZACIÓN: adelantar bajo bandera amarilla · drive-through ({laps} vueltas)", "penalty", car)
        elif kind == "yellow":
            _say(rc, f"PENALIZACIÓN: adelantar bajo bandera amarilla · +{step} s", "penalty", car)
        elif kind == "black":
            _say(rc, "DESCALIFICADO: ignoraste la bandera negra", "penalty", car)


def blue_stale_servers() -> list[int]:
    """Servers in a race whose cached blue-flag rule has to be read again (a worker thread does it: never the event loop)."""
    return [i.server_id for i in supervisor.live() if _sanctioned(i) and flag_rules_stale(i.server_id)]


def save_flag_dues() -> None:
    from app.live.racecontrol import acknowledge_flag, pending_flag_dues, pending_flag_served
    from app.stewards.auto import stage_flag_dues
    served, rows = pending_flag_served(), pending_flag_dues()   # (served first: its row was queued before it, so the rows read next hold it unless it is already saved)
    if not rows and not served:
        return
    with Session(engine) as s:
        stage_flag_dues(s, rows, served)
        s.commit()
    acknowledge_flag(rows, served)
    for row in rows:
        raceaudit.record(row["server_id"], "flag_due_saved", due=row, executed=True)


def drs_stale_servers() -> list[int]:
    """Servers in a race whose cached DRS rule has to be read again (the monitor does it in a worker thread: never on the event loop)."""
    return [i.server_id for i in supervisor.live() if (i.acsp.session or {}).get("session_type") == RACE_TYPE and drs_rule_stale(i.server_id)]


def drs_tick(now: float) -> None:
    """The DRS state of every race that has the rule on: the leader's laps and how wet the track is go in, the state the hubs read comes out (RaceControl.drs_tick). Event loop, no database."""
    for inst in supervisor.live():
        session = inst.acsp.session
        if (session or {}).get("session_type") != RACE_TYPE:
            continue
        rule = drs_rule_peek(inst.server_id)
        rc = state_of(inst.server_id, True, session, create=bool(rule))
        if not rc:
            continue
        laps = max((s.completed_laps if s.completed_laps is not None else int(max(s.progress, 0)) for c, s in inst.acsp.field.samples().items() if c != rc.pace_car), default=0)
        rc.drs_tick(rule, now, laps, inst.director.signal if inst.director else None)


def give_back_stale_servers() -> list[int]:
    """Servers in a race whose cached rule has to be read again (done by the monitor in a worker thread: never on the event loop)."""
    return [i.server_id for i in supervisor.live() if (i.acsp.session or {}).get("session_type") == RACE_TYPE and give_back_rule_stale(i.server_id)]


SANCTIONED = (1, 2, RACE_TYPE)   # practice, qualifying and race: the rules sanction in every session (the blue flag, the DRS and the give-back are race matters, see RaceControl._is_race)


def _sanctioned(inst) -> bool:
    return (inst.acsp.session or {}).get("session_type") in SANCTIONED


def flag_modes_stale_servers() -> list[int]:
    """Servers in a race whose cached modes of the automatic flags have to be read again (in a worker thread, like the give-back rule)."""
    return [i.server_id for i in supervisor.live() if _sanctioned(i) and flag_modes_stale(i.server_id)]


def give_back_tick(now: float) -> list[dict]:
    """The advantage gained off the track of every race that has the league's rule on: reads the cars (positions, speeds), says who is no longer in debt, and returns the penalties that fell due (rows
    for `GiveBackDue`, saved off the event loop by the caller). Runs in the monitor, on the event loop that also feeds the field."""
    rows: list[dict] = []
    for inst in supervisor.live():
        session = inst.acsp.session
        if (session or {}).get("session_type") != RACE_TYPE:
            continue
        rule = give_back_rule_peek(inst.server_id)
        rc = state_of(inst.server_id, True, session, create=bool(rule))
        if not rc:
            continue
        rc.gb_rule = rule
        speeds = {i: s for i, c in inst.acsp.cars.items() if (s := speed_kmh(c)) is not None}
        events = rc.give_back_tick(inst.acsp.field.snapshot(), speeds, now)
        rc.gb_names = {c: info.get("driver_name") or "" for c, info in inst.acsp.cars.items()}
        for ob in rc.give_back_view(now)["obligations"]:   # (a slot that changed hands: the new driver owes nothing)
            owner, cur = rc.gb_owner.get(ob["id"]), (inst.acsp.cars.get(ob["car"]) or {}).get("driver_guid")
            if owner and cur and str(cur) != owner:
                rc.give_back_forgive(ob["id"])
        for ev in events:
            ob = ev[1]
            if ev[0] == "open":
                guid = (inst.acsp.cars.get(ob["car"]) or {}).get("driver_guid")
                if guid:
                    rc.gb_owner[ob["id"]] = str(guid)
                log.info("give-back: server %s car %s owes the place to %s (until %ss)", inst.server_id, ob["car"], sorted(ob["creditors"]), rule["window_s"] if rule else "?")
                continue
            owner = rc.gb_owner.pop(ob["id"], None)
            if ev[0] == "cleared" and ev[2] == "returned":
                _say(rc, "POSICIÓN DEVUELTA ✓", "race", ob["car"])
            elif ev[0] == "due" and rule:
                row = _due_row(inst, ob["car"], sorted(ob["creditors"]), rule["seconds"], now, owner=owner)
                if row:
                    rows.append(row)
                    _due_notices(inst, rc, ob["car"], rule["seconds"])
    return rows


def save_dues(rows: list[dict]) -> None:
    """The penalties that fell due, written to the database (a worker thread: never the event loop). All or none."""
    with Session(engine) as s:
        for r in rows:
            s.add(GiveBackDue(**r))
        s.commit()
    for row in rows:
        raceaudit.record(row["server_id"], "give_back_due_saved", due=row, executed=True)


def neutral_rows_to_save() -> tuple[list[dict], list[dict]]:
    """(the VSC / FCY time penalties that can be written now, those that never can). Runs in the event loop (it reads the live cars): a due is written for the driver in the slot NOW only if the slot has the
    generation it had when the due was made; if the slot changed hands (or the server is gone) the due is dropped: it is never put on somebody else."""
    from app.live.racecontrol import pending_neutral_dues

    live = {i.server_id: i for i in supervisor.live()}
    save, drop = [], []
    for r in pending_neutral_dues():
        inst = live.get(r["server_id"])
        info = (inst.acsp.cars.get(r["car"]) or {}) if inst else {}
        guid = str(info.get("driver_guid") or "")
        if inst and guid and inst.acsp.field.generation.get(r["car"], 0) == r["generation"]:
            save.append({**r, "driver_guid": guid, "driver_name": info.get("driver_name") or ""})
        else:
            drop.append(r)
    return save, drop


def announce_neutral(rows: list[dict]) -> None:
    """On the screens, once each (after the row is saved): what the severe level of a VSC / FCY costs, seconds that go straight onto the race result."""
    for r in rows:
        inst = next((i for i in supervisor.live() if i.server_id == r["server_id"]), None)
        rc = state_of(r["server_id"], True, inst.acsp.session) if inst else None
        if not rc:
            continue
        who = _name(inst, r["car"])
        why = "sectores en rojo bajo VSC" if r["kind"] == "vsc" else "delta de FCY no respetado al volver a verde"
        _say(rc, f"{who}: {why} · +{r['seconds']} s al resultado", "penalty")
        _say(rc, f"SANCIÓN: +{r['seconds']} s al resultado · {why}", "penalty", r["car"])


def save_neutral_dues(rows: list[dict]) -> None:
    """The time penalties of VSC / FCY written to the database (a worker thread: never the event loop). A row that is already there (same server, flag, driver and level) is left as it is."""
    with Session(engine) as s:
        for r in rows:
            if s.exec(select(NeutralDue.id).where(NeutralDue.server_id == r["server_id"], NeutralDue.flag_id == r["flag_id"], NeutralDue.driver_guid == r["driver_guid"], NeutralDue.level == r["level"])).first():
                continue
            s.add(NeutralDue(server_id=r["server_id"], flag_id=r["flag_id"], driver_guid=r["driver_guid"], driver_name=r["driver_name"], ts=r["ts"], kind=r["kind"], level=r["level"], seconds=r["seconds"], reason=r["reason"]))
        s.commit()
    for r in rows:
        raceaudit.record(r["server_id"], "neutral_due_saved", due=r, executed=True)


def save_cut_dues() -> None:
    from app.live.racecontrol import pending_cut_dues, acknowledge_cut_dues, pending_cut_served, acknowledge_cut_served
    from app.stewards.auto import stage_cut_dues
    served = pending_cut_served()   # (first: a served key's row was queued before it, so the rows read next hold it unless it is already saved)
    rows = pending_cut_dues()
    if not rows and not served:
        return
    with Session(engine) as s:
        stage_cut_dues(s, rows, served)
        s.commit()
    acknowledge_cut_dues(rows)
    acknowledge_cut_served(served)
    for row in rows:
        raceaudit.record(row["server_id"], "cuts_due_saved", due=row, executed=True)


def save_drs_dues() -> None:
    from app.live.racecontrol import acknowledge_drs, pending_drs_dues, pending_drs_served
    from app.stewards.auto import stage_drs_dues
    served, rows = pending_drs_served(), pending_drs_dues()   # (served first: its row was queued before it, so the rows read next hold it unless it is already saved)
    if not rows and not served:
        return
    with Session(engine) as s:
        stage_drs_dues(s, rows, served)
        s.commit()
    acknowledge_drs(rows, served)
    for row in rows:
        raceaudit.record(row["server_id"], "drs_due_saved", due=row, executed=True)


def slot_of(cars: dict, steam_id: str) -> int | None:
    """Where the driver with that SteamID sits (None: not on the server)."""
    return next((i for i, c in cars.items() if steam_id and (c.get("driver_guid") or "") == steam_id), None)


def pace_car_check() -> None:
    """The pace car is a driver: its slot follows him when he connects or reconnects, and it is empty while he is away (he stays named, so he is the pace car again when he comes back)."""
    for inst in supervisor.live():
        want = getattr(inst, "pace_steam_id", None)   # the pace car the event / preset names: put once, then the director's choices stand
        rc = state_of(inst.server_id, True, inst.acsp.session, create=bool(want))
        if rc and want:
            inst.pace_steam_id = None
            rc.set_pace_car(slot_of(inst.acsp.cars, want), PACE_CAR_KMH, want)
        if rc and rc.pace_guid:
            rc.follow_pace_driver(slot_of(inst.acsp.cars, rc.pace_guid))


_NR: dict = {"t": -1e9, "guids": frozenset()}


def _non_racing_guids(now: float) -> frozenset:
    """SteamIDs of every league's cars that do not race (director, caster, pace car), read at most every 30 s."""
    if now - _NR["t"] > 30:
        try:
            with Session(engine) as db:
                _NR["guids"] = frozenset(db.exec(select(LeagueMember.guid).where(LeagueMember.non_racing)).all())
        except SQLAlchemyError:
            log.exception("non-racing roster")
        _NR["t"] = now
    return _NR["guids"]


def incident_tick(now: float) -> None:
    """Applied neutralization decisions, plus the existing NASCAR proposals."""
    from app.league import neutralization_rule_cached

    for inst in supervisor.live():
        rule = neutralization_rule_cached(inst.server_id) or {"mode": "off"}
        rc = state_of(inst.server_id, True, inst.acsp.session, create=rule.get("mode", "off") != "off" or fieldrec.enabled(inst.server_id))
        if not rc:
            continue
        if rc.neutralization_rule != rule:
            rc.audit("mode_change", previous=rc.neutralization_rule, rule=rule, executed=True)
        rc.neutralization_rule = rule
        rc.audit_guids = {i: c.get("driver_guid") for i, c in inst.acsp.cars.items()}
        out = _non_racing_guids(now)   # (the director and the caster are not racing: their stops are no incident)
        speeds = {i: s for i, c in inst.acsp.cars.items() if (s := speed_kmh(c)) is not None and (c.get("driver_guid") or "") not in out}
        rc.neutralization_excluded = {i for i, c in inst.acsp.cars.items() if (c.get("driver_guid") or "") in out}
        prop, actions = rc.incident_step(speeds, inst.acsp.field, now, connected=set(inst.acsp.cars))
        if prop:
            who = ", ".join(_name(inst, c) for c in prop["cars"])
            _say(rc, f"{prop['text']}: {who}" + (" · AMARILLA AUTOMÁTICA" if rc.auto_yellow else ""), "flag")
            nascar_actions(inst.server_id, rc, actions)


def autoflags_tick(now: float) -> None:
    """The blue flag and the danger warning of every server in a race, in SHADOW (decided and written down, shown to nobody). The director and the caster are not racing."""
    mono = time.monotonic()
    for inst in supervisor.live():
        if not _sanctioned(inst):
            continue
        rc = state_of(inst.server_id, True, inst.acsp.session, create=True)   # (a session always has its state: the judges of the flags need it; the blue flag needs the pace of its cars)
        if not rc:
            continue
        rc.rules = flag_rules_peek(inst.server_id)
        rc.audit_guids = {i: c.get("driver_guid") for i, c in inst.acsp.cars.items()}   # (who is in each slot NOW: a sanction needs a name, not only a number)
        out = _non_racing_guids(now)
        cars = {i: c for i, c in inst.acsp.cars.items() if (c.get("driver_guid") or "") not in out}
        speeds = {i: s for i, c in cars.items() if (s := speed_kmh(c)) is not None}
        samples = {c: s for c, s in inst.acsp.field.samples().items() if c in cars}
        modes = flag_modes_peek(inst.server_id) or {k: "live" for k in ("blue", "danger", "yellow", "meatball", "black", "red")}
        if rc.flag_modes != modes:
            rc.audit("flag_mode_change", previous=rc.flag_modes, modes=modes, executed=True)
        rc.flag_modes = modes   # (no fresh reading of the league: nothing live)
        rc.autoflags_tick(samples, speeds, inst.acsp.length.metres(), mono, now)
        rc.yellow_tick(samples, speeds, mono, now)
        _flag_messages(inst, rc)
        models = {c: (info.get("car_model") or "") for c, info in cars.items()}
        for d in rc.pass_tick(samples, speeds, mono, now):
            if not d["applied"]:
                continue   # (the director's kill switch: only logged)
            who = _name(inst, d["car"])
            if d["kind"] == "pass_open":
                names = ", ".join(_name(inst, c) for c in d["passed"])
                _say(rc, f"{who}: adelantó bajo {d['flag'].upper()} · devuelve la posición a {names}", "flag")
                _say(rc, f"ADELANTASTE BAJO {d['flag'].upper()}: devuelve la posición a {names} · {d['seconds']} s", "flag", d["car"])
            elif d["kind"] == "pass_cleared":
                _say(rc, f"{who}: devolvió la posición", "info")
            else:
                pen = d["pen"]
                _say(rc, f"{who}: no devolvió la posición bajo {d['flag'].upper()}" + (f" · DRIVE-THROUGH {pen['laps']} v." if pen["kind"] == "pits" else ""), "penalty")
                if pen["kind"] == "pits":
                    _say(rc, f"PENALIZACIÓN: adelantar bajo {d['flag'].upper()} · drive-through ({pen['laps']} vueltas)", "penalty", d["car"])
        for d in rc.pace_tick(samples, speeds, mono, now, models):
            if d.get("level") == 2:
                continue   # (the severe level is a time penalty: announced when it is saved, `announce_neutral`)
            if d["applied"]:   # (what the judge did, as it was when it did it: not re-read after the lock)
                pen = d["pen"]
                what = f"el delta ({d['delta_s']:+.2f} s)" if d["kind"] == "delta" else f"el ritmo ({d['speed_kmh']} km/h)"
                _say(rc, f"{_name(inst, d['car'])}: no respetó {what}" + (f" · DRIVE-THROUGH {pen['laps']} v." if pen["kind"] == "pits" else ""), "penalty")
                if pen["kind"] == "pits":
                    _say(rc, f"PENALIZACIÓN: {'delta' if d['kind'] == 'delta' else 'ritmo'} de {d['flag'].upper()} no respetado · drive-through ({pen['laps']} vueltas)", "penalty", d["car"])


def _say(rc, text: str, kind: str = "info", to: int = -1, hold: str | None = None) -> None:
    """What used to go to the chat now goes on the screen of every hub (race-state `notices`): it arrives with the flag itself, so it can never come before it."""
    rc.notify(text, kind, to, hold=hold)


@state_router.get("/race-state")
def race_state(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    inst = _live(server_id)
    rc = state_of(server_id, bool(inst), inst.acsp.session if inst else None)
    if inst and _sanctioned(inst):
        cut_rule_of(server_id)   # (refreshes the cached rules here, in a request thread: the snapshot and the monitor never touch the database)
        give_back_rule_of(server_id)
    server = _get(sess, server_id)
    if rc:
        return {**rc.snapshot(), "nascar_armed": armed_view(inst, server)}
    race = bool(inst) and _sanctioned(inst)   # (nothing has happened yet in this session, but the hub's counter already needs the rule: "CUTS 0/5")
    rule = cut_rule_of(server_id) if race else None
    return {"nascar_armed": armed_view(inst, server), "cuts": {}, "cuts_rule": {k: rule[k] for k in ("cap", "max", "per", "step")} if rule else None, "drs": None, "obligations": [], "give_back_reviews": [], "rev": 0, "ts": 0, "running": bool(inst), "flag": "green", "red_ref": None, "pace_kmh": None, "penalties": [], "notices": [], "vsc_id": 0, "grace_left": 0,
                                     "tolerance_kmh": None, "vsc_penalty": None, "violations": [], "exempt": [], "nascar": None, "caution": None, "caution_eta": None, "proposal": None, "auto_yellow": True, "pacecar": None, "pace_steam_id": "", "nascar_next_delay_min": NEXT_SESSION_DELAY // 60}


@control_router.get("/autoflags")
def autoflags_view(server_id: int, sess: SessionDep) -> dict:
    """What the automatic blue flag and danger warning WOULD show (shadow: nobody sees them), and their last decisions: for the steward to judge them against the race before they go live."""
    _get(sess, server_id)
    inst = _live(server_id)
    rc = state_of(server_id, bool(inst), inst.acsp.session if inst else None)
    return rc.autoflags_view() if rc else {"blue": [], "danger": [], "unknown_length": False, "log": [], "pace": [], "auto_off": []}


@state_router.get("/race-state/auto")
def race_state_auto(server_id: int, slot: int, name: str, sess: SessionDep) -> dict:
    """The automatic flags (blue, danger) of ONE driver's screen: `{auto: {...} | null}`. The hub asks with its slot and its name (409 when that car is not that driver, like /pit); a driver sees only
    his own. Null unless the league set the type to `live`, the director has not switched it off and the look is fresh."""
    _get(sess, server_id)
    inst = _live(server_id)
    car = inst.acsp.cars.get(slot) if inst else None
    if not car or (car.get("driver_name") or "").strip().lower() != name.strip().lower():
        raise HTTPException(409, "that car is not that driver")
    generation = inst.acsp.field.generation.get(slot, 0)   # (taken WITH the name check: if the slot changes hands during the database read below, the new driver's flags do not go to this request)
    modes = flag_modes_of(server_id) or {}   # (refreshes the cached modes here, in a request thread; a failed reading shows nothing live)
    rc = state_of(server_id, True, inst.acsp.session)
    model = car.get("car_model") or ""
    return {"auto": rc.auto_for(slot, generation, time.monotonic(), modes, model) if rc else None, "live": rc.auto_live(modes) if rc else []}


class AutoOffIn(BaseModel):
    kind: Literal["blue", "danger", "pace", "pass", "neutralization"]
    off: bool


class PaceConfigIn(BaseModel):
    tolerance_kmh: int | None = Field(default=None, ge=0, le=TOL_MAX)
    penalty: PenaltyCfg | None = None


@control_router.post("/vsc-config")
def vsc_config(server_id: int, body: PaceConfigIn, sess: SessionDep) -> dict:
    """The director changes the tolerance and / or the penalty (drive-through laps) of the VSC / FCY being run; it counts from now on."""
    inst, _ = _control(server_id, sess)
    with _lock:
        rc = state_of(server_id, True, inst.acsp.session, create=True)
        try:
            rc.set_pace_config(body.tolerance_kmh, body.penalty.model_dump() if body.penalty else None)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"tolerance_kmh": rc.tolerance_kmh, "penalty": dict(rc.vsc_penalty)}


@control_router.post("/autoflags")
def autoflags_off(server_id: int, body: AutoOffIn, sess: SessionDep) -> dict:
    """The director's kill switch for one automatic flag: `off` forces it off at once (the screens lose it with the next question); it outlives the session."""
    inst, _ = _control(server_id, sess)
    with _lock:   # (the state is looked up INSIDE the lock: a session change in between would copy the switch to a new state and leave this one, which is dropped, changed)
        rc = state_of(server_id, True, inst.acsp.session, create=True)
        (rc.auto_off.add if body.off else rc.auto_off.discard)(body.kind)
        rc.audit("kill_switch", switch=body.kind, off=body.off, executed=True)
        return {"auto_off": sorted(rc.auto_off)}


@state_router.post("/race-control/pit", status_code=204)
def report_pit(server_id: int, body: PitIn, sess: SessionDep) -> None:
    """The hub of a car says whether it is in the pit lane (only used as a VSC exemption). 409 when that car is not that driver, like the track-limit reports."""
    _get(sess, server_id)
    inst = _live(server_id)
    car = inst.acsp.cars.get(body.car_id) if inst else None
    if not car or (car.get("driver_name") or "").strip().lower() != body.name.strip().lower():
        raise HTTPException(409, "that car is not that driver")
    rc = state_of(server_id, True, inst.acsp.session)
    if rc:
        rc.report_pit(body.car_id, body.pit)
        if body.pit and rc.pace_car_in and body.car_id == rc.pace_car:
            auto_green(server_id, rc, body.car_id)   # (alone on the track the pace car IS the restart: its arrival in the pits brings the green)


class DrsUseIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    name: str = Field(max_length=80)


@state_router.post("/race-control/drs-use", status_code=204)
def report_drs_use(server_id: int, body: DrsUseIn, sess: SessionDep) -> None:
    """The hub of a car says it is using DRS. Client evidence (like the cut counter): the manager decides with its own state (is DRS off now, was this driver already sanctioned in this period); 409 when that car is not that driver."""
    _get(sess, server_id)
    inst = _live(server_id)
    car = inst.acsp.cars.get(body.car_id) if inst else None
    if not car or (car.get("driver_name") or "").strip().lower() != body.name.strip().lower():
        raise HTTPException(409, "that car is not that driver")
    rc = state_of(server_id, True, inst.acsp.session)
    verdict = rc.drs_use(body.car_id, str(car.get("driver_guid") or ""), time.time()) if rc else None
    if verdict:
        laps = verdict["laps"]
        _say(rc, f"PENALIZACIÓN: DRS con el DRS inactivo · drive-through ({laps} vueltas)", "penalty", body.car_id)


class DamageIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    name: str = Field(max_length=80)
    level: int = Field(ge=0, le=100)


@state_router.post("/race-control/damage", status_code=204)
def report_damage(server_id: int, body: DamageIn, sess: SessionDep) -> None:
    """The hub of a car says how damaged it is (0-100 %, read by its own game): client evidence for the orange disc (rule `meatball`). 409 when that car is not that driver, like the other hub reports."""
    _get(sess, server_id)
    inst = _live(server_id)
    car = inst.acsp.cars.get(body.car_id) if inst else None
    if not car or (car.get("driver_name") or "").strip().lower() != body.name.strip().lower():
        raise HTTPException(409, "that car is not that driver")
    rc = state_of(server_id, True, inst.acsp.session)
    if rc:
        rc.damage_report(body.car_id, body.level, time.time())


def _control(server_id: int, sess: SessionDep):
    server = _get(sess, server_id)
    inst = _live(server_id)
    if not inst:
        raise HTTPException(409, "server not running or ACSP plugin not connected")
    rc = state_of(server_id, True, inst.acsp.session, create=True)
    rc.audit_guids = {i: c.get("driver_guid") for i, c in inst.acsp.cars.items()}
    rc.audit_revision = (server.session or {}).get("rulebook_revision")
    return inst, rc


def last_lap_bytes(session: dict, leader_laps: int, elapsed_ms: int, restore: dict | None = None) -> tuple[bytes, dict] | None:
    """The director's white flag is the last lap, in a laps race or a timed one: acServer is told (SET_SESSION_INFO) to end the race after the lap the leader is on. A laps race: the distance becomes
    the leader's laps + 1; a timed one: the time is cut to what has elapsed (the leader's next crossing is then the chequered). Returns (packet, what to put back); `restore` = the originals: the packet
    that gives them back (yellow or green after the white flag). None when the session is not a race or has nothing to cut."""
    if session.get("session_type") != RACE_TYPE or session.get("session_index") is None:
        return None
    laps, minutes = int(session.get("laps") or 0), int(session.get("time_min") or 0)
    if restore is not None:
        laps, minutes = restore["laps"], restore["time_min"]
    elif laps:
        if leader_laps + 1 >= laps:
            return None   # (already the last lap)
        laps = leader_laps + 1
    elif minutes:
        minutes = max(1, elapsed_ms // 60000)
    else:
        return None
    pkt = acsp.encode_set_session_info(session["session_index"], session.get("name", ""), RACE_TYPE, laps, minutes, int(session.get("wait_time") or 0))
    return pkt, {"laps": int(session.get("laps") or 0), "time_min": int(session.get("time_min") or 0)}


def apply_last_lap(inst, rc, flag: str) -> None:
    """A director's flag: white = this is the last lap (any race); yellow/green/VSC/FCY after it = the race goes on, the original distance or time is given back. NASCAR moves its own distance."""
    ses = inst.acsp.session or {}
    if ses.get("session_type") != RACE_TYPE:
        return
    leader = max(0, rc.leader_laps_seen - rc.lap_base)
    if rc.nascar:
        if flag == "white" and rc.last_lap_orig is None and rc.nascar.phase == "racing" and rc.nascar.laps + 1 < rc.nascar.total:
            total = rc.nascar.total
            try:
                rc.nascar.set_total(max(2, rc.nascar.laps + 1))
                rc.last_lap_orig = {"total": total}   # (only if the distance moved: explicit stage ends refuse it and the engine keeps its own white flag)
            except ValueError:
                log.info("white flag: this NASCAR race has explicit stage ends, its distance stays")
        elif flag != "white" and rc.last_lap_orig and flag != "checkered":
            total, rc.last_lap_orig = rc.last_lap_orig["total"], None
            rc.nascar.set_total(total)
        return
    if flag == "white" and rc.last_lap_orig is None:
        out = last_lap_bytes(ses, leader, inst.acsp.board.elapsed_ms())
        if out:
            rc.last_lap_orig = out[1]
            inst.acsp.send(out[0])
    elif flag in ("yellow", "green", "vsc", "fcy") and rc.last_lap_orig:
        out = last_lap_bytes(ses, leader, 0, restore=rc.last_lap_orig)
        rc.last_lap_orig = None
        if out:
            inst.acsp.send(out[0])


@control_router.post("")
def set_flag(server_id: int, body: FlagIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    try:
        actions = rc.set_flag(body.flag, body.pace_kmh, body.grace_s, body.tolerance_kmh, body.penalty.model_dump(), field=inst.acsp.field, mode=body.mode)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.flag == "vsc" and rc.vsc_mode == "delta":   # a VSC by delta is time per sector: it asks for no speed
        pen = f" · si baja del límite: drive-through de {body.penalty.laps} vueltas" if body.penalty.kind == "pits" else ""
        _say(rc, f"VIRTUAL SAFETY CAR: reduce el ritmo en {body.grace_s} s · mantén el delta positivo{pen}", "flag")
    elif body.flag in ("vsc", "fcy"):
        pen = f" · si sigues por encima: drive-through de {body.penalty.laps} vueltas" if body.penalty.kind == "pits" else ""
        name = "VIRTUAL SAFETY CAR" if body.flag == "vsc" else "FULL COURSE YELLOW"
        _say(rc, f"{name}: baja a {body.pace_kmh} km/h en {body.grace_s} s{pen}", "flag")
    else:
        _say(rc, SAY[body.flag], "flag")
    if body.flag == "checkered":
        laps = max(0, rc.leader_laps_seen - (rc.lap_base if rc.nascar else 0))
        if laps > 0:
            _mark_finish(server_id, laps)
    with _lock:
        apply_last_lap(inst, rc, body.flag)
    nascar_actions(server_id, rc, actions)   # (after the director's own notice: a white flag the engine adds comes last)
    return rc.snapshot()


class FlagNoticeIn(BaseModel):
    kind: Literal["blue", "bw-warning", "meatball", "black", "yellow-double", "red", "track-limits"]
    car_id: int | None = Field(default=None, ge=0, le=255)   # None = every driver (yellow-double and red only)
    on: bool = True   # red only: False withdraws it (the red flag lives next to the track flag, no flag withdraws it)


@control_router.post("/flag-notice")
def flag_notice(server_id: int, body: FlagNoticeIn, sess: SessionDep) -> dict:
    """A flag shown by hand: blue, black-and-white, black, orange disc and track limits go to ONE driver; the double yellow and the red to everybody. Only a notice: no penalty is set (the black flag
    here does not make the game's own black flag, and the red does not stop the session)."""
    if body.kind in PER_DRIVER and body.car_id is None:
        raise HTTPException(422, "esta bandera es para un piloto: indica car_id")
    _, rc = _control(server_id, sess)
    if body.kind == "red":
        inst = _live(server_id)
        rc.set_red(body.on, field=inst.acsp.field if inst else None)   # (the cars have `pit_s` seconds to be in the pit lane; it stays beside any track flag until withdrawn)
        if not body.on:
            rc.drop_hold("red-order")
            return rc.snapshot()
        if rc.red_ref and inst:
            top = " · ".join(f"{i + 1} {_name(inst, c)}" for i, c in enumerate(rc.red_ref["order"][:10]))
            _say(rc, f"ORDEN DE REINICIO (vuelta {rc.red_ref['lap']}): {top}", "race", hold="red-order")
    _say(rc, FLAG_NOTICES[body.kind], "flag", -1 if body.car_id is None else body.car_id)
    log.info("flag-notice %s to %s", body.kind, body.car_id)
    return rc.snapshot()


class NascarIn(BaseModel):
    total: int = Field(ge=0, le=MAX_TOTAL)
    stages: list[int] | None = Field(default=None, min_length=1)
    shares: list[Annotated[float, Field(gt=0, le=MAX_SHARE, allow_inf_nan=False)]] | None = Field(default=None, min_length=1, max_length=MAX_SHARES)
    next_delay_min: int | None = Field(default=None, ge=0, le=60)


def armed_view(inst, server) -> dict | None:
    """The NASCAR arming of a SERVER (not of a session): what the director armed, kept for the races until the session is stopped. {total, shares} or None. The engine itself only exists in a race;
    this is what survives practice, qualifying and restarts (the memory of the running instance, which is the saved session of the server as of the last apply or arming)."""
    cfg = getattr(inst, "nascar_config", None) if inst is not None else (server.session or {}).get("nascar")
    return {"total": cfg["total"], "shares": list(cfg["shares"]) if cfg.get("shares") is not None else None} if cfg else None   # (a preset may leave the shares to the default)


_arm_lock = threading.Lock()   # one arming at a time: the database and the instance's memory must end with the same one when two directors press at once


def _remember_nascar(sess: Session, server_id: int, inst, remembered: dict | None) -> None:
    """Keep the arming for the next race and for every later one until the session is stopped (the in-memory engine is reset by every new_session; the instance arms a fresh one from this)."""
    with _arm_lock:
        server = _get(sess, server_id)
        saved = dict(server.session or {})   # a new dict: a JSON column does not notice in-place changes
        saved.pop("nascar", None)
        if remembered:
            saved["nascar"] = remembered
        server.session = saved or None
        sess.add(server)
        try:
            sess.commit()
        except SQLAlchemyError as e:   # the live engine did change (that is what was asked): say only the memory for later races failed
            sess.rollback()
            log.warning("NASCAR could not be saved for server %s: %s", server_id, e)
            raise HTTPException(503, "NASCAR quedó armado en esta carrera, pero no se pudo guardar para las siguientes") from e
        inst.nascar_config = remembered


@control_router.post("/nascar")
@control_router.patch("/nascar")
def configure_nascar(server_id: int, body: NascarIn, sess: SessionDep) -> dict:
    """Arm (or, with total 0, disarm) NASCAR on the server. In a RACE the engine starts now (and is remembered for the races that follow); in practice or qualifying nothing runs: it is armed for the
    next race only, and the arming stays on the server through practice, qualifying and restarts until the session is stopped (`armed_view`, race-state `nascar_armed`)."""
    inst, rc = _control(server_id, sess)
    shares = tuple(body.shares) if body.shares is not None else None
    if (inst.acsp.session or {}).get("session_type") != RACE_TYPE:
        if body.stages is not None:
            raise HTTPException(422, "las etapas explícitas solo se pueden armar dentro de una carrera")
        was = armed_view(inst, None)
        if shares is None and was and was["shares"] is not None:   # as with a live engine: a change of distance alone keeps the shares that were armed
            shares = tuple(was["shares"])
        try:
            trial = RaceControl(None)   # a throwaway engine: the same checks as a live one, nothing runs
            trial.configure_nascar(body.total, None, shares, body.next_delay_min)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        remembered = {"total": trial.nascar.total, "shares": list(trial.nascar.shares)} if trial.nascar else None
        _remember_nascar(sess, server_id, inst, remembered)
        if (inst.acsp.session or {}).get("session_type") == RACE_TYPE:   # a race began while this was being saved: its engine was armed from the OLD memory, so arm it now
            return configure_nascar(server_id, body, sess)
        return {**rc.snapshot(), "nascar_armed": armed_view(inst, None)}   # (the wait after the finish, next_delay_min, is a live engine's: it is set when the race has started)
    try:
        actions = rc.configure_nascar(body.total, body.stages, shares, body.next_delay_min)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    nascar_actions(server_id, rc, actions)
    # Remember it for the next race (the in-memory engine is reset by every new_session). Explicit `stages` are API-only and not part of a preset.
    if not rc.nascar:
        remembered = None
    elif not rc.nascar.fixed_ends:
        remembered = {"total": rc.nascar.total, "shares": list(rc.nascar.shares)}
    else:
        return {**rc.snapshot(), "nascar_armed": armed_view(inst, None)}
    _remember_nascar(sess, server_id, inst, remembered)
    return {**rc.snapshot(), "nascar_armed": armed_view(inst, None)}


def disarm_when_stopped(server_id: int, was) -> None:
    """The session was stopped (the director's stop, the end of an event, the idle stop): the arming goes with it, but only the arming that INSTANCE had (`was`): if a newer apply has saved another one
    meanwhile it is the next session's and stays. A restart for an apply is NOT this (it writes the new session first)."""
    if not was:
        return
    with _arm_lock, Session(engine) as sess:
        server = sess.get(Server, server_id)
        if server is not None and (server.session or {}).get("nascar") == was:
            saved = dict(server.session)
            saved.pop("nascar", None)
            server.session = saved or None
            sess.add(server)
            sess.commit()


supervisor.stopped_hooks.append(disarm_when_stopped)


PITS_OPEN = "BOXES ABIERTOS: aprovecha para parar"


def nascar_actions(server_id: int, rc, actions: list[tuple], crossing: int | None = None) -> None:
    inst = _live(server_id)
    if not inst:
        return
    queue = sorted(actions, key=lambda a: a[0] == "say")   # flags and the finish first, the messages after: the notice never shows before the flag it announces
    while queue:
        action = queue.pop(0)
        with _lock:
            if state_of(server_id, True, inst.acsp.session) is not rc or not rc.nascar:
                return
            if action[0] == "flag" and rc.nascar.flag == action[1]:
                queue += rc.set_flag(action[1], source="nascar", field=inst.acsp.field)   # (a stage-break yellow starts a caution: its free pass and order notices come back)
            elif action[0] == "finish" and rc.nascar.phase == DRAINING and rc.finish_wait is None:
                rc.finish_wait = {i for i in inst.acsp.cars if i != crossing}
                rc.finish_deadline = time.time() + 90
                rc._bump()
        if action[0] == "say":
            _say(rc, action[1], "race")
        elif action[0] == "finish" and crossing is not None:   # the checkered came with the lap of the first car to complete the distance: that car is the winner (AC ranks by laps, then by time)
            log.info("winner: car %s (%s)", crossing, _name(inst, crossing))
            _mark_finish(server_id, rc.leader_laps_seen)
            rc.notify(f"GANADOR: {_name(inst, crossing)}", "winner", hold="winner")   # (stays until the session restarts: state_of forgets it with the session)
        elif action[0] == "caution":
            _say(rc, "AMARILLA: orden congelado, nadie adelanta", "flag")
            eta = rc.caution_eta() or {}
            closed = "BOXES CERRADOS: solo con permiso del director" + (f" · abren en la vuelta {eta['pits']}" if eta.get("pits") else "")   # (the lap goes last: the hub writes it over the banner)
            _say(rc, PITS_OPEN if rc.caution and rc.caution.pits_open else closed, "flag", hold="pits")   # (a stage break's "break" action may have come first)
            if action[1] is not None:
                _say(rc, f"FREE PASS: {_name(inst, action[1])}", "race", hold="freepass")
                _say(rc, "FREE PASS: pasa al pace car por fuera y ponte al final de la fila", "race", action[1], hold="freepass-car")
            else:
                _say(rc, "SIN FREE PASS", "race", hold="freepass")
        elif action[0] == "break":
            lap = rc.stage_break(crossing is not None)
            _record_stage(server_id, inst, rc)
            if lap is not None:
                _say(rc, PITS_OPEN, "flag", hold="pits")
                if lap:
                    _say(rc, f"REINICIO: bandera verde en la vuelta {lap}", "race", hold="restart-lap")
        elif action[0] == "no_free_pass":
            rc.drop_hold("freepass-car")
            _say(rc, f"FREE PASS anulado: {_name(inst, action[1])} entró a boxes", "race", hold="freepass")
        elif action[0] == "pit_violation":
            _say(rc, f"{_name(inst, action[1])}: entró a boxes cerrados · DRIVE-THROUGH", "penalty")
            _say(rc, "DRIVE-THROUGH: boxes cerrados bajo amarilla", "penalty", action[1])
        elif action[0] == "violation":
            _say(rc, f"{_name(inst, action[1])}: adelantó bajo amarilla · DRIVE-THROUGH", "penalty")
            _say(rc, "DRIVE-THROUGH: adelantaste bajo amarilla", "penalty", action[1])


_stage_files = threading.Lock()   # the journal of the stages is written by the race thread and filed by the ACSP loop: one at a time


def _results_dir(server_id: int) -> Path:
    return Path(settings.data_dir) / "instances" / str(server_id) / "results"


def _record_stage(server_id: int, inst, rc) -> None:
    """A stage of a NASCAR race ended: the order of the field at that moment goes to disk at once (results/stages.pending), because the points of the stage are read from it when the race is counted.
    Cars that do not race (director, caster, pace car) and cars that are gone take no place."""
    with _lock:
        stage = rc.nascar.stage + 1 if rc.nascar else 0
        if not (rc.caution and rc.nascar) or stage in rc.stage_log:   # (a repeated break does not rewrite a stage that is over)
            return
        out, order = _non_racing_guids(time.time()), []
        for car in rc.caution.frozen:
            g = "" if car in rc.caution.gone else (inst.acsp.cars.get(car) or {}).get("driver_guid") or ""   # (a car that left: its slot may be somebody else's now)
            if g and g not in out and g not in order:
                order.append(g)
        first = not rc.stage_log
        rc.stage_log[stage] = order[:40]
        data = {"at": time.time(), "stages": [{"stage": n, "order": o} for n, o in sorted(rc.stage_log.items())]}
    d = _results_dir(server_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        with _stage_files:
            if first:
                claim_orphan(d)   # (the journal of the race before, if its session end was never heard of, is not lost)
            tmp = d / (STAGES_PENDING + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, d / STAGES_PENDING)
    except OSError:
        log.exception("could not write the stage order of server %s", server_id)
        with _lock:
            rc.stage_log.pop(stage, None)   # (not saved: the next look at this stage tries again)


def claim_stages(server_id: int, filename: str) -> None:
    """acServer said the session ended and wrote `filename`: if it is a race and a NASCAR journal is waiting, it becomes that result's own (<result>.stages)."""
    name = re.split(r"[\\/]", str(filename))[-1]
    d = _results_dir(server_id)
    if not name.endswith("_RACE.json"):
        return
    with _stage_files:
        if pending_is_unclaimed(d, name):   # (a journal older than another race result is a lost one: not this race's)
            try:
                os.replace(d / STAGES_PENDING, d / (name[: -len(".json")] + STAGES_SUFFIX))
            except OSError:
                log.exception("could not file the stage order of %s", name)


def _mark_finish(server_id: int, laps: int) -> None:
    """The chequered flag fell: acServer goes on counting the laps of a race it does not know is over, so the result file is cut at this moment (app/results.py `_finish_cut`)."""
    d = _results_dir(server_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        (d / f"{int(now)}{FINISH_SUFFIX}").write_text(json.dumps({"at": now, "laps": laps}))
    except OSError:
        log.exception("could not write the finish marker of server %s", server_id)


def plain_winner(client, event: dict) -> None:
    """A race without NASCAR: the winner is the first car to cross the line after the end (the distance in laps, or the time of a timed race), as AC itself ranks it (laps, then time)."""
    s = client.session
    if s.get("session_type") != 3 or client.winner_done:
        return
    row = next((r for r in event["leaderboard"] if r["car_id"] == event["car_id"]), None)
    goal = s.get("laps") or 0
    over = (row is not None and row["laps"] >= goal) if goal else (bool(s.get("time_min")) and time.time() - client.session_t0 >= s["time_min"] * 60)
    if not over:
        return
    client.winner_done = True
    rc = state_of(client.server_id, True, s, create=True)
    name = client.cars.get(event["car_id"], {}).get("driver_name") or f"#{event['car_id']}"
    log.info("winner: car %s (%s)", event["car_id"], name)
    rc.notify(f"GANADOR: {name}", "winner", hold="winner")


def auto_green(server_id: int, rc, car: int) -> None:
    """The restart leader crossed the line after "to go": the green flag, as the director would send it."""
    inst = _live(server_id)
    if inst and rc.formation:   # a rolling start: the green when the pace car is in the pits and a competitor crosses the line (alone: when it arrives)
        if rc.formation_green(car, set(inst.acsp.cars) <= {rc.pace_car}):
            _say(rc, SAY["green"], "flag")
        elif rc.take_blocked_warning():
            _say(rc, "El pace car no ha entrado a boxes: la bandera verde es manual", "flag")
        return
    with _lock:
        if inst and rc.flag in ("vsc", "fcy"):
            speeds = {c: v for c, info in inst.acsp.cars.items() if (v := speed_kmh(info)) is not None}
            rc.neutralization_clear_tick(inst.acsp.field.samples(), speeds, time.monotonic(), time.time(), connected=set(inst.acsp.cars))
        actions = rc.green_if_due(car, field=inst.acsp.field) if inst else None
    if actions is None:
        if rc.take_blocked_warning():
            _say(rc, "El pace car no ha entrado a boxes: la bandera verde es manual", "flag")
        return
    _say(rc, SAY["green"], "flag")
    nascar_actions(server_id, rc, actions)


def finish_nascar(now: float) -> None:
    """Capture the configured delay at done; later delay edits never move an existing `next_at`."""
    for inst in supervisor.live():
        rc = state_of(inst.server_id, True, inst.acsp.session)
        with _lock:
            if not rc or not rc.nascar:
                continue
            if rc.finish_wait is not None:
                if rc.finish_wait & inst.acsp.cars.keys() and now < rc.finish_deadline:
                    continue
                rc.finish_wait = None
                rc.nascar.done()
                rc.next_at = now + rc.next_delay_s
                rc._bump()
            if rc.next_at is None or now < rc.next_at:
                continue
            rc.next_at = None
            rc._bump()
        inst.acsp.send(acsp.encode_next_session())


class AutoYellowIn(BaseModel):
    enabled: bool


@control_router.post("/auto-yellow")
def auto_yellow(server_id: int, body: AutoYellowIn, sess: SessionDep) -> dict:
    """The yellow comes out by itself on a pile-up or a stopped car (off: the director only gets the proposal). Memory only, like the rest of the state."""
    _, rc = _control(server_id, sess)
    with _lock:
        rc.auto_yellow = body.enabled
        rc._bump()
    return rc.snapshot()


class PaceCarIn(BaseModel):
    steam_id: str | None = Field(default=None, pattern=r"^[0-9]{17}$")   # the pace car's driver; null: no pace car
    car_id: int | None = Field(default=None, ge=0, le=255)               # older panels: the car now in that slot, turned into its driver's SteamID; null: no pace car
    kmh: int = Field(default=120, ge=PACE_MIN, le=PACE_MAX)


@control_router.post("/give-back/{ob_id}/forgive", status_code=204)
def give_back_forgive(server_id: int, ob_id: int, sess: SessionDep) -> None:
    """The director forgives a place that was owed (the obligation closes, nothing is written)."""
    _, rc = _control(server_id, sess)
    if not rc.give_back_forgive(ob_id):
        raise HTTPException(404, "that obligation is not open")


@control_router.post("/give-back/{ob_id}/sanction", status_code=204)
def give_back_sanction(server_id: int, ob_id: int, sess: SessionDep, user: CurrentUser) -> None:
    """The director sanctions a place that was owed right now, without waiting for the time: the same penalty, written at once. The obligation is only closed once the penalty can be written: with no
    Steam ID (409) or a database that fails (503) it stays open."""
    inst, rc = _control(server_id, sess)
    ob = rc.give_back_peek(ob_id)
    if not ob or not rc.gb_rule:
        raise HTTPException(404, "that obligation is not open")
    seconds = rc.gb_rule["seconds"]
    row = _due_row(inst, ob["car"], ob["creditors"], seconds, time.time(), by=user.username, owner=rc.gb_owner.get(ob_id))
    if not row:
        raise HTTPException(409, "that car has no Steam ID, or its driver changed: no penalty can be written")
    taken = rc.give_back_due_now(ob_id)
    if not taken:
        raise HTTPException(404, "that obligation was closed meanwhile")
    try:
        save_dues([row])
    except Exception as e:   # noqa: BLE001
        rc.give_back_restore(taken)
        log.exception("give-back: sanction of obligation %s could not be written", ob_id)
        raise HTTPException(503, "the penalty could not be written: the obligation is back open") from e
    rc.gb_owner.pop(ob_id, None)
    _due_notices(inst, rc, ob["car"], seconds)


@control_router.post("/pace-car")
def set_pace_car(server_id: int, body: PaceCarIn, sess: SessionDep) -> dict:
    """Which DRIVER is the pace car (by SteamID; his client runs the hub script that drives the car on autopilot) and its pace. It leads under every NASCAR caution and goes in on 'to go'.
    `steam_id` or `car_id` (one of them, `null` removes the pace car); he does not have to be connected yet: his slot is found whenever he joins."""
    inst, rc = _control(server_id, sess)
    given = body.model_fields_set & {"steam_id", "car_id"}
    if len(given) != 1:
        raise HTTPException(422, "send steam_id or car_id (null removes the pace car)")
    steam_id = body.steam_id
    if "car_id" in given and body.car_id is not None:
        car = inst.acsp.cars.get(body.car_id)
        if not car:
            raise HTTPException(409, "that car is not on the server")
        steam_id = car.get("driver_guid") or ""
        if not re.fullmatch(r"[0-9]{17}", steam_id):
            raise HTTPException(409, "that car has no SteamID")
    inst.pace_steam_id = None   # the director chose: what the event named no longer applies
    try:
        if steam_id is None:
            rc.set_pace_car(None)
        else:
            rc.set_pace_car(slot_of(inst.acsp.cars, steam_id), body.kmh, steam_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return rc.snapshot()


class PitOkIn(BaseModel):
    car_id: int = Field(ge=0, le=255)
    allowed: bool = True


@control_router.post("/pit-ok")
def pit_ok(server_id: int, body: PitOkIn, sess: SessionDep) -> dict:
    """The pits are closed under a NASCAR caution: the director lets one car in (or takes the permission back). A car already judged for entering stays judged: clear its penalty by hand."""
    _, rc = _control(server_id, sess)
    if not rc.pit_exception(body.car_id, body.allowed):
        raise HTTPException(409, "no hay una amarilla de NASCAR en curso")
    return rc.snapshot()


class WinnerTestIn(BaseModel):
    name: str = Field(default="Piloto de prueba", min_length=1, max_length=40, pattern=r"^[^\"\\\x00-\x1f]+$")   # (the hub reads notices with a regex: no quotes or backslashes)


@control_router.post("/winner-test")
def winner_test(server_id: int, body: WinnerTestIn, sess: SessionDep) -> dict:
    """A FAKE winner notice (15 s) to see the hub's winner banner without finishing a race. Changes nothing else."""
    _, rc = _control(server_id, sess)
    rc.notify(f"GANADOR: {body.name}", "winner", ttl=15)
    log.info("winner-test: %s", body.name)
    return rc.snapshot()


class RestartIn(BaseModel):
    to_go: Literal[1, 2] = 1   # "one to go" / "two to go" (the rulebook's choose can come with either)


def _restart(inst, rc, to_go: int) -> dict:
    plan = rc.restart_plan(inst.acsp.field.snapshot(), to_go)
    if plan is None:
        if rc.flag not in CAUTION:
            raise HTTPException(409, "no hay una amarilla en curso")
        # a yellow / VSC / FCY with no NASCAR caution (no NASCAR armed, no pace car): nobody to place or lead, only the notice; the green stays the director's
        log.info("restart called: %s to go (plain %s, no NASCAR caution)", to_go, rc.flag)
        rc.audit("manual_to_go", to_go=to_go, flag=rc.flag, nascar_caution=False, executed=True)
        _say(rc, ("UNA" if to_go == 1 else "DOS") + " PARA VERDE", "flag", hold="togo")
        return rc.snapshot()
    order, waved, choosers = plan
    log.info("restart called: %s to go", to_go)
    _say(rc, ("UNA" if to_go == 1 else "DOS") + " PARA VERDE", "flag", hold="togo")
    if rc.caution and rc.caution.pace_only:
        return rc.snapshot()   # (no places or lanes to give outside NASCAR)
    for car in waved:
        _say(rc, "WAVE-AROUND: pasa al pace car y al líder, y ponte al final de la fila", "race", car, hold=f"wave-{car}")
    for i, car in enumerate(order, 1):
        _say(rc, f"REINICIO: P{i}" + (" · ELIGE CARRIL (interior o exterior)" if car in choosers else " · sin elección: al final de tu vuelta"), "race", car, hold=f"restart-{car}")
    return rc.snapshot()


@control_router.post("/restart")
def call_restart(server_id: int, body: RestartIn, sess: SessionDep) -> dict:
    """'To go' under a NASCAR caution: the wave-around cars are told to pass, every car gets its place in the restart and who may choose a lane. The green flag is still the director's."""
    inst, rc = _control(server_id, sess)
    return _restart(inst, rc, body.to_go)


def rolling_start(rc, cars: dict) -> None:
    """A NASCAR race begins with its pace car connected: the rolling start (one formation lap behind it, then the 'one to go'). Without it the standing start goes on."""
    rc.follow_pace_driver(slot_of(cars, rc.pace_guid))   # (he may have connected since the last check)
    if rc.pace_car is not None and rc.start_formation():
        _say(rc, "ROLLING START: una vuelta de formación detrás del pace car", "flag")


def auto_caution(server_id: int, rc, car: int) -> None:
    """A car crossed the line under a caution: after the pace car's third lap with the track clear the 'to go' comes out by itself; at the sixth, if it is not clear, the director is told (the 'to go' is theirs)."""
    inst = _live(server_id)
    if not inst:
        return
    if rc.formation:
        if rc.formation_lap(car):
            _say(rc, "VUELTA DE FORMACIÓN COMPLETA: UNA PARA VERDE", "flag")
        return
    speeds = {i: v for i, c in inst.acsp.cars.items() if (v := speed_kmh(c)) is not None}
    with _lock:   # (the decision and the restart as one step: a director's 'to go' cannot slip in between and be overwritten)
        verdict = rc.caution_lap(car, speeds, time.time(), set(inst.acsp.cars))
        if verdict == "go":
            try:
                _restart(inst, rc, 1)
            except HTTPException:
                pass   # (the caution ended in between: nothing to restart)
        elif verdict == "two":
            try:
                _restart(inst, rc, 2)
            except HTTPException:
                pass   # (the caution ended in between: nothing to announce)
        elif verdict == "pits":
            _say(rc, PITS_OPEN, "flag", hold="pits")
        elif verdict == "one":
            _say(rc, "UNA PARA VERDE", "flag", hold="togo")
        elif verdict == "warn":
            _say(rc, "La pista no se despeja tras 6 vueltas: el «to go» es manual", "flag")


@control_router.post("/penalties", status_code=201)
def add_penalty(server_id: int, body: PenaltyIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    car = inst.acsp.cars.get(body.car_id)
    if not car:
        raise HTTPException(409, "that car is not on the server")
    p = rc.add_penalty(body.car_id, body.laps)
    _say(rc, f"DRIVE-THROUGH para {car.get('driver_name') or '#' + str(body.car_id)}: pasa por boxes en {body.laps} vueltas", "penalty")
    return p


@control_router.delete("/penalties/{penalty_id}", status_code=204)
def clear_penalty(server_id: int, penalty_id: int, sess: SessionDep) -> None:
    _, rc = _control(server_id, sess)
    if not rc.clear_penalty(penalty_id):
        raise HTTPException(404, "penalty not found")


def announce(server_id: int, violations: list[dict]) -> None:
    """The chat line of a judged VSC (names, not car numbers)."""
    inst = _live(server_id)
    if not inst:
        return
    rc = state_of(server_id, True, inst.acsp.session)
    if not rc:
        return
    if not violations:
        _say(rc, "VSC: todos al ritmo", "flag")
        return
    names = ", ".join((inst.acsp.cars.get(v["car_id"], {}).get("driver_name") or "#" + str(v["car_id"])) + f" ({v['speed_kmh']} km/h)" for v in violations)
    _say(rc, "VSC: no bajaron a tiempo: " + names, "penalty")


async def monitor_forever() -> None:
    """Judges every VSC when its grace ends (one loop for all servers, started by the app's lifespan)."""
    while True:
        try:
            now = time.time()
            finish_nascar(now)
            caution_tick(now)
            from app.league import neutralization_rule, neutralization_rule_stale
            for inst in supervisor.live():
                if neutralization_rule_stale(inst.server_id):
                    await asyncio.to_thread(neutralization_rule, inst.server_id)
            for sid in flag_modes_stale_servers():
                await asyncio.to_thread(flag_modes_of, sid)
            for sid in blue_stale_servers():
                await asyncio.to_thread(flag_rules_of, sid)
            for sid in drs_stale_servers():
                await asyncio.to_thread(drs_rule_of, sid)
            for sid in give_back_stale_servers():
                await asyncio.to_thread(give_back_rule_of, sid)   # (the league's rule, from the database, off the event loop)
            from app.league import cut_rule_stale
            for inst in supervisor.live():
                if cut_rule_stale(inst.server_id):
                    await asyncio.to_thread(cut_rule_of, inst.server_id)
                rc = state_of(inst.server_id, True, inst.acsp.session)
                if rc and _sanctioned(inst):
                    from app.live.racecontrol import cut_rule_peek
                    rule = cut_rule_peek(inst.server_id)
                    with _lock:
                        cars = set(rc.cuts_client) | set(rc.cuts_server)
                    for car in cars:
                        guid = (inst.acsp.cars.get(car) or {}).get("driver_guid")
                        if guid:
                            rc.add_cuts(car, 0, False, rule, str(guid))
            try:
                await asyncio.to_thread(save_cut_dues)
            except Exception:
                log.exception("cuts: pending rounds could not be saved; retained for retry")
            try:
                await asyncio.to_thread(save_flag_dues)
            except Exception:
                log.exception("flags: pending sanctions could not be saved; retained for retry")
            try:
                await asyncio.to_thread(save_drs_dues)
            except Exception:
                log.exception("drs: pending offences could not be saved; retained for retry")
            try:
                from app.live.racecontrol import acknowledge_neutral_dues
                n_save, n_drop = neutral_rows_to_save()
                if n_drop:
                    log.warning("neutral: %s time penalties dropped (the slot changed hands or the server stopped): %s", len(n_drop), n_drop)
                    acknowledge_neutral_dues(n_drop)
                if n_save:
                    await asyncio.to_thread(save_neutral_dues, n_save)
                    acknowledge_neutral_dues(n_save)   # (only after the commit: a failure keeps them for the next look)
                    announce_neutral(n_save)
            except Exception:
                log.exception("neutral: pending time penalties could not be saved; retained for retry")
            drs_tick(now)
            _unsaved_dues.extend(give_back_tick(now))
            if len(_unsaved_dues) > MAX_UNSAVED_DUES:   # (a database that has been down for minutes: what does not fit is logged whole, never dropped silently)
                log.error("give-back: too many penalties waiting to be saved, dropping the oldest: %s", _unsaved_dues[:-MAX_UNSAVED_DUES])
                del _unsaved_dues[:-MAX_UNSAVED_DUES]
            if _unsaved_dues:
                batch = list(_unsaved_dues)
                try:
                    await asyncio.to_thread(save_dues, batch)
                    del _unsaved_dues[:len(batch)]   # (only after it is written: a failure keeps them for the next tick)
                except Exception:   # noqa: BLE001 - kept and tried again; the loop must not stop
                    log.exception("give-back: %s penalties could not be saved, they will be tried again", len(batch))
            incident_tick(now)
            autoflags_tick(now)
            pace_car_check()
            live = [(inst.server_id, inst.acsp.cars) for inst in supervisor.live()]
            for server_id, violations in tick(now, live):
                announce(server_id, violations)
        except Exception:   # a bug here must not stop the loop: the next tick tries again
            log.exception("race control monitor")
        await asyncio.sleep(MONITOR_EVERY)


@control_router.get("/audit")
def audit_view(server_id: int, sess: SessionDep, since: datetime | None = None, limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    _get(sess, server_id)
    stamp = (since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)).isoformat() if since else None
    return raceaudit.read(server_id, stamp, limit)
