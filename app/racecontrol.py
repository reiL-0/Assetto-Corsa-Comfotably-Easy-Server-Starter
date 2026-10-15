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
Each change is also said in the chat, so drivers without the hub see it. The state is in memory and goes with the server or the session. `monitor_forever` (started by the app's
lifespan) watches a VSC: it samples the speeds and judges the drivers when the grace ends.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import supervisor
from app.auth import require, require_any
from app.db import SessionDep
from app.live import acsp
from app.live.nascar import DRAINING, MAX_SHARE, MAX_SHARES, MAX_TOTAL
from app.live.racecontrol import GRACE_DEFAULT, GRACE_MAX, GRACE_MIN, LAPS_MAX, NEXT_SESSION_DELAY, PACE_MAX, PACE_MIN, TOL_DEFAULT, TOL_MAX, _lock, state_of, tick
from app.servers import _get

log = logging.getLogger("acmanager.racecontrol")
# the hub's token is the lowest role: it reads the state and reports its pit-lane status, nothing else of the manager
state_router = APIRouter(prefix="/servers/{server_id}", tags=["race-control"], dependencies=[Depends(require_any("reporter", "driver", "steward", "admin"))])
control_router = APIRouter(prefix="/servers/{server_id}/race-control", tags=["race-control"], dependencies=[Depends(require("steward"))])

SAY = {"green": "BANDERA VERDE: se reanuda la carrera", "yellow": "BANDERA AMARILLA: sin adelantar", "white": "BANDERA BLANCA: última vuelta", "checkered": "BANDERA DE CUADROS"}
CHAT_DELAY = 2.0       # s; the hub asks every 1 s (+ the website's 1 s cache)
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


def _chat(inst, text: str) -> None:
    inst.acsp.send(acsp.encode_broadcast_chat(text))


def _say_later(inst, text: str) -> None:
    """The chat line of a flag change goes out CHAT_DELAY s late: the hub of every client learns the flag by asking (at most POLL + the website's 1 s cache later), and the yellow on
    screen must not come after the announcement. The state itself changes at once."""
    def go() -> None:
        try:
            _chat(inst, text)
        except Exception:   # the server may have stopped meanwhile: a lost announcement is not worth a traceback in a timer thread
            log.debug("flag announcement not sent", exc_info=True)

    t = threading.Timer(CHAT_DELAY, go)   # the API runs in worker threads: no event loop here
    t.daemon = True
    t.start()


@state_router.get("/race-state")
def race_state(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    inst = _live(server_id)
    rc = state_of(server_id, bool(inst), inst.acsp.session if inst else None)
    return rc.snapshot() if rc else {"rev": 0, "ts": 0, "running": bool(inst), "flag": "green", "pace_kmh": None, "penalties": [], "vsc_id": 0, "grace_left": 0,
                                     "tolerance_kmh": None, "vsc_penalty": None, "violations": [], "exempt": [], "nascar": None, "nascar_next_delay_min": NEXT_SESSION_DELAY // 60}


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
        actions = rc.set_flag(body.flag, body.pace_kmh, body.grace_s, body.tolerance_kmh, body.penalty.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    nascar_actions(server_id, rc, actions)
    if body.flag in ("vsc", "fcy"):
        pen = f" · si sigues por encima: drive-through de {body.penalty.laps} vueltas" if body.penalty.kind == "pits" else ""
        name = "VIRTUAL SAFETY CAR" if body.flag == "vsc" else "FULL COURSE YELLOW"
        _say_later(inst, f"{name}: baja a {body.pace_kmh} km/h en {body.grace_s} s{pen}")
    else:
        _say_later(inst, SAY[body.flag])
    return rc.snapshot()


class NascarIn(BaseModel):
    total: int = Field(ge=0, le=MAX_TOTAL)
    stages: list[int] | None = Field(default=None, min_length=1)
    shares: list[Annotated[float, Field(gt=0, le=MAX_SHARE, allow_inf_nan=False)]] | None = Field(default=None, min_length=1, max_length=MAX_SHARES)
    next_delay_min: int | None = Field(default=None, ge=0, le=60)


@control_router.post("/nascar")
@control_router.patch("/nascar")
def configure_nascar(server_id: int, body: NascarIn, sess: SessionDep) -> dict:
    _, rc = _control(server_id, sess)
    try:
        rc.configure_nascar(body.total, body.stages, tuple(body.shares) if body.shares is not None else None, body.next_delay_min)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return rc.snapshot()


def nascar_actions(server_id: int, rc, actions: list[tuple], crossing: int | None = None) -> None:
    inst = _live(server_id)
    if not inst:
        return
    for action in actions:
        with _lock:
            if state_of(server_id, True, inst.acsp.session) is not rc or not rc.nascar:
                return
            if action[0] == "flag" and rc.nascar.flag == action[1]:
                rc.set_flag(action[1], source="nascar")
            elif action[0] == "finish" and rc.nascar.phase == DRAINING and rc.finish_wait is None:
                rc.finish_wait = {i for i in inst.acsp.cars if i != crossing}
                rc.finish_deadline = time.time() + 90
                rc._bump()
        if action[0] == "say":
            _say_later(inst, action[1])


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


@control_router.post("/penalties", status_code=201)
def add_penalty(server_id: int, body: PenaltyIn, sess: SessionDep) -> dict:
    inst, rc = _control(server_id, sess)
    car = inst.acsp.cars.get(body.car_id)
    if not car:
        raise HTTPException(409, "that car is not on the server")
    p = rc.add_penalty(body.car_id, body.laps)
    _chat(inst, f"DRIVE-THROUGH para {car.get('driver_name') or '#' + str(body.car_id)}: pasa por boxes en {body.laps} vueltas")
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
    if not violations:
        _chat(inst, "VSC: todos al ritmo")
        return
    names = ", ".join((inst.acsp.cars.get(v["car_id"], {}).get("driver_name") or "#" + str(v["car_id"])) + f" ({v['speed_kmh']} km/h)" for v in violations)
    _chat(inst, "VSC: no bajaron a tiempo: " + names)


async def monitor_forever() -> None:
    """Judges every VSC when its grace ends (one loop for all servers, started by the app's lifespan)."""
    while True:
        try:
            now = time.time()
            live = [(i.server_id, i.acsp.cars) for i in supervisor.live()]
            finish_nascar(now)
            for server_id, violations in tick(now, live):
                announce(server_id, violations)
        except Exception:   # a bug here must not stop the loop: the next tick tries again
            log.exception("race control monitor")
        await asyncio.sleep(MONITOR_EVERY)
