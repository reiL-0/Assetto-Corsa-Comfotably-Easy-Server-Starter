"""Race control: the director's flag, the virtual safety car (VSC) and drive-throughs to serve, for the CSP hub script on every client (app/live/racecontrol.py).

  GET    /servers/{id}/race-state               what the hub reads (the website serves it as /api/csp/state): the website's token (role reporter) and everyone above
  POST   /servers/{id}/race-control/pit         {car_id, name, pit}: the hub of a car says whether it is in the pit lane (a VSC exemption; reporter and above)
  POST   /servers/{id}/race-control             {flag: green|yellow|vsc|fcy|white|checkered, ...}: the director (steward). A yellow is only a flag; a `vsc` or `fcy` needs pace_kmh and may carry
                                                grace_s (10), tolerance_kmh (5) and penalty {kind: none|pits, laps}
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
import logging
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app import supervisor
from app.auth import require, require_any
from app.db import SessionDep
from app.live import acsp
from app.live.nascar import DRAINING, MAX_SHARE, MAX_SHARES, MAX_TOTAL
from app.live.racecontrol import GRACE_DEFAULT, GRACE_MAX, GRACE_MIN, LAPS_MAX, NEXT_SESSION_DELAY, PACE_MAX, PACE_MIN, TOL_DEFAULT, TOL_MAX, _lock, speed_kmh, state_of, tick
from app.servers import _get

log = logging.getLogger("acmanager.racecontrol")
# the hub's token is the lowest role: it reads the state and reports its pit-lane status, nothing else of the manager
state_router = APIRouter(prefix="/servers/{server_id}", tags=["race-control"], dependencies=[Depends(require_any("reporter", "driver", "steward", "admin"))])
control_router = APIRouter(prefix="/servers/{server_id}/race-control", tags=["race-control"], dependencies=[Depends(require("steward"))])

SAY = {"green": "BANDERA VERDE: se reanuda la carrera", "yellow": "BANDERA AMARILLA: sin adelantar", "white": "BANDERA BLANCA: última vuelta", "checkered": "BANDERA DE CUADROS"}
MONITOR_EVERY = 0.25   # s; ACSP positions arrive about every 0.2 s


class PenaltyCfg(BaseModel):
    kind: Literal["none", "pits"] = "none"
    laps: int = Field(default=3, ge=1, le=LAPS_MAX)


class FlagIn(BaseModel):
    flag: Literal["green", "yellow", "vsc", "fcy", "white", "checkered"]
    pace_kmh: int | None = Field(default=None, ge=PACE_MIN, le=PACE_MAX)   # a VSC only (a yellow is only a flag)
    grace_s: int = Field(default=GRACE_DEFAULT, ge=GRACE_MIN, le=GRACE_MAX)   # a VSC: seconds every driver gets to slow down by themselves
    tolerance_kmh: int = Field(default=TOL_DEFAULT, ge=0, le=TOL_MAX)         # a VSC: km/h over the pace that are forgiven when the grace ends
    penalty: PenaltyCfg = PenaltyCfg()                                        # a VSC: what whoever is still over gets


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


def pace_car_check() -> None:
    """The pace car stops being one when its driver leaves or somebody else takes the slot (the guid it was named with is not in that slot any more)."""
    for inst in supervisor.live():
        rc = state_of(inst.server_id, True, inst.acsp.session)
        if rc and rc.pace_car is not None:
            car = inst.acsp.cars.get(rc.pace_car)
            if not car or (car.get("driver_guid") or "") != rc.pace_guid:
                rc.set_pace_car(None)


def incident_tick(now: float) -> None:
    """Contacts and stopped cars of every server racing under NASCAR: a proposal for the director, or the yellow itself when the director asked for that."""
    for inst in supervisor.live():
        rc = state_of(inst.server_id, True, inst.acsp.session)
        if not rc or not rc.nascar:
            continue
        speeds = {i: s for i, c in inst.acsp.cars.items() if (s := speed_kmh(c)) is not None}
        prop, actions = rc.incident_step(speeds, inst.acsp.field, now)
        if prop:
            who = ", ".join(_name(inst, c) for c in prop["cars"])
            _say(rc, f"{prop['text']}: {who}" + (" · AMARILLA AUTOMÁTICA" if rc.auto_yellow else ""), "flag")
            nascar_actions(inst.server_id, rc, actions)


def _say(rc, text: str, kind: str = "info", to: int = -1, hold: str | None = None) -> None:
    """What used to go to the chat now goes on the screen of every hub (race-state `notices`): it arrives with the flag itself, so it can never come before it."""
    rc.notify(text, kind, to, hold=hold)


@state_router.get("/race-state")
def race_state(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    inst = _live(server_id)
    rc = state_of(server_id, bool(inst), inst.acsp.session if inst else None)
    return rc.snapshot() if rc else {"rev": 0, "ts": 0, "running": bool(inst), "flag": "green", "pace_kmh": None, "penalties": [], "notices": [], "vsc_id": 0, "grace_left": 0,
                                     "tolerance_kmh": None, "vsc_penalty": None, "violations": [], "exempt": [], "nascar": None, "caution": None, "proposal": None, "auto_yellow": True, "pacecar": None, "nascar_next_delay_min": NEXT_SESSION_DELAY // 60}


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


def _control(server_id: int, sess: SessionDep):
    _get(sess, server_id)
    inst = _live(server_id)
    if not inst:
        raise HTTPException(409, "server not running or ACSP plugin not connected")
    return inst, state_of(server_id, True, inst.acsp.session, create=True)


@control_router.post("")
def set_flag(server_id: int, body: FlagIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    try:
        actions = rc.set_flag(body.flag, body.pace_kmh, body.grace_s, body.tolerance_kmh, body.penalty.model_dump(), field=inst.acsp.field)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.flag in ("vsc", "fcy"):
        pen = f" · si sigues por encima: drive-through de {body.penalty.laps} vueltas" if body.penalty.kind == "pits" else ""
        name = "VIRTUAL SAFETY CAR" if body.flag == "vsc" else "FULL COURSE YELLOW"
        _say(rc, f"{name}: baja a {body.pace_kmh} km/h en {body.grace_s} s{pen}", "flag")
    else:
        _say(rc, SAY[body.flag], "flag")
    nascar_actions(server_id, rc, actions)   # (after the director's own notice: a white flag the engine adds comes last)
    return rc.snapshot()


class NascarIn(BaseModel):
    total: int = Field(ge=0, le=MAX_TOTAL)
    stages: list[int] | None = Field(default=None, min_length=1)
    shares: list[Annotated[float, Field(gt=0, le=MAX_SHARE, allow_inf_nan=False)]] | None = Field(default=None, min_length=1, max_length=MAX_SHARES)
    next_delay_min: int | None = Field(default=None, ge=0, le=60)


@control_router.post("/nascar")
@control_router.patch("/nascar")
def configure_nascar(server_id: int, body: NascarIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    try:
        actions = rc.configure_nascar(body.total, body.stages, tuple(body.shares) if body.shares is not None else None, body.next_delay_min)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    nascar_actions(server_id, rc, actions)
    # Remember it for the next race (the in-memory engine is reset by every new_session). Explicit `stages` are API-only and not part of a preset.
    if not rc.nascar:
        remembered = None
    elif not rc.nascar.fixed_ends:
        remembered = {"total": rc.nascar.total, "shares": list(rc.nascar.shares)}
    else:
        return rc.snapshot()
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
    return rc.snapshot()


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
        elif action[0] == "caution":
            _say(rc, "AMARILLA: orden congelado, nadie adelanta", "flag")
            _say(rc, "BOXES CERRADOS: solo con permiso del director", "flag", hold="pits")
            if action[1] is not None:
                _say(rc, f"FREE PASS: {_name(inst, action[1])}", "race", hold="freepass")
                _say(rc, "FREE PASS: pasa al pace car por fuera y ponte al final de la fila", "race", action[1], hold="freepass-car")
            else:
                _say(rc, "SIN FREE PASS", "race", hold="freepass")
        elif action[0] == "no_free_pass":
            rc.drop_hold("freepass-car")
            _say(rc, f"FREE PASS anulado: {_name(inst, action[1])} entró a boxes", "race", hold="freepass")
        elif action[0] == "pit_violation":
            _say(rc, f"{_name(inst, action[1])}: entró a boxes cerrados · DRIVE-THROUGH", "penalty")
            _say(rc, "DRIVE-THROUGH: boxes cerrados bajo amarilla", "penalty", action[1])
        elif action[0] == "violation":
            _say(rc, f"{_name(inst, action[1])}: adelantó bajo amarilla · DRIVE-THROUGH", "penalty")
            _say(rc, "DRIVE-THROUGH: adelantaste bajo amarilla", "penalty", action[1])


def auto_green(server_id: int, rc, car: int) -> None:
    """The restart leader crossed the line after "to go": the green flag, as the director would send it."""
    inst = _live(server_id)
    if inst and rc.formation:   # a rolling start: the green when the pace car is in the pits and a competitor crosses the line (alone: when it arrives)
        if rc.formation_green(car, set(inst.acsp.cars) <= {rc.pace_car}):
            _say(rc, SAY["green"], "flag")
        elif rc.take_blocked_warning():
            _say(rc, "El pace car no ha entrado a boxes: la bandera verde es manual", "flag")
        return
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
    car_id: int | None = Field(default=None, ge=0, le=255)   # None: no pace car
    kmh: int = Field(default=120, ge=PACE_MIN, le=PACE_MAX)


@control_router.post("/pace-car")
def set_pace_car(server_id: int, body: PaceCarIn, sess: SessionDep) -> dict:
    """Which connected car is the pace car (a client running the hub script that drives it on autopilot) and its pace. It leads under every NASCAR caution and goes in on 'to go'."""
    inst, rc = _control(server_id, sess)
    if body.car_id is not None and body.car_id not in inst.acsp.cars:
        raise HTTPException(409, "that car is not on the server")
    try:
        rc.set_pace_car(body.car_id, body.kmh, (inst.acsp.cars.get(body.car_id) or {}).get("driver_guid") or "")
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


class RestartIn(BaseModel):
    to_go: Literal[1, 2] = 1   # "one to go" / "two to go" (the rulebook's choose can come with either)


def _restart(inst, rc, to_go: int) -> dict:
    plan = rc.restart_plan(inst.acsp.field.snapshot(), to_go)
    if plan is None:
        raise HTTPException(409, "no hay una amarilla de NASCAR en curso")
    order, waved, choosers = plan
    log.info("restart called: %s to go", to_go)
    _say(rc, ("UNA" if to_go == 1 else "DOS") + " PARA VERDE", "flag", hold="togo")
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
    car = cars.get(rc.pace_car) if rc.pace_car is not None else None
    if car and (car.get("driver_guid") or "") == rc.pace_guid and rc.start_formation():
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
            live = [(i.server_id, i.acsp.cars) for i in supervisor.live()]
            finish_nascar(now)
            caution_tick(now)
            incident_tick(now)
            pace_car_check()
            for server_id, violations in tick(now, live):
                announce(server_id, violations)
        except Exception:   # a bug here must not stop the loop: the next tick tries again
            log.exception("race control monitor")
        await asyncio.sleep(MONITOR_EVERY)
