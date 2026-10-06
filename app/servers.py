"""Server CRUD, server_cfg.ini / entry_list.ini rendering, and start/stop."""

from __future__ import annotations

import asyncio
import configparser
import io
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.requests import HTTPConnection
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, model_validator
from sqlmodel import Session, select

from app import content, csp, integrity, supervisor, timeline
from app.live import cspcmd, cspweather
from app import tenancy, tenantcontent
from app.auth import CurrentUser, require
from app.config import settings
from app.db import SessionDep, engine
from app.live import acsp
from app.live.acsp import ACSPClient
from app.models import Plan, Server
from app.results import apply_penalties, non_racing_for, parse_result_file, penalties_for

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
    wake: str = "window"
    tenant_id: int | None = None
    limits: dict = {}  # {cpu_percent, mem_mb, enforced}: caps of this server, applied the next time it starts (supervisor.limit_prefix)
    integrity: str = "warn"
    integrity_extras: bool = False
    welcome: str = ""
    csp_extra: str = ""
    weather_plan: dict | None = None  # played to CSP clients as hidden chat commands (app/live/cspweather.py)
    session: dict | None = None  # the last SessionIn applied through /apply (None for a server never set up from the panel)


def _ports(base: int) -> dict[str, int]:
    # plugin: acServer's own UDP_PLUGIN_LOCAL_PORT. plugin_local: our side of
    # the ACSP socket (UDP_PLUGIN_ADDRESS), one pair per 4-port block.
    # http: the port players and Content Manager use (the manager answers there, app/wake.py); http_internal: where acServer's own
    # HTTP listens (outside the blocks, one per block, still open to the world: the game's UDP ping names it)
    internal = settings.port_range_end + (base - settings.port_range_start) // 4
    return {"tcp": base, "udp": base, "http": base + 1, "plugin": base + 2, "plugin_local": base + 3, "http_internal": internal}


def _out(s: Server) -> ServerOut:
    return ServerOut(
        id=s.id,
        name=s.name,
        base_port=s.base_port,
        ports=_ports(s.base_port),
        config=s.config,
        entry_list=s.entry_list,
        wake=s.wake,
        tenant_id=s.tenant_id,
        limits={"cpu_percent": s.cpu_limit, "mem_mb": s.mem_limit_mb, "enforced": settings.limits_scope in ("user", "system")},
        welcome=s.welcome,
        csp_extra=s.csp_extra,
        weather_plan=s.weather_plan,
        session=s.session,
        integrity=s.integrity,
        integrity_extras=s.integrity_extras,
    )


# --- INI rendering -----------------------------------------------------------

def _ini_value(v: Scalar) -> str:
    if isinstance(v, bool):  # bool before int: AC wants 1/0
        return "1" if v else "0"
    return str(v)


def _render_ini(sections: dict[str, dict[str, Scalar]]) -> str:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # keep KEY casing
    for name, kv in sections.items():   # no line breaks anywhere: a value must not be able to add keys to the file acServer reads
        cp[tenancy.clean_text(name)] = {tenancy.clean_text(k): tenancy.clean_text(_ini_value(v)) for k, v in kv.items()}
    buf = io.StringIO()
    cp.write(buf, space_around_delimiters=False)
    return buf.getvalue()


def render_server_cfg(s: Server, plan: Plan | None = None) -> str:
    """server_cfg.ini with allocated ports merged into [SERVER] (user values win; a customer's plan forces ports, slots and plain content names)."""
    sections = {name: dict(kv) for name, kv in s.config.items()}
    server = sections.setdefault("SERVER", {})
    p = _ports(s.base_port)
    server.setdefault("TCP_PORT", p["tcp"])
    server.setdefault("UDP_PORT", p["udp"])
    server["HTTP_PORT"] = p["http_internal"]   # the manager owns the public HTTP port
    server.setdefault("UDP_PLUGIN_LOCAL_PORT", p["plugin"])
    if s.welcome or s.csp_extra.strip():
        server["WELCOME_MESSAGE"] = "cfg/welcome.txt"   # relative to the instance directory, acServer's working directory
    else:
        server.pop("WELCOME_MESSAGE", None)
    server.setdefault("UDP_PLUGIN_ADDRESS", f"127.0.0.1:{p['plugin_local']}")
    if plan:
        sections, _ = tenancy.clamp_config(sections, s.entry_list, plan, p)
    return _render_ini(sections)


