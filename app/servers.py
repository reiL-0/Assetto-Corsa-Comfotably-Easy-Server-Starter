"""Server CRUD, server_cfg.ini / entry_list.ini rendering, and start/stop."""

from __future__ import annotations

import asyncio
import configparser
import threading
from contextlib import nullcontext
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from sqlmodel import Session, select

from app import content, csp, integrity, supervisor, timeline
from app.live import cspcmd, cspweather
from app.auth import require
from app.config import settings
from app.db import SessionDep
from app.live import acsp
from app.live.acsp import ACSPClient
from app.models import Server
from app.services import ini_generator
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







def _ports(base: int) -> dict[str, int]:
    return ini_generator.ports(base, settings.port_range_start, settings.port_range_end)


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


def _write_instance(s: Server) -> Path:
    d = Path(settings.data_dir) / "instances" / str(s.id)
    (d / "cfg").mkdir(parents=True, exist_ok=True)
    (d / "results").mkdir(exist_ok=True)
    # acServer reads content/ and system/ relative to its cwd: share the install's copies.
    bin_dir = settings.acserver_dir()
    for name in ("content", "system"):
        link = d / name
        if bin_dir and not link.exists() and (bin_dir / name).is_dir():
            link.symlink_to(bin_dir / name)
    (d / "cfg" / "server_cfg.ini").write_text(render_server_cfg(s))
    (d / "cfg" / "entry_list.ini").write_text(render_entry_list(s))
    welcome = d / "cfg" / "welcome.txt"
    if s.welcome or s.csp_extra.strip():
        welcome.write_text(csp.welcome_with_extra(s.welcome, s.csp_extra))
    else:
        welcome.unlink(missing_ok=True)
    return d


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


DEFAULT_WEATHER = {"GRAPHICS": "3_clear", "BASE_TEMPERATURE_AMBIENT": 18, "BASE_TEMPERATURE_ROAD": 6,
                   "VARIATION_AMBIENT": 1, "VARIATION_ROAD": 1}














def _check_content(body: SessionIn, wanted: list[str]) -> dict[str, list[str]]:
    """Track, layout and cars must be installed and loadable by acServer. Returns each car's skins."""
    tracks = {t["track"]: t for t in content.list_tracks()}
    t = tracks.get(body.track)
    if not t or not t["usable"]:
        raise HTTPException(400, f"track {body.track!r} is not installed")
    layouts = [c["config"] for c in t["configs"] if c["config"]]
    if body.track_config and body.track_config not in layouts:
        raise HTTPException(400, f"layout {body.track_config!r} is not installed for {body.track}")
    if layouts and not body.track_config and not (content._tracks_dir() / body.track / "data" / "surfaces.ini").is_file():
        raise HTTPException(400, f"{body.track} needs a layout: {', '.join(layouts)}")
    cars = {c["car"]: c for c in content.list_cars() if c["usable"]}
    missing = [c for c in wanted if c not in cars]
    if missing:
        raise HTTPException(400, f"cars not installed: {', '.join(missing)}")
    return {c: cars[c]["skins"] for c in wanted}


@router.post("/{server_id}/apply", response_model=AppliedOut)
async def apply_session(server_id: int, body: SessionIn, sess: SessionDep) -> AppliedOut:
    """Build server_cfg + entry list from the form, save them, and (optionally) restart the server with them."""
    return await apply_to_server(sess, _get(sess, server_id), body)


def _apply_options(cfg: dict, srv: dict, o: OptionsIn) -> None:
    """Write every option the form sent; leave the rest of the file alone."""
    for field, value in o.model_dump(exclude={"weather", "dynamic_track"}, exclude_none=True).items():
        srv[field.upper()] = int(value) if isinstance(value, bool) else value
    if o.weather is not None:
        for name in [k for k in cfg if k.startswith("WEATHER_")]:
            del cfg[name]
        for i, w in enumerate(o.weather):
            cfg[f"WEATHER_{i}"] = {
                "GRAPHICS": w.graphics, "BASE_TEMPERATURE_AMBIENT": w.ambient, "BASE_TEMPERATURE_ROAD": w.road,
                "VARIATION_AMBIENT": w.ambient_var, "VARIATION_ROAD": w.road_var,
                "WIND_BASE_SPEED_MIN": w.wind_min, "WIND_BASE_SPEED_MAX": max(w.wind_min, w.wind_max),
                "WIND_BASE_DIRECTION": w.wind_direction, "WIND_VARIATION_DIRECTION": w.wind_direction_var,
            }
    if o.dynamic_track is not None:
        cfg["DYNAMIC_TRACK"] = {k.upper(): v for k, v in o.dynamic_track.model_dump().items()}


