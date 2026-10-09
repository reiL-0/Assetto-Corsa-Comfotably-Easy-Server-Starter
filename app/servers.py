"""Server CRUD, server_cfg.ini / entry_list.ini rendering, and start/stop."""

from __future__ import annotations

import asyncio
import configparser
import threading
from contextlib import contextmanager
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from sqlmodel import select

from app import supervisor
from app.live import cspweather
from app.auth import require
from app.config import settings
from app.db import SessionDep
from app.live import acsp
from app.live.acsp import ACSPClient
from app.models import Server
from app.services import ini_generator, server_service
from app.services.server_service import ServerError, adopt_running, server_lock, stop_instance, write_instance as _write_instance  # noqa: F401 - re-exported
from app.results import apply_penalties, non_racing_for, parse_result_file, penalties_for
from app.schemas.servers import (  # noqa: F401 - re-exported: other modules import these names from here
    Scalar,
    ServerIn,
    ServerOut,
    WeatherIn,
    DynamicTrackIn,
    OptionsIn,
    EntryIn,
    SessionIn,
    AppliedOut,
    WakeIn,
    WeatherEntryIn,
    LiveWeatherIn,
    WeatherPlanIn,
    CspWeatherIn,
    CspExtraIn,
    LimitsIn,
    ChatIn,
    SetSessionIn,
    AdminCommandIn,
)

router = APIRouter(prefix="/servers", tags=["servers"])
# Live moderation actions: stewards may use these; everything else on `router` is admin-write.
steward = APIRouter(prefix="/servers", tags=["servers"], dependencies=[Depends(require("steward"))])


_ports = server_service.ports


def _out(s: Server) -> ServerOut:
    return ServerOut(
        id=s.id,
        name=s.name,
        base_port=s.base_port,
        ports=_ports(s.base_port),
        config=s.config,
        entry_list=s.entry_list,
        wake=s.wake,
        stewards=s.stewards,
        limits={"cpu_percent": s.cpu_limit, "mem_mb": s.mem_limit_mb, "enforced": settings.limits_scope in ("user", "system")},
        welcome=s.welcome,
        csp_extra=s.csp_extra,
        weather_plan=s.weather_plan,
        session=s.session,
        integrity=s.integrity,
        integrity_extras=s.integrity_extras,
    )


# --- INI rendering -----------------------------------------------------------

_render_ini = ini_generator.render_ini


def render_server_cfg(s: Server) -> str:
    """server_cfg.ini with allocated ports merged into [SERVER] (user values win)."""
    return ini_generator.server_cfg(s.config, _ports(s.base_port), bool(s.welcome or s.csp_extra.strip()))


def render_entry_list(s: Server) -> str:
    return ini_generator.entry_list(s.entry_list)


# --- persistence helpers ---------------------------------------------------

def _get(sess: SessionDep, server_id: int) -> Server:
    s = sess.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    return s


_alloc_lock = threading.Lock()   # ponytail: one manager process (handlers run in threads); a UNIQUE(base_port) migration (plan T1.1) covers several


def _alloc_base_port(sess: SessionDep) -> int:
    taken = set(sess.exec(select(Server.base_port)).all())
    for base in range(settings.port_range_start, settings.port_range_end, 4):
        if base not in taken:
            return base
    raise HTTPException(507, "no free port block in configured range")


# --- routes --------------------------------------------------------------

@router.post("", response_model=ServerOut, status_code=201)
def create(body: ServerIn, sess: SessionDep) -> ServerOut:
    with _alloc_lock:   # choosing the block and committing it are one step: two simultaneous creates must not get the same ports
        s = Server(
            name=body.name,
            config=body.config,
            entry_list=body.entry_list,
            base_port=_alloc_base_port(sess),
        )
        sess.add(s)
        sess.commit()
    sess.refresh(s)
    return _out(s)


@router.get("", response_model=list[ServerOut])
def list_servers(sess: SessionDep) -> list[ServerOut]:
    return [_out(s) for s in sess.exec(select(Server)).all()]


@router.get("/{server_id}", response_model=ServerOut)
def get_server(server_id: int, sess: SessionDep) -> ServerOut:
    return _out(_get(sess, server_id))


@router.patch("/{server_id}", response_model=ServerOut)
def update(server_id: int, body: ServerIn, sess: SessionDep) -> ServerOut:
    s = _get(sess, server_id)
    s.name, s.config, s.entry_list = body.name, body.config, body.entry_list
    s.updated_at = datetime.now(UTC)
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return _out(s)


@router.delete("/{server_id}", status_code=204)
def delete(server_id: int, sess: SessionDep) -> None:
    s = _get(sess, server_id)
    inst = supervisor.get(server_id)
    if inst and inst.running:
        raise HTTPException(409, "stop the server before deleting it")
    sess.delete(s)
    sess.commit()


@router.get("/{server_id}/server_cfg.ini", response_class=PlainTextResponse)
def server_cfg_ini(server_id: int, sess: SessionDep) -> str:
    return render_server_cfg(_get(sess, server_id))


