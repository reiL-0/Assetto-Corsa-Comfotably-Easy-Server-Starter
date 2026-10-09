"""Lifecycle of a managed server as the domain sees it: apply a session, start, stop. No HTTP in here.

`app/servers.py` (the routes), `app/schedule.py` (the scheduler, the wake) and `app/events.py` (run an event) all call these; the routes
translate `ServerError` into an `HTTPException` with the same status and detail they always had.

Locking: ONE per-server lock (`server_lock`) makes the composite operations atomic (apply = stop + start, a start with its INI files,
a stop). `supervisor` has its own lock inside start/stop; the order is always server_lock -> supervisor, so there is no deadlock.
Callers that already hold the lock (the scheduler deciding `loaded` and applying as one step) pass `held=True`; the lock is not reentrant.

The database session is the caller's (`sess`): the service commits what it changes and never opens one.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import Session, select

from app import content, csp, integrity, supervisor, timeline
from app.config import settings
from app.models import Server
from app.schemas.servers import OptionsIn, SessionIn
from app.services import ini_generator


class ServerError(Exception):
    """A refusal the caller should show: `status` is the HTTP-equivalent code, `detail` the message (the routes pass both through)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class ApplyResult:
    """What `apply` did. A refusal BEFORE anything is saved (bad session, content missing) is a raised ServerError; once the configuration is
    committed the outcome is this: `restarted` (it was (re)started with the new config) or `start_error` (config saved, start failed: the
    server keeps the new config and a later start / retry uses it)."""

    restarted: bool = False
    start_error: ServerError | None = None


def ports(base: int) -> dict[str, int]:
    return ini_generator.ports(base, settings.port_range_start, settings.port_range_end)


def _load(sess: Session, server_id: int) -> Server:
    s = sess.get(Server, server_id)
    if not s:
        raise ServerError(404, "server not found")
    return s


DEFAULT_WEATHER = {"GRAPHICS": "3_clear", "BASE_TEMPERATURE_AMBIENT": 18, "BASE_TEMPERATURE_ROAD": 6,
                   "VARIATION_AMBIENT": 1, "VARIATION_ROAD": 1}


# One lock per server for the COMPOSITE operations (apply = stop + start, start with its INI files, stop). supervisor has its own per-server
# lock inside start/stop; the order is always servers -> supervisor, so there is no deadlock (nothing in supervisor takes this one).
# ponytail: in-process (asyncio), like the supervisor registry: one manager process; a second process would need a file/DB lock.
server_locks: dict[int, asyncio.Lock] = {}


def server_lock(server_id: int) -> asyncio.Lock:
    return server_locks.setdefault(server_id, asyncio.Lock())


def check_content(body: SessionIn, wanted: list[str]) -> dict[str, list[str]]:
    """Track, layout and cars must be installed and loadable by acServer. Returns each car's skins."""
    tracks = {t["track"]: t for t in content.list_tracks()}
    t = tracks.get(body.track)
    if not t or not t["usable"]:
        raise ServerError(400, f"track {body.track!r} is not installed")
    layouts = [c["config"] for c in t["configs"] if c["config"]]
    if body.track_config and body.track_config not in layouts:
        raise ServerError(400, f"layout {body.track_config!r} is not installed for {body.track}")
    if layouts and not body.track_config and not (content._tracks_dir() / body.track / "data" / "surfaces.ini").is_file():
        raise ServerError(400, f"{body.track} needs a layout: {', '.join(layouts)}")
    cars = {c["car"]: c for c in content.list_cars() if c["usable"]}
    missing = [c for c in wanted if c not in cars]
    if missing:
        raise ServerError(400, f"cars not installed: {', '.join(missing)}")
    return {c: cars[c]["skins"] for c in wanted}


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