def _clock_key(cfg: dict) -> tuple:
    """What defines the session clock (app/timeline.py): changing any of it restarts the clock; a typo fix in the name does not."""
    g = lambda sec, k: (cfg.get(sec) or {}).get(k)  # noqa: E731
    return (g("PRACTICE", "TIME"), g("QUALIFY", "TIME"), g("RACE", "LAPS"), g("RACE", "TIME"), g("SERVER", "TRACK"),
            g("SERVER", "CONFIG_TRACK"), g("SERVER", "LOOP_MODE"))


async def apply_to_server(sess: SessionDep, s: Server, body: SessionIn, held: bool = False) -> AppliedOut:
    """`held`: the caller already holds server_lock(s.id) (schedule does, to decide on `loaded` and apply as one step); the lock is not reentrant."""
    server_id = s.id
    if not (body.practice_min or body.qualify_min or body.race_laps or body.race_min):
        raise HTTPException(400, "enable at least one session (practice, qualify or race)")
    cars = list(dict.fromkeys(e.model for e in body.entries)) if body.entries else body.cars
    skins = _check_content(body, cars)
    guids = [g for e in body.entries for g in e.guid.split(";") if g]
    if len(guids) != len(set(guids)):
        raise HTTPException(400, "a Steam ID appears in more than one entry")
    if body.locked and not guids:
        raise HTTPException(400, "a locked entry list needs at least one entry with a Steam ID (nobody could join)")
    cfg = {name: dict(kv) for name, kv in s.config.items()}
    srv = cfg.setdefault("SERVER", {})
    slots = len(body.entries) or body.max_clients
    srv.update(NAME=body.name, PASSWORD=body.password, TRACK=body.track, CONFIG_TRACK=body.track_config,
               CARS=";".join(cars), MAX_CLIENTS=slots, LOCKED_ENTRY_LIST=int(body.locked),
               PICKUP_MODE_ENABLED=int(body.pickup))
    if body.admin_password is not None:
        srv["ADMIN_PASSWORD"] = body.admin_password
    srv.update(LOOP_MODE=int(body.loop), REVERSED_GRID_RACE_POSITIONS=body.reversed_grid)
    for key, value in (("SLEEP_TIME", 1), ("REGISTER_TO_LOBBY", 0)):
        srv.setdefault(key, value)  # without SLEEP_TIME acServer spins a core; without weather it panics
    for sec in ("PRACTICE", "QUALIFY", "RACE"):
        cfg.pop(sec, None)
    if body.practice_min:
        cfg["PRACTICE"] = {"NAME": "Practice", "TIME": body.practice_min, "IS_OPEN": 1}
    if body.qualify_min:
        cfg["QUALIFY"] = {"NAME": "Qualify", "TIME": body.qualify_min, "IS_OPEN": 1}
    if body.race_laps:
        cfg["RACE"] = {"NAME": "Race", "LAPS": body.race_laps, "WAIT_TIME": body.race_wait_s, "IS_OPEN": 1}
    elif body.race_min:   # timed race: acServer ends it when the time is up (LAPS=0)
        cfg["RACE"] = {"NAME": "Race", "LAPS": 0, "TIME": body.race_min, "WAIT_TIME": body.race_wait_s, "IS_OPEN": 1}
    _apply_options(cfg, srv, body.options)
    if not any(k.startswith("WEATHER_") for k in cfg):
        cfg["WEATHER_0"] = dict(DEFAULT_WEATHER)
    if body.restart or _clock_key(cfg) != _clock_key(s.config):
        s.anchor_index = s.anchor_at = None   # a different session set-up (or a real restart): the clock starts over with the next start
    s.config = cfg
    s.welcome = body.welcome.strip()
    s.session = body.model_dump(exclude={"admin_password", "restart"})
    if body.entries:
        s.entry_list = [
            {"MODEL": e.model, "SKIN": e.skin or (skins[e.model] or [""])[0], "SPECTATOR_MODE": int(e.spectator),
             "DRIVERNAME": e.driver_name, "TEAM": e.team, "GUID": e.guid, "BALLAST": e.ballast, "RESTRICTOR": e.restrictor}
            for e in body.entries
        ]
    else:  # one open slot per client, cars taken in turns; the skin is the pack's first (clients pick their own)
        s.entry_list = [
            {"MODEL": car, "SKIN": (skins[car] or [""])[0]}
            for car in (cars[i % len(cars)] for i in range(body.max_clients))
        ]
    s.updated_at = datetime.now(UTC)
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    restarted = False
    if body.restart:
        async with (nullcontext() if held else server_lock(server_id)):   # stop + start as ONE step: a wake / API start / event end cannot slip in between
            await supervisor.stop(server_id)
            await _start_unlocked(server_id, sess)
        restarted = True
    return AppliedOut(**_out(s).model_dump(), restarted=restarted)