@router.get("/{server_id}/entry_list.ini", response_class=PlainTextResponse)
def entry_list_ini(server_id: int, sess: SessionDep) -> str:
    return render_entry_list(_get(sess, server_id))


@router.put("/{server_id}/server_cfg.ini", response_model=ServerOut)
async def upload_server_cfg_ini(server_id: int, request: Request, sess: SessionDep) -> ServerOut:
    """Raw INI in -> parsed into the structured config (headless file-transfer path)."""
    s = _get(sess, server_id)
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read_string((await request.body()).decode())
    s.config = {sec: dict(cp[sec]) for sec in cp.sections()}
    s.updated_at = datetime.now(UTC)
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return _out(s)


@router.put("/{server_id}/entry_list.ini", response_model=ServerOut)
async def upload_entry_list_ini(server_id: int, request: Request, sess: SessionDep) -> ServerOut:
    s = _get(sess, server_id)
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read_string((await request.body()).decode())
    car_sections = sorted(
        (sec for sec in cp.sections() if sec.startswith("CAR_")),
        key=lambda sec: int(sec.split("_", 1)[1]),
    )
    s.entry_list = [dict(cp[sec]) for sec in car_sections]
    s.updated_at = datetime.now(UTC)
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return _out(s)


def _restart_weather(s: Server) -> None:
    inst = supervisor.get(s.id)
    if inst:
        inst.set_weather_plan(s.weather_plan, s.config)


@router.put("/{server_id}/weather_plan", response_model=ServerOut)
async def set_weather_plan(server_id: int, body: WeatherPlanIn, sess: SessionDep) -> ServerOut:   # async: the director is a task of the event loop
    """The weather this server plays to CSP clients (hidden chat commands, see app/live/cspweather.py): a list of weathers per session (as in AC Server Manager)
    or the live weather of a place. Starts at once on a running server, otherwise at its next start."""
    s = _get(sess, server_id)
    s.weather_plan = body.model_dump()
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    _restart_weather(s)
    return _out(s)


@router.delete("/{server_id}/weather_plan", status_code=204)
async def clear_weather_plan(server_id: int, sess: SessionDep) -> None:
    s = _get(sess, server_id)
    s.weather_plan = None
    sess.add(s)
    sess.commit()
    _restart_weather(s)


@steward.post("/{server_id}/csp_weather")
def send_csp_weather(server_id: int, body: CspWeatherIn, sess: SessionDep) -> dict:
    """Send these conditions to every CSP client once, right now (a test, or a manual weather change). A running plan overwrites them at its next step."""
    _get(sess, server_id)
    text = cspweather.command({**body.model_dump(), "upcoming": body.current if body.upcoming is None else body.upcoming}, int(time.time()))
    _acsp(server_id).send(acsp.encode_broadcast_chat(text))
    return {"sent": True, "chars": len(text)}


@router.put("/{server_id}/csp_extra", response_model=ServerOut)
def set_csp_extra(server_id: int, body: CspExtraIn, sess: SessionDep) -> ServerOut:
    """Custom Shaders Patch extra options of this server (`[SCRIPT_n]`, `[EXTRA_RULES]`...), sent to CSP clients hidden in the welcome message
    (app/csp.py). Written to `cfg/welcome.txt` the next time the server starts or a session is applied; kept across sessions."""
    s = _get(sess, server_id)
    s.csp_extra = body.text.strip()
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return _out(s)


@router.put("/{server_id}/limits", response_model=ServerOut)
def set_limits(server_id: int, body: LimitsIn, sess: SessionDep) -> ServerOut:
    """CPU and RAM caps of this server, enforced by the OS from its next start (a running acServer keeps the ones it started with)."""
    s = _get(sess, server_id)
    s.cpu_limit, s.mem_limit_mb = body.cpu_percent, body.mem_mb
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return _out(s)


@router.put("/{server_id}/wake", response_model=ServerOut)
def set_wake(server_id: int, body: WakeIn, sess: SessionDep) -> ServerOut:
    """When a player trying to join a stopped server starts it: never / inside an event's window / always. See app/wake.py."""
    s = _get(sess, server_id)
    s.wake = body.mode
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return _out(s)