async def start_unlocked(server_id: int, sess: Session) -> dict:
    s = _load(sess, server_id)
    if not settings.acserver_cmd:
        raise ServerError(400, "ACM_ACSERVER_CMD is not configured")
    try:
        integrity.gate(sess, s)   # 409 in «require» mode when the content differs from its seal
    except HTTPException as e:   # ponytail: gate still raises the HTTP type (it is also a route helper of app/integrity.py); translated here, once
        raise ServerError(e.status_code, str(e.detail)) from e
    planned, planned_at = timeline.server_position(s), time.time()   # where the session clock is, read before acServer re-anchors it
    p = ports(s.base_port)
    try:
        inst = await supervisor.start(
            server_id,
            write_instance(s),
            acsp_remote_port=p["plugin"],
            acsp_local_port=p["plugin_local"],
            http_port=p["http_internal"],
            cpu_percent=s.cpu_limit,
            mem_mb=s.mem_limit_mb,
        )
    except RuntimeError as e:
        raise ServerError(409, str(e)) from e
    inst.set_weather_plan(s.weather_plan, s.config)
    timeline.start_resume(server_id, planned, planned_at)   # if the session clock ran while the server was off, move it to where the clock is
    return {"running": inst.running, "pid": inst.pid}


async def stop_instance(server_id: int, inst: supervisor.Instance, reason: str) -> None:
    """Automatic stops (end of an event): same exclusion as the others, and only if `inst` is still the registered one (an apply may have replaced it)."""
    async with server_lock(server_id):
        await supervisor.stop(server_id, reason=reason, only=inst)


def write_instance(s: Server) -> Path:
    d = Path(settings.data_dir) / "instances" / str(s.id)
    (d / "cfg").mkdir(parents=True, exist_ok=True)
    (d / "results").mkdir(exist_ok=True)
    # acServer reads content/ and system/ relative to its cwd: share the install's copies.
    bin_dir = settings.acserver_dir()
    for name in ("content", "system"):
        link = d / name
        if bin_dir and not link.exists() and (bin_dir / name).is_dir():
            link.symlink_to(bin_dir / name)
    (d / "cfg" / "server_cfg.ini").write_text(ini_generator.server_cfg(s.config, ports(s.base_port), bool(s.welcome or s.csp_extra.strip())))
    (d / "cfg" / "entry_list.ini").write_text(ini_generator.entry_list(s.entry_list))
    welcome = d / "cfg" / "welcome.txt"
    if s.welcome or s.csp_extra.strip():
        welcome.write_text(csp.welcome_with_extra(s.welcome, s.csp_extra))
    else:
        welcome.unlink(missing_ok=True)
    return d


async def apply(sess: Session, s: Server, body: SessionIn, held: bool = False) -> ApplyResult:
    """`held`: the caller already holds server_lock(s.id) (schedule does, to decide on `loaded` and apply as one step); the lock is not reentrant."""
    server_id = s.id
    if not (body.practice_min or body.qualify_min or body.race_laps or body.race_min):
        raise ServerError(400, "enable at least one session (practice, qualify or race)")
    cars = list(dict.fromkeys(e.model for e in body.entries)) if body.entries else body.cars
    skins = check_content(body, cars)
    guids = [g for e in body.entries for g in e.guid.split(";") if g]
    if len(guids) != len(set(guids)):
        raise ServerError(400, "a Steam ID appears in more than one entry")
    if body.locked and not guids:
        raise ServerError(400, "a locked entry list needs at least one entry with a Steam ID (nobody could join)")
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
    result = ApplyResult()
    if body.restart:
        try:
            async with (nullcontext() if held else server_lock(server_id)):   # stop + start as ONE step: a wake / API start / event end cannot slip in between
                await supervisor.stop(server_id)
                await start_unlocked(server_id, sess)
            result.restarted = True
        except ServerError as e:
            result.start_error = e   # saved, but not running: reported, not hidden behind a bare refusal
    return result


async def start(sess: Session, server_id: int, held: bool = False) -> dict:
    """Start a server (its INI files are written under the lock too: two starts never rewrite them while acServer reads them)."""
    async with (nullcontext() if held else server_lock(server_id)):
        return await start_unlocked(server_id, sess)


async def stop(server_id: int) -> None:
    async with server_lock(server_id):
        await supervisor.stop(server_id)


async def adopt_running(sess: Session) -> int:
    """At boot: take back every acServer a previous manager process left running (see supervisor.adopt)."""
    n = 0
    for s in sess.exec(select(Server)).all():
        p = ports(s.base_port)
        inst = await supervisor.adopt(
            s.id, Path(settings.data_dir) / "instances" / str(s.id),
            acsp_remote_port=p["plugin"], acsp_local_port=p["plugin_local"], car_slots=len(s.entry_list), http_port=p["http_internal"],
        )
        if inst:
            inst.set_weather_plan(s.weather_plan, s.config)
        n += inst is not None
    return n