def render_entry_list(s: Server, plan: Plan | None = None) -> str:
    cars = s.entry_list[: plan.slots] if plan else s.entry_list
    return _render_ini({f"CAR_{i}": car for i, car in enumerate(cars)})


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
        if name == "content" and s.tenant_id is not None:
            continue   # a customer's server gets a content/ made only of what that customer holds (below)
        link = d / name
        if bin_dir and not link.exists() and (bin_dir / name).is_dir():
            link.symlink_to(bin_dir / name)
    with Session(engine) as sess:
        plan = tenancy.plan_of(sess, s)   # a customer's server is written within its plan
    (d / "cfg" / "server_cfg.ini").write_text(render_server_cfg(s, plan))
    (d / "cfg" / "entry_list.ini").write_text(render_entry_list(s, plan))
    if s.tenant_id is not None:
        tenantcontent.compose(s, d)
    welcome = d / "cfg" / "welcome.txt"
    if s.welcome or s.csp_extra.strip():
        welcome.write_text(csp.welcome_with_extra(s.welcome, s.csp_extra))
    else:
        welcome.unlink(missing_ok=True)
    return d


# --- routes --------------------------------------------------------------

@router.post("", response_model=ServerOut, status_code=201)
def create(body: ServerIn, sess: SessionDep, user: CurrentUser) -> ServerOut:
    plan = tenancy.enforce_new_server(sess, user)   # a customer: its tenant must be active and under the plan's server limit
    s = Server(
        tenant_id=user.tenant_id,
        integrity="off" if plan else "warn",   # the seals cover the shared content/, not a customer's
        cpu_limit=plan.cpu_percent if plan else None,
        mem_limit_mb=plan.mem_mb if plan else None,
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
def list_servers(sess: SessionDep, conn: HTTPConnection) -> list[ServerOut]:
    return [_out(s) for s in tenancy.visible(conn, list(sess.exec(select(Server)).all()))]


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
    s = _get(sess, server_id)
    return render_server_cfg(s, tenancy.plan_of(sess, s))


@router.get("/{server_id}/entry_list.ini", response_class=PlainTextResponse)
def entry_list_ini(server_id: int, sess: SessionDep) -> str:
    s = _get(sess, server_id)
    return render_entry_list(s, tenancy.plan_of(sess, s))


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


class WeatherIn(BaseModel):
    """One [WEATHER_n] block. acServer cycles through them; the name is only sent to the clients (they need it installed). With CSP the name
    ends in `_type=<WeatherFX id>` (3_clear_type=15, 7_heavy_clouds_type=7 for rain): CSP clients show that weather, others the stock folder."""

    graphics: str = Field(default="3_clear", pattern=r"^[\w.\-=]{1,80}$")   # CSP weather names carry _type=<id> (WeatherFX type)
    ambient: int = Field(default=18, ge=-10, le=50)  # °C
    road: int = Field(default=6, ge=-20, le=50)  # °C ABOVE the ambient (acServer's BASE_TEMPERATURE_ROAD is relative)
    ambient_var: int = Field(default=1, ge=0, le=20)
    road_var: int = Field(default=1, ge=0, le=20)
    wind_min: int = Field(default=0, ge=0, le=60)  # km/h
    wind_max: int = Field(default=0, ge=0, le=60)
    wind_direction: int = Field(default=0, ge=0, le=359)  # degrees
    wind_direction_var: int = Field(default=0, ge=0, le=359)


class DynamicTrackIn(BaseModel):
    session_start: int = Field(default=95, ge=0, le=100)  # grip % when the session starts
    randomness: int = Field(default=2, ge=0, le=100)
    session_transfer: int = Field(default=90, ge=0, le=100)  # % of the grip carried to the next session
    lap_gain: int = Field(default=130, ge=0, le=1000)  # laps for the track to gain one grip point


class OptionsIn(BaseModel):
    """server_cfg.ini [SERVER] options. A field left out (None) keeps whatever the server has now; names are the
    INI keys in lower case, so `field.upper()` is the key."""

    sun_angle: int | None = Field(default=None, ge=-80, le=80)  # time of day: 0 = 13:00, 16 degrees per hour
    time_of_day_mult: int | None = Field(default=None, ge=0, le=100)  # clock speed
    abs_allowed: int | None = Field(default=None, ge=0, le=2)  # 0 off, 1 factory, 2 forced on
    tc_allowed: int | None = Field(default=None, ge=0, le=2)
    stability_allowed: bool | None = None
    autoclutch_allowed: bool | None = None
    tyre_blankets_allowed: bool | None = None
    force_virtual_mirror: bool | None = None
    damage_multiplier: int | None = Field(default=None, ge=0, le=100)  # %
    fuel_rate: int | None = Field(default=None, ge=0, le=500)  # % of normal consumption
    tyre_wear_rate: int | None = Field(default=None, ge=0, le=500)
    allowed_tyres_out: int | None = Field(default=None, ge=-1, le=4)  # wheels outside the line before a cut; -1 = never
    legal_tyres: str | None = Field(default=None, pattern=r"^[\w;]{0,60}$")  # e.g. "SV;S;M;H"
    max_ballast_kg: int | None = Field(default=None, ge=0, le=500)
    start_rule: int | None = Field(default=None, ge=0, le=2)  # 0 locked until green, 1 teleport to pits, 2 drive-through
    race_gas_penalty_disabled: bool | None = None
    max_contacts_per_km: int | None = Field(default=None, ge=-1, le=50)  # -1 = off
    race_over_time: int | None = Field(default=None, ge=0, le=3600)  # s the race stays open after the winner
    result_screen_time: int | None = Field(default=None, ge=0, le=600)
    qualify_max_wait_perc: int | None = Field(default=None, ge=100, le=1000)
    race_pit_window_start: int | None = Field(default=None, ge=0, le=720)  # minutes; 0 and 0 = no window
    race_pit_window_end: int | None = Field(default=None, ge=0, le=720)
    kick_quorum: int | None = Field(default=None, ge=0, le=100)  # % of votes
    voting_quorum: int | None = Field(default=None, ge=0, le=100)
    vote_duration: int | None = Field(default=None, ge=1, le=300)
    blacklist_mode: int | None = Field(default=None, ge=0, le=2)  # 0 plain kick, 1 until restart, 2 permanent ban
    client_send_interval_hz: int | None = Field(default=None, ge=10, le=60)
    weather: list[WeatherIn] | None = Field(default=None, max_length=10)  # None = keep the current blocks
    dynamic_track: DynamicTrackIn | None = None


class EntryIn(BaseModel):
    """One slot of the entry list. With a `guid` only that driver can take it (needs the list locked or not)."""

    model: str
    skin: str = ""
    driver_name: str = Field(default="", max_length=60)
    team: str = Field(default="", max_length=60)
    guid: str = Field(default="", pattern=r"^(\d{17}(;\d{17})*)?$")  # SteamID64; several joined by ';' share the car
    ballast: int = Field(default=0, ge=0, le=300)  # kg
    restrictor: int = Field(default=0, ge=0, le=100)  # %
    spectator: bool = False


class SessionIn(BaseModel):
    """What the admin panel's "new session" form sends."""

    name: str = Field(min_length=1, max_length=80)
    password: str = ""  # join password; "" = open
    admin_password: str | None = None  # None = keep the current one
    track: str
    track_config: str = ""
    cars: list[str] = []  # open slots are spread over these; ignored when `entries` is given
    max_clients: int = Field(default=10, ge=1, le=50)  # ...as is this: the entry list sets the slot count
    entries: list[EntryIn] = Field(default_factory=list, max_length=50)
    options: OptionsIn = Field(default_factory=OptionsIn)
    locked: bool = False  # only the Steam IDs in `entries` may join (LOCKED_ENTRY_LIST)
    pickup: bool = True  # drivers without a reserved slot pick a free one on joining
    welcome: str = Field(default="", max_length=2000)  # shown to a driver when joining (the session's rules, the league's link...)
    practice_min: int | None = Field(default=None, ge=0, le=720)
    qualify_min: int | None = Field(default=None, ge=0, le=720)
    race_laps: int | None = Field(default=None, ge=0, le=999)
    race_min: int | None = Field(default=None, ge=0, le=1440)  # a timed race (endurance) instead of laps; not both
    race_wait_s: int = Field(default=60, ge=0, le=600)
    reversed_grid: int = Field(default=0, ge=-1, le=50)  # race grid: 0 as qualified, N = invert the first N, -1 = all
    loop: bool = True  # start over after the last session (practice -> qualify -> race -> practice ...)
    restart: bool = True

    @model_validator(mode="after")
    def _has_cars(self) -> SessionIn:
        if not self.cars and not self.entries:
            raise ValueError("choose cars or fill the entry list")
        if self.race_laps and self.race_min:
            raise ValueError("a race is by laps or by time, not both")
        return self


class AppliedOut(ServerOut):
    restarted: bool


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


async def apply_to_server(sess: SessionDep, s: Server, body: SessionIn) -> AppliedOut:
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
        await supervisor.stop(server_id)
        await start_server(server_id, sess)
        restarted = True
    return AppliedOut(**_out(s).model_dump(), restarted=restarted)


@router.post("/{server_id}/start")
async def start_server(server_id: int, sess: SessionDep) -> dict:
    s = _get(sess, server_id)
    if not settings.acserver_cmd:
        raise HTTPException(400, "ACM_ACSERVER_CMD is not configured")
    if s.tenant_id is not None and (miss := tenantcontent.missing(s)):
        raise HTTPException(409, "your content is missing: " + ", ".join(miss) + " (upload it first)")
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
            cpu_percent=tenancy.limits_for(sess, s)[0],   # a customer's server starts with its plan's caps
            mem_mb=tenancy.limits_for(sess, s)[1],
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    inst.set_weather_plan(s.weather_plan, s.config)
    timeline.start_resume(server_id, planned, planned_at)   # if the session clock ran while the server was off, move it to where the clock is
    return {"running": inst.running, "pid": inst.pid}


class WakeIn(BaseModel):
    mode: Literal["off", "window", "always"]


class WeatherEntryIn(BaseModel):
    """One weather of the plan, as in AC Server Manager's editor (app/live/weatherplan.py)."""

    type: int = Field(default=15, ge=0, le=32)          # WeatherFX type id: 15 clear, 7 rain, 8 heavy rain, 1 thunderstorm...
    duration_min: float = Field(default=0, ge=0, le=1440)   # real minutes before moving to the next one; 0 = until the session ends
    sessions: list[Literal["practice", "qualify", "race"]] = ["practice", "qualify", "race"]
    ambient: float = Field(default=20, ge=-10, le=50)
    road: float = Field(default=6, ge=-20, le=40)        # added to the ambient temperature
    ambient_var: float = Field(default=0, ge=0, le=20)
    road_var: float = Field(default=0, ge=0, le=20)
    wind_min: float = Field(default=0, ge=0, le=40)      # m/s
    wind_max: float = Field(default=0, ge=0, le=40)
    wind_dir: float = Field(default=0, ge=0, le=360)
    wind_dir_var: float = Field(default=0, ge=0, le=180)


class LiveWeatherIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    refresh_min: float = Field(default=10, ge=1, le=120)


class WeatherPlanIn(BaseModel):
    mode: Literal["entries", "live"] = "entries"        # entries: the list below per session; live: the real weather of `live` (lat, lon)
    entries: list[WeatherEntryIn] = Field(default=[], max_length=8)   # few changes: each one makes every client recompute clouds and rain (frame drops on weaker PCs)
    transition_s: float = Field(default=90, ge=20, le=900)          # each change is a smooth blend of this many seconds (long ones are gentler)
    update_s: float = Field(default=30, ge=5, le=120)               # seconds between commands to the clients
    live: LiveWeatherIn | None = None
    driving: Literal["real", "visual"] = "real"                    # visual: the weather is only seen (grip 100 %, no water on the track); real: it changes the grip
    sun_angle: int | None = Field(default=None, ge=-80, le=80)      # sun position sent to CSP clients (0 = 13:00, 16 degrees per hour); None = the server's SUN_ANGLE

    @model_validator(mode="after")
    def _complete(self) -> WeatherPlanIn:
        if self.mode == "entries" and not self.entries:
            raise ValueError("entries: at least one weather")
        if self.mode == "live" and not self.live:
            raise ValueError("live: latitude and longitude are required")
        return self


def _restart_weather(s: Server) -> None:
    inst = supervisor.get(s.id)
    if inst:
        inst.set_weather_plan(s.weather_plan, s.config)


@router.put("/{server_id}/weather_plan", response_model=ServerOut)
def set_weather_plan(server_id: int, body: WeatherPlanIn, sess: SessionDep) -> ServerOut:
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
def clear_weather_plan(server_id: int, sess: SessionDep) -> None:
    s = _get(sess, server_id)
    s.weather_plan = None
    sess.add(s)
    sess.commit()
    _restart_weather(s)


class CspWeatherIn(BaseModel):
    current: int = Field(default=15, ge=0, le=32)
    upcoming: int | None = Field(default=None, ge=0, le=32)
    transition: float = Field(default=0, ge=0, le=1)
    ambient: float = Field(default=20, ge=-10, le=50)
    road: float = Field(default=22, ge=-10, le=70)
    grip: float = Field(default=1.0, ge=0.6, le=1.0)
    rain: float = Field(default=0, ge=0, le=1)
    wetness: float = Field(default=0, ge=0, le=1)
    water: float = Field(default=0, ge=0, le=1)


@steward.post("/{server_id}/csp_weather")
def send_csp_weather(server_id: int, body: CspWeatherIn, sess: SessionDep) -> dict:
    """Send these conditions to every CSP client once, right now (a test, or a manual weather change). A running plan overwrites them at its next step."""
    _get(sess, server_id)
    text = cspweather.command({**body.model_dump(), "upcoming": body.current if body.upcoming is None else body.upcoming}, int(time.time()))
    _acsp(server_id).send(acsp.encode_broadcast_chat(text))
    return {"sent": True, "chars": len(text)}


class CspExtraIn(BaseModel):
    text: str = Field(default="", max_length=6000)  # INI; "" removes it


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


class LimitsIn(BaseModel):
    cpu_percent: int | None = Field(default=None, ge=10, le=800)   # 100 = one core; None = unlimited
    mem_mb: int | None = Field(default=None, ge=256, le=65536)     # None = unlimited


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


class SetSessionIn(BaseModel):
    index: int = Field(ge=0, le=7)
    name: str = Field(min_length=1, max_length=40)
    session_type: int = Field(ge=1, le=3)
    laps: int = Field(default=0, ge=0, le=999)
    time_min: int = Field(default=0, ge=0, le=1440)
    wait_s: int = Field(default=0, ge=0, le=600)


@steward.post("/{server_id}/session_info")
def set_session_info(server_id: int, body: SetSessionIn, sess: SessionDep) -> dict:
    """Redefine one session of the running server (ACSP SET_SESSION_INFO): name, type, laps, length in seconds."""
    _get(sess, server_id)
    _acsp(server_id).send(acsp.encode_set_session_info(body.index, body.name, body.session_type, body.laps, body.time_min, body.wait_s))
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