@router.get("/{server_id}/status")
def status(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    inst = supervisor.get(server_id)
    if not inst:
        return {"running": False, "returncode": None, "uptime": 0.0}
    return {"running": inst.running, "returncode": inst.exit_code, "uptime": inst.uptime}


@router.get("/{server_id}/logs")
def logs(server_id: int, sess: SessionDep, tail: int = 200) -> dict:
    _get(sess, server_id)
    inst = supervisor.get(server_id)
    return {"lines": list(inst.log)[-tail:] if inst else []}


def result_path(server_id: int, filename: str) -> Path:
    if "/" in filename or filename in (".", ".."):
        raise HTTPException(400, "invalid filename")
    return Path(settings.data_dir) / "instances" / str(server_id) / "results" / filename


@router.get("/{server_id}/results")
def list_results(server_id: int, sess: SessionDep) -> list[str]:
    _get(sess, server_id)
    d = Path(settings.data_dir) / "instances" / str(server_id) / "results"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


@router.get("/{server_id}/results/{filename}")
def download_result(server_id: int, filename: str, sess: SessionDep) -> FileResponse:
    _get(sess, server_id)
    p = result_path(server_id, filename)
    if not p.is_file():
        raise HTTPException(404, "result not found")
    return FileResponse(p)


@router.get("/{server_id}/results/{filename}/parsed")
def parsed_result(server_id: int, filename: str, sess: SessionDep, raw: bool = False) -> dict:
    """The classification with the stewards' penalties applied (`raw=true`: exactly what acServer wrote)."""
    _get(sess, server_id)
    p = result_path(server_id, filename)
    if not p.is_file():
        raise HTTPException(404, "result not found")
    parsed = parse_result_file(p)
    return parsed if raw else apply_penalties(parsed, penalties_for(sess, server_id, filename), non_racing_for(sess, server_id, filename))


# --- ACSP: live timing, chat, live map, admin actions ----------------------

def _acsp(server_id: int) -> ACSPClient:
    inst = supervisor.get(server_id)
    if not inst or not inst.acsp:
        raise HTTPException(409, "server not running or ACSP plugin not connected")
    return inst.acsp


@router.get("/{server_id}/session")
def session_info(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    return _acsp(server_id).session


@router.get("/{server_id}/cars")
def cars(server_id: int, sess: SessionDep) -> dict[int, dict]:
    _get(sess, server_id)
    return _acsp(server_id).snapshot()


@steward.post("/{server_id}/chat")
def send_chat(server_id: int, body: ChatIn, sess: SessionDep) -> dict:
    _get(sess, server_id)
    client = _acsp(server_id)
    if body.car_id is None:
        client.send(acsp.encode_broadcast_chat(body.message))
    else:
        client.send(acsp.encode_send_chat(body.car_id, body.message))
    return {"sent": True}


@steward.post("/{server_id}/kick/{car_id}")
def kick(server_id: int, car_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    _acsp(server_id).send(acsp.encode_kick_user(car_id))
    return {"sent": True}


@steward.post("/{server_id}/next_session")
def next_session(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    _acsp(server_id).send(acsp.encode_next_session())
    return {"sent": True}


@steward.post("/{server_id}/restart_session")
def restart_session(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    _acsp(server_id).send(acsp.encode_restart_session())
    return {"sent": True}


@steward.post("/{server_id}/session_info")
def set_session_info(server_id: int, body: SetSessionIn, sess: SessionDep) -> dict:
    """Redefine one session of the running server (ACSP SET_SESSION_INFO): name, type, laps, length in seconds."""
    _get(sess, server_id)
    _acsp(server_id).send(acsp.encode_set_session_info(body.index, body.name, body.session_type, body.laps, body.time_min, body.wait_s))
    return {"sent": True}


@steward.post("/{server_id}/admin")
def admin_command(server_id: int, body: AdminCommandIn, sess: SessionDep) -> dict:
    _get(sess, server_id)
    _acsp(server_id).send(acsp.encode_admin_command(body.command))
    return {"sent": True}


@router.websocket("/{server_id}/live")
async def live(websocket: WebSocket, server_id: int) -> None:
    """Streams new ACSP events (session/chat/car updates/laps) as JSON frames."""
    await websocket.accept()
    inst = supervisor.get(server_id)
    if not inst or not inst.acsp:
        await websocket.close(code=4409, reason="server not running or ACSP not connected")
        return
    sent = inst.acsp.n_events
    try:
        while True:
            total = inst.acsp.n_events
            events = list(inst.acsp.events)
            for event in events[max(0, len(events) - (total - sent)):]:
                await websocket.send_json(event)
            sent = total
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        pass


# --- lifecycle: adapters over app/services/server_service.py -----------------

@contextmanager
def _http():
    """The service refuses with ServerError; the routes answer with the same status and detail they always did."""
    try:
        yield
    except ServerError as e:
        raise HTTPException(e.status, e.detail) from e


async def apply_to_server(sess: SessionDep, s: Server, body: SessionIn, held: bool = False) -> AppliedOut:
    with _http():
        result = await server_service.apply(sess, s, body, held)
    if result.start_error:   # same status as ever; the message now says the configuration WAS saved
        raise HTTPException(result.start_error.status, "configuración guardada, arranque fallido: " + result.start_error.detail)
    return AppliedOut(**_out(s).model_dump(), restarted=result.restarted)


@router.post("/{server_id}/apply", response_model=AppliedOut)
async def apply_session(server_id: int, body: SessionIn, sess: SessionDep) -> AppliedOut:
    """Build server_cfg + entry list from the form, save them, and (optionally) restart the server with them."""
    return await apply_to_server(sess, _get(sess, server_id), body)


@router.post("/{server_id}/start")
async def start_server(server_id: int, sess: SessionDep) -> dict:
    with _http():
        return await server_service.start(sess, server_id)


@router.post("/{server_id}/stop")
async def stop_server(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    await server_service.stop(server_id)
    return {"running": False}
