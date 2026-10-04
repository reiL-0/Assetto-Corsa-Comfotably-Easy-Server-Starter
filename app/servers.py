"""Server CRUD, server_cfg.ini / entry_list.ini rendering, and start/stop."""

from __future__ import annotations

import asyncio
import configparser
import io
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from app import content, supervisor
from app.auth import require
from app.config import settings
from app.db import SessionDep
from app.live import acsp
from app.live.acsp import ACSPClient
from app.models import Server
from app.results import parse_result_file

router = APIRouter(prefix="/servers", tags=["servers"])
# Live moderation actions: stewards may use these; everything else on `router` is admin-write.
steward = APIRouter(prefix="/servers", tags=["servers"], dependencies=[Depends(require("steward"))])

Scalar = str | int | float | bool


class ServerIn(BaseModel):
    name: str = Field(min_length=1)
    config: dict[str, dict[str, Scalar]] = {}
    entry_list: list[dict[str, Scalar]] = []


class ServerOut(BaseModel):
    id: int
    name: str
    base_port: int
    ports: dict[str, int]
    config: dict[str, dict[str, Scalar]]
    entry_list: list[dict[str, Scalar]]


def _ports(base: int) -> dict[str, int]:
    # plugin: acServer's own UDP_PLUGIN_LOCAL_PORT. plugin_local: our side of
    # the ACSP socket (UDP_PLUGIN_ADDRESS), one pair per 4-port block.
    return {"tcp": base, "udp": base, "http": base + 1, "plugin": base + 2, "plugin_local": base + 3}


def _out(s: Server) -> ServerOut:
    return ServerOut(
        id=s.id,
        name=s.name,
        base_port=s.base_port,
        ports=_ports(s.base_port),
        config=s.config,
        entry_list=s.entry_list,
    )


# --- INI rendering -----------------------------------------------------------

def _ini_value(v: Scalar) -> str:
    if isinstance(v, bool):  # bool before int: AC wants 1/0
        return "1" if v else "0"
    return str(v)


def _render_ini(sections: dict[str, dict[str, Scalar]]) -> str:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # keep KEY casing
    for name, kv in sections.items():
        cp[name] = {k: _ini_value(v) for k, v in kv.items()}
    buf = io.StringIO()
    cp.write(buf, space_around_delimiters=False)
    return buf.getvalue()


def render_server_cfg(s: Server) -> str:
    """server_cfg.ini with allocated ports merged into [SERVER] (user values win)."""
    sections = {name: dict(kv) for name, kv in s.config.items()}
    server = sections.setdefault("SERVER", {})
    p = _ports(s.base_port)
    server.setdefault("TCP_PORT", p["tcp"])
    server.setdefault("UDP_PORT", p["udp"])
    server.setdefault("HTTP_PORT", p["http"])
    server.setdefault("UDP_PLUGIN_LOCAL_PORT", p["plugin"])
    server.setdefault("UDP_PLUGIN_ADDRESS", f"127.0.0.1:{p['plugin_local']}")
    return _render_ini(sections)


def render_entry_list(s: Server) -> str:
    return _render_ini({f"CAR_{i}": car for i, car in enumerate(s.entry_list)})


# --- persistence helpers ---------------------------------------------------

def _get(sess: SessionDep, server_id: int) -> Server:
    s = sess.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    return s


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
        if not link.exists() and (bin_dir / name).is_dir():
            link.symlink_to(bin_dir / name)
    (d / "cfg" / "server_cfg.ini").write_text(render_server_cfg(s))
    (d / "cfg" / "entry_list.ini").write_text(render_entry_list(s))
    return d


# --- routes --------------------------------------------------------------

@router.post("", response_model=ServerOut, status_code=201)
def create(body: ServerIn, sess: SessionDep) -> ServerOut:
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


DEFAULT_WEATHER = {"GRAPHICS": "3_clear", "BASE_TEMPERATURE_AMBIENT": 18, "BASE_TEMPERATURE_ROAD": 24,
                   "VARIATION_AMBIENT": 1, "VARIATION_ROAD": 1}


class SessionIn(BaseModel):
    """What the admin panel's "new session" form sends."""

    name: str = Field(min_length=1, max_length=80)
    password: str = ""  # join password; "" = open
    admin_password: str | None = None  # None = keep the current one
    track: str
    track_config: str = ""
    cars: list[str] = Field(min_length=1)
    max_clients: int = Field(ge=1, le=50)
    practice_min: int | None = Field(default=None, ge=0, le=720)
    qualify_min: int | None = Field(default=None, ge=0, le=720)
    race_laps: int | None = Field(default=None, ge=0, le=999)
    race_wait_s: int = Field(default=60, ge=0, le=600)
    restart: bool = True