# One lock per server for the COMPOSITE operations (apply = stop + start, start with its INI files, stop). supervisor has its own per-server
# lock inside start/stop; the order is always servers -> supervisor, so there is no deadlock (nothing in supervisor takes this one).
# ponytail: in-process (asyncio), like the supervisor registry: one manager process; a second process would need a file/DB lock.
server_locks: dict[int, asyncio.Lock] = {}


def server_lock(server_id: int) -> asyncio.Lock:
    return server_locks.setdefault(server_id, asyncio.Lock())


@router.post("/{server_id}/start")
async def start_server(server_id: int, sess: SessionDep) -> dict:
    async with server_lock(server_id):   # the INI files are written under it too: two starts never rewrite them while acServer reads them
        return await _start_unlocked(server_id, sess)


async def stop_instance(server_id: int, inst: supervisor.Instance, reason: str) -> None:
    """Automatic stops (end of an event): same exclusion as the others, and only if `inst` is still the registered one (an apply may have replaced it)."""
    async with server_lock(server_id):
        await supervisor.stop(server_id, reason=reason, only=inst)


async def _start_unlocked(server_id: int, sess: Session) -> dict:
    s = _get(sess, server_id)
    if not settings.acserver_cmd:
        raise HTTPException(400, "ACM_ACSERVER_CMD is not configured")
    integrity.gate(sess, s)   # 409 in «require» mode when the content differs from its seal
    planned, planned_at = timeline.server_position(s), time.time()   # where the session clock is, read before acServer re-anchors it
    p = _ports(s.base_port)
    try:
        inst = await supervisor.start(
            server_id,
            _write_instance(s),
            acsp_remote_port=p["plugin"],
            acsp_local_port=p["plugin_local"],
            http_port=p["http_internal"],
            cpu_percent=s.cpu_limit,
            mem_mb=s.mem_limit_mb,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    inst.set_weather_plan(s.weather_plan, s.config)
    timeline.start_resume(server_id, planned, planned_at)   # if the session clock ran while the server was off, move it to where the clock is
    return {"running": inst.running, "pid": inst.pid}










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


async def adopt_running(sess: Session) -> int:
    """At boot: take back every acServer a previous manager process left running (see supervisor.adopt)."""
    n = 0
    for s in sess.exec(select(Server)).all():
        p = _ports(s.base_port)
        inst = await supervisor.adopt(
            s.id, Path(settings.data_dir) / "instances" / str(s.id),
            acsp_remote_port=p["plugin"], acsp_local_port=p["plugin_local"], car_slots=len(s.entry_list), http_port=p["http_internal"],
        )
        if inst:
            inst.set_weather_plan(s.weather_plan, s.config)
        n += inst is not None
    return n


@router.post("/{server_id}/stop")
async def stop_server(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    async with server_lock(server_id):
        await supervisor.stop(server_id)
    return {"running": False}


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