class AppliedOut(ServerOut):
    restarted: bool


def _check_content(body: SessionIn) -> dict[str, list[str]]:
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
    missing = [c for c in body.cars if c not in cars]
    if missing:
        raise HTTPException(400, f"cars not installed: {', '.join(missing)}")
    return {c: cars[c]["skins"] for c in body.cars}


@router.post("/{server_id}/apply", response_model=AppliedOut)
async def apply_session(server_id: int, body: SessionIn, sess: SessionDep) -> AppliedOut:
    """Build server_cfg + entry list from the form, save them, and (optionally) restart the server with them."""
    s = _get(sess, server_id)
    if not (body.practice_min or body.qualify_min or body.race_laps):
        raise HTTPException(400, "enable at least one session (practice, qualify or race)")
    skins = _check_content(body)
    cfg = {name: dict(kv) for name, kv in s.config.items()}
    srv = cfg.setdefault("SERVER", {})
    srv.update(NAME=body.name, PASSWORD=body.password, TRACK=body.track, CONFIG_TRACK=body.track_config,
               CARS=";".join(body.cars), MAX_CLIENTS=body.max_clients)
    if body.admin_password is not None:
        srv["ADMIN_PASSWORD"] = body.admin_password
    for key, value in (("SLEEP_TIME", 1), ("PICKUP_MODE_ENABLED", 1), ("LOOP_MODE", 1), ("REGISTER_TO_LOBBY", 0)):
        srv.setdefault(key, value)  # without SLEEP_TIME acServer spins a core; without weather it panics
    for sec in ("PRACTICE", "QUALIFY", "RACE"):
        cfg.pop(sec, None)
    if body.practice_min:
        cfg["PRACTICE"] = {"NAME": "Practice", "TIME": body.practice_min, "IS_OPEN": 1}
    if body.qualify_min:
        cfg["QUALIFY"] = {"NAME": "Qualify", "TIME": body.qualify_min, "IS_OPEN": 1}
    if body.race_laps:
        cfg["RACE"] = {"NAME": "Race", "LAPS": body.race_laps, "WAIT_TIME": body.race_wait_s, "IS_OPEN": 1}
    if not any(k.startswith("WEATHER_") for k in cfg):
        cfg["WEATHER_0"] = dict(DEFAULT_WEATHER)
    s.config = cfg
    # one slot per client, cars taken in turns; the skin is the pack's first one (clients pick their own in pickup mode)
    s.entry_list = [
        {"MODEL": car, "SKIN": (skins[car] or [""])[0]}
        for car in (body.cars[i % len(body.cars)] for i in range(body.max_clients))
    ]
    s.updated_at = datetime.now(UTC)
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    restarted = False
    if body.restart:
        await supervisor.stop(server_id)
        await start_server(server_id, sess)
        restarted = True
    return AppliedOut(**_out(s).model_dump(), restarted=restarted)


@router.post("/{server_id}/start")
async def start_server(server_id: int, sess: SessionDep) -> dict:
    s = _get(sess, server_id)
    if not settings.acserver_cmd:
        raise HTTPException(400, "ACM_ACSERVER_CMD is not configured")
    p = _ports(s.base_port)
    try:
        inst = await supervisor.start(
            server_id,
            _write_instance(s),
            acsp_remote_port=p["plugin"],
            acsp_local_port=p["plugin_local"],
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"running": inst.running, "pid": inst.proc.pid}


@router.post("/{server_id}/stop")
async def stop_server(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    await supervisor.stop(server_id)
    return {"running": False}


@router.get("/{server_id}/status")
def status(server_id: int, sess: SessionDep) -> dict:
    _get(sess, server_id)
    inst = supervisor.get(server_id)
    if not inst:
        return {"running": False, "returncode": None, "uptime": 0.0}
    return {"running": inst.running, "returncode": inst.proc.returncode, "uptime": inst.uptime}


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
def parsed_result(server_id: int, filename: str, sess: SessionDep) -> dict:
    _get(sess, server_id)
    p = result_path(server_id, filename)
    if not p.is_file():
        raise HTTPException(404, "result not found")
    return parse_result_file(p)


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


class ChatIn(BaseModel):
    message: str
    car_id: int | None = None  # None -> broadcast to everyone


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


class AdminCommandIn(BaseModel):
    command: str  # e.g. "ballast 3 50", "restrict 3 10" -- console admin commands


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
