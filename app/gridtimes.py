"""The starting grid of a race, set by the director, delivered through the times of the session before it.

acServer has no command for the grid: when a session ends it sorts the cars by `BestLaps` (practice / qualify: smaller first) and sends THAT order as the grid of the next session. So in the last seconds of a
practice or qualify session, while a `GridOrder` exists for the server, the pole sitter's best lap is set to 1 ms, the second's to 2 ms and so on, straight in acServer's memory (`/proc/<pid>/mem`: only the
VALUES of an array that already exists; the manager is the parent of the acServer, or the same user with ptrace_scope 0). Nothing real is lost: a lap is only stored when it beats the stored time (OnLapCompleted),
which no real lap does, and the site's qualifying result is computed from the `Laps` of the result file (app/results.py), not from `Result.BestLap`.

Who is where: `GridOrder` (Steam IDs, pole first) is the truth and lives in the database, so a restart loses nothing. Every second of the window the plan is rebuilt from the cars connected at that moment, so a driver
who leaves and comes back keeps his place (the order is by Steam ID, whatever slot he gets) and one the order does not name goes last. The order is deleted when a race begins: that race used it.

The addresses are those of ONE build of acServer (`ACSERVER_SHA256`); on any other the manager refuses to write. Needs a Race to come after a non-race session: a race sorts by laps and total time, not by `BestLaps`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from app import results
from app.config import settings
from app.db import engine
from app.models import GridOrder, Server

log = logging.getLogger("acm.gridtimes")

ACSERVER_SHA256 = "09abb6ae684fe5a0ed268213622c21ad74bb7df2e6de596349c75a9afb38bb07"   # the linux/386 acServer 1.15 the addresses below belong to
CURRENT_RESULT = 0x83E3460   # main.CurrentSessionResult: its first field is BestLaps []uint32 (pointer, length, capacity: 12 bytes, Go on 32 bits)
CARS = 0x83E2E68             # main.Cars []*main.Car: one per slot of the entry list
CAR_SESSION_ID = 60          # offset of main.Car.SessionID (uint8) after Model, Driver and Skin: DWARF, and checked against a live process
CURRENT_INDEX = 0x83F45E0    # main.CurrentSessionIndex (int32): 0, 1, 2, 0 ... with every NEXT_SESSION (checked on a live process)
LEAD_S = 5.0     # the writes start this long before the session's time is up (acServer sorts the moment it is) ...
LATE_S = 600.0   # ... and go on until the next session begins (an extended or overtime qualify), at most this long after
TICK_S = 1.0
DRAIN_S = 0.3    # between the two questions about the next session: an answer still in flight is old
MAX_SLOTS = 256   # more than this in a slice header means the layout is not what we think


class GridError(Exception):
    """Something in acServer's memory is not as expected: nothing was written (or the read-back differs)."""


class Mem:
    """The memory of the acServer process. Tests pass anything with the same `read` / `write`."""

    def __init__(self, pid: int) -> None:
        self.fd = os.open(f"/proc/{pid}/mem", os.O_RDWR)

    def read(self, addr: int, n: int) -> bytes:
        return os.pread(self.fd, n, addr)

    def write(self, addr: int, data: bytes) -> None:
        os.pwrite(self.fd, data, addr)

    def close(self) -> None:
        os.close(self.fd)


def pace_guid(server_id: int) -> str:
    """SteamID of the pace car's driver, "" when there is none: the director's own choice (RaceControl.pace_guid), or the one the preset names (`pace_steam_id`) while the manager has not taken it yet. Memory only: cheap to ask often."""
    from app import supervisor   # late, like the rest of this module
    from app.live import racecontrol
    with racecontrol._lock:
        rc = racecontrol._states.get(server_id)
        if rc and rc.pace_guid:
            return str(rc.pace_guid)
    inst = supervisor.get(server_id)
    return str(getattr(inst, "pace_steam_id", None) or "")


def plan(order: list[str], cars: dict[int, str], pace: str = "") -> dict[int, int]:
    """{slot (acServer's SessionID): best lap in ms}: the pace car first (place 1, in or out of the order), then the connected cars in the director's order, the ones it does not name after them in slot order. 1-based: 0 means «no lap» to acServer."""
    place = {guid: i for i, guid in enumerate(order) if guid != pace}
    ranked = sorted(cars, key=lambda sid: (0 if pace and cars[sid] == pace else 1, place.get(cars[sid], len(order)), sid))
    return {sid: i + 1 for i, sid in enumerate(ranked)}


def apply(mem, ranks: dict[int, int], session_index: int | None = None) -> None:
    """Write `ranks` into BestLaps of the session being played. Checks first that it IS the session `session_index` (a write that lands after the switch would put 1 ms laps in the next session), that the arrays
    are long enough and that main.Cars[slot] really is that slot; reads the values back after. ponytail: the check and the write are microseconds apart, not atomic."""
    if not ranks:
        return
    if session_index is not None and struct.unpack("<i", mem.read(CURRENT_INDEX, 4))[0] != session_index:
        raise GridError("the session changed")
    base, n, _ = struct.unpack("<III", mem.read(CURRENT_RESULT, 12))
    cars_base, cn, _ = struct.unpack("<III", mem.read(CARS, 12))
    top = max(ranks)
    if not (0 < n <= MAX_SLOTS and 0 < cn <= MAX_SLOTS) or top >= n or top >= cn:
        raise GridError(f"BestLaps has {n} entries and Cars {cn}: slot {top} does not fit")
    cars = struct.unpack(f"<{cn}I", mem.read(cars_base, 4 * cn))
    for sid in ranks:
        if not cars[sid] or mem.read(cars[sid] + CAR_SESSION_ID, 1)[0] != sid:
            raise GridError(f"main.Cars[{sid}] is not slot {sid}: acServer indexes its results differently here")
    for sid, ms in ranks.items():
        mem.write(base + 4 * sid, struct.pack("<I", ms))
    back = struct.unpack(f"<{n}I", mem.read(base, 4 * n))
    if any(back[sid] != ms for sid, ms in ranks.items()):
        raise GridError("the times read back are not the ones written (the array moved?)")


_known: dict[tuple, bool] = {}   # (pid, start time) -> its binary is the build the addresses belong to


def known_binary(pid: int, started_at: float | None) -> bool:
    key = (pid, started_at)
    if key not in _known:
        h = hashlib.sha256()
        with open(f"/proc/{pid}/exe", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _known[key] = h.hexdigest() == ACSERVER_SHA256
    return _known[key]


# --- the order (database) ---------------------------------------------------------------------------------------------------------------------------------------

def get_order(server_id: int) -> list[str]:
    with Session(engine) as s:
        row = s.get(GridOrder, server_id)
        return list(row.guids) if row else []


def set_order(server_id: int, guids: list[str], source: str) -> None:
    guids = list(dict.fromkeys(str(g) for g in guids if str(g).strip()))   # no repeats, order kept
    with Session(engine) as s:
        s.merge(GridOrder(server_id=server_id, guids=guids, source=source))
        s.commit()


def clear_order(server_id: int, before: float | None = None) -> bool:
    """Delete the order. With `before` (unix time): only if it was set before then (a race that began at that moment spends the orders that existed, not one the director saves a moment later)."""
    with Session(engine) as s:
        row = s.get(GridOrder, server_id)
        if row is None:
            return False
        if before is not None and row.created_at.replace(tzinfo=None) > datetime.fromtimestamp(before, UTC).replace(tzinfo=None):
            return False
        s.delete(row)
        s.commit()
        return True


# --- the order from the race before (the intermediate session of a sequence) -----------------------------------------------------------------------------------

def shape(grid: list[str], n: int) -> list[str]:
    """acServer's own meaning of REVERSED_GRID_RACE_POSITIONS: 0 as it is, N = the first N inverted, -1 = all."""
    if n == -1:
        return grid[::-1]
    return grid[:n][::-1] + grid[n:] if n > 0 else list(grid)


def last_result_file(server_id: int, since: float, kind: str = "RACE") -> Path | None:
    """The newest result of this kind (RACE, QUALIFY) of this server written at or after `since` (unix time): the session that just ended, whose file acServer writes when it ends."""
    folder = Path(settings.data_dir) / "instances" / str(server_id) / "results"
    files = [p for p in folder.glob(f"*_{kind}.json") if p.stat().st_mtime >= since] if folder.exists() else []
    return max(files, key=lambda p: (results.result_start(p.name) or datetime.min.astimezone(), p.name), default=None)


def order_now(server_id: int, since: float, source: str = "race", step_idx: int | None = None) -> list[str]:
    """The order to deliver now: the director's own (GridOrder) if there is one, else the result of the session before it:
    source "race": the race, by the server's `grid_from_race` (0 as finished, N = first N inverted, -1 = all inverted);
    source "qualify": the qualifying as the site classifies it (clean laps), when the server's `grid_from_qualify` is set (a practice after the qualifying to arrange the grid).
    Both with the stewards' grid penalties applied; [] when none of that. Reads the database and a result file: call it off the event loop."""
    if order := get_order(server_id):
        return order
    if source == "singlerun":   # a qualifying by single runs: app/singlerunlive.py ranks the runs
        from app import singlerunlive
        return singlerunlive.grid_now(server_id, since, step_idx)
    if source == "knockout":   # Q1, Q2, Q3 of a knockout qualifying: app/knockoutlive.py has the grid of the three stages
        from app import knockoutlive
        return knockoutlive.grid_now(server_id)
    with Session(engine) as s:
        srv = s.get(Server, server_id)
        session = (srv.session if srv else None) or {}
        if source == "qualify":
            policy, kind = (0 if session.get("grid_from_qualify") else None), "QUALIFY"
        else:
            policy, kind = session.get("grid_from_race"), "RACE"
        if policy is None:
            return []
        path = last_result_file(server_id, since, kind)
        if path is None:
            return []
        parsed = results.parse_result_file(path)
        grid = results.apply_penalties(parsed, results.penalties_for(s, server_id, path.name), results.non_racing_for(s, server_id, path.name))["grid"]
    return shape(grid, int(policy))


# --- what the panel shows: the order the next race will have, and why ----------------------------------------------------------------------------------------------

def _names(guids: list[str]) -> dict[str, str]:
    """The last name each driver was seen with (joins); a driver never seen has none."""
    from sqlmodel import select
    from app.models import Activity
    if not guids:
        return {}
    with Session(engine) as s:
        rows = s.exec(select(Activity.guid, Activity.name).where(Activity.kind == "join", Activity.guid.in_(guids)).order_by(Activity.ts)).all()
    return {g: n for g, n in rows if n}


def _source_now(server_id: int, client) -> tuple[str | None, str]:
    """(source, why not): which order the guard would deliver in the session being played, by the SEQUENCE saved for the server (the guard asks acServer for the types, which is the same unless somebody redefines sessions by hand):
    only the non-race session right before a race delivers, from the session before it."""
    session = getattr(client, "session", None) or {}
    idx = session.get("current_session_index", session.get("session_index"))
    with Session(engine) as s:
        srv = s.get(Server, server_id)
        sess = (srv.session if srv else None) or {}
    steps = sess.get("sequence") or []
    if not client or idx is None or not steps:
        return None, "El orden de la carrera lo decide acServer con la clasificación (sin secuencia de sesiones con práctica de ajuste)."
    idx = int(idx)
    cur, nxt = steps[idx % len(steps)], steps[(idx + 1) % len(steps)]
    if cur["kind"] == "race" or nxt["kind"] != "race" or idx == 0:
        return None, "El orden se calcula en la práctica de ajuste, antes de la carrera."
    before = steps[idx - 1]
    if before["kind"] == "race":
        return ("race", "") if sess.get("grid_from_race") is not None else (None, "La carrera anterior no pasa su orden: falta «parrilla de la carrera anterior».")
    if before["kind"] != "qualify":   # (the guard takes a qualifying order only from a qualifying: a practice before the practice that arranges the grid says nothing)
        return None, "La sesión anterior no es una clasificación: no hay orden que entregar."
    if before.get("stage") == 3:
        return "knockout", ""
    if before.get("run_interval_s"):
        return "singlerun", ""
    return ("qualify", "") if sess.get("grid_from_qualify") else (None, "La política de parrilla es «La decido yo»: sin orden fijado, acServer usa la clasificación.")


def preview(server_id: int, client) -> dict:
    """The order of the next race as the panel shows it: [{guid, name, online, reason, tone}] and where it comes from. `mode`: manual (the director's own, GridOrder), auto (what the guard will write from the session before),
    none (nothing to say yet: `note` says why). Reads the database and result files: call it off the event loop."""
    slots = {c.get("driver_guid"): cid for cid, c in list((getattr(client, "cars", None) or {}).items()) if c.get("driver_guid")}   # guid -> slot (plan() puts the unnamed ones after the named in slot order)
    cars = {c.get("driver_guid"): c for c in list((getattr(client, "cars", None) or {}).values()) if c.get("driver_guid")}
    manual = get_order(server_id)
    source, note, order, reasons = None, "", manual, {}
    if manual:
        mode, source = "manual", "manual"
    else:
        source, note = _source_now(server_id, client)
        mode = "auto" if source else "none"
        if source:
            wait = int((getattr(client, "session", None) or {}).get("wait_time") or 0)
            since = client.session_t0 - 120 - wait
            cur = (getattr(client, "session", None) or {}).get("current_session_index", (getattr(client, "session", None) or {}).get("session_index"))
            order = order_now(server_id, since, source, int(cur) - 1 if cur is not None else None) if source == "singlerun" else order_now(server_id, since, source)
            if source in ("qualify", "race") and order:   # why each place: where he finished, and what the stewards took
                kind = "RACE" if source == "race" else "QUALIFY"
                path = last_result_file(server_id, since, kind)
                if path is not None:
                    with Session(engine) as s:
                        parsed = results.parse_result_file(path)
                        pens = results.penalties_for(s, server_id, path.name)
                    base = [e["driver_guid"] for e in parsed["classification"] if e["driver_guid"]]
                    lost = {}
                    for p in pens:
                        if p.kind == "grid":
                            lost[p.driver_guid] = lost.get(p.driver_guid, 0) + p.value
                    for i, g in enumerate(order):
                        b = base.index(g) + 1 if g in base else None
                        txt = f"P{b} en la {'carrera' if source == 'race' else 'clasificación'}" if b else "Sin tiempo"
                        if lost.get(g):
                            reasons[g] = (f"{txt} · −{lost[g]} por sanción de parrilla", "pen")
                        elif b and b - (i + 1) > 0:
                            reasons[g] = (f"{txt} · sube {b - (i + 1)} puesto" + ("s" if b - (i + 1) > 1 else ""), "ok")
                        else:
                            reasons[g] = (txt, "ok")
            elif order:
                reasons = {g: ("Por la eliminatoria" if source == "knockout" else "Por su tanda", "ok") for g in order}
    pace = pace_guid(server_id)
    if order and pace in cars:   # the pace car is always the first (plan()), whatever the order says
        order = [pace] + [g for g in order if g != pace]
    names = _names(list(dict.fromkeys([*order, *cars])))
    rows = [{"guid": g, "name": (cars.get(g) or {}).get("driver_name") or names.get(g) or g, "online": g in cars,
             "reason": "Pace car: sale primero" if g == pace else "Orden fijado a mano" if manual else reasons.get(g, ("", "ok"))[0], "tone": "hand" if manual and g != pace else reasons.get(g, ("", "ok"))[1]} for g in order]
    late = sorted((g for g in cars if g not in order), key=lambda g: slots[g]) if (order or mode == "manual") else []   # connected and not named: the guard puts them after the named ones (plan())
    rows += [{"guid": g, "name": cars[g].get("driver_name") or names.get(g) or g, "online": True, "reason": "Nuevo: entró después, va al final", "tone": "new"} for g in late]
    return {"mode": mode, "source": source, "note": note, "order": rows}


# --- the guard (one task per non-race session that comes right before a race) ----------------------------------------------------------------------------------

def on_session(client, event: dict, is_new: bool) -> None:
    """ACSPClient calls it for `new_session` and for the description of the CURRENT session (`session_info`, which is what arrives after the manager restarts). A race spends the order; a timed practice / qualify gets
    a guard that wakes LEAD_S before its end, again if its length changes (`time_min`)."""
    idx = event.get("session_index")
    if not is_new and idx != event.get("current_session_index"):
        return   # the description of another session
    key = (idx, event.get("time_min"), event.get("session_type"))
    if not is_new and client.grid_key == key:
        return   # the same session, nothing changed
    client.grid_key = key
    client.grid_last = None
    if client.grid_task:
        client.grid_task.cancel()
    client.grid_task = None
    kind = event.get("session_type")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:   # no loop (a test calling _apply directly)
        return
    if kind == 3:
        loop.create_task(_spend(client.server_id, time.time() - (event.get("elapsed_ms") or 0) / 1000))
    elif kind in (1, 2) and event.get("time_min"):
        left = event["time_min"] * 60 - (event.get("elapsed_ms") or 0) / 1000
        client.grid_task = loop.create_task(_guard(client, left, idx, event.get("session_count") or 0))


async def _spend(server_id: int, started_at: float) -> None:
    if await asyncio.to_thread(clear_order, server_id, started_at):
        log.info("server %s: the race began, its grid order is spent", server_id)


async def _guard(client, left: float, idx: int, count: int) -> None:
    warned = None
    try:
        await asyncio.sleep(max(0.0, left - LEAD_S))
        nxt = (idx + 1) % count if count else idx + 1
        if not await _race_at(client, nxt):
            log.debug("server %s: the session after %s is not a race: no grid times", client.server_id, idx)
            return
        # an order from the session before only makes sense right after it IN THIS CYCLE: the opening session of a cycle that ends in a race is not «between two races» (its race is graded by its own times)
        source = {3: "race", 2: "qualify"}.get(await _type_at(client, idx - 1)) if idx > 0 and count else None
        if source == "qualify":
            from app import sessionpatch   # late
            before, _ = await asyncio.to_thread(sessionpatch.step_and_original, client.server_id, idx - 1)
            if (before or {}).get("stage") == 3:   # the practice that follows Q3: the grid is the knockout's
                source = "knockout"
            elif (before or {}).get("run_interval_s"):
                source = "singlerun"
        end = time.monotonic() + LEAD_S + LATE_S
        while True:
            err = await _tick(client, idx, source)
            if err and err != warned:
                log.warning("server %s: grid times not written: %s", client.server_id, err)
            warned = err
            if time.monotonic() >= end:
                return
            await asyncio.sleep(TICK_S)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("server %s: the grid guard failed", client.server_id)


async def _type_at(client, nxt: int) -> int | None:
    """The type of session `nxt` (1 practice, 2 qualify, 3 race), as acServer says NOW (the director may have redefined it). Asked twice and the second answer trusted: one still in flight from an earlier question would be old."""
    from app.live import acsp   # late: acsp imports this module
    for _ in range(2):
        client.session_types.pop(nxt, None)
        client.send(acsp.encode_get_session_info(nxt))
        for _ in range(10):
            if nxt in client.session_types:
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(DRAIN_S)
    return client.session_types.get(nxt)


async def _race_at(client, nxt: int) -> bool:
    return await _type_at(client, nxt) == 3


async def _tick(client, idx: int, source: str | None = None) -> str | None:
    """One write. Returns why nothing was written (or None); the database and the memory are read off the event loop.
    ponytail: the cars are read here and written a moment later in the thread: a slot that changes hands in between gets the old driver's place for one second (the next tick corrects it)."""
    from app import supervisor   # late, like acsp.py does: supervisor owns the client
    if source:
        wait = int((getattr(client, "session", None) or {}).get("wait_time") or 0)   # the file of the session before is written when this one is SET UP, a start that the wait puts that much after it
        extra = (idx - 1,) if source == "singlerun" else ()   # (acServer's file does not say which session it is: the single run's step is the one right before this)
        order = await asyncio.to_thread(order_now, client.server_id, client.session_t0 - 120 - wait, source, *extra)
    else:
        order = await asyncio.to_thread(get_order, client.server_id)
    inst = supervisor.get(client.server_id)
    if not inst or not inst.pid:
        return None
    cars = {cid: str(c.get("driver_guid") or "") for cid, c in list(client.cars.items())}
    pace = pace_guid(client.server_id)
    if order:
        ranks = plan(order, cars, pace)
    else:   # nothing to deliver: acServer's own order stands, but the pace car is always the first (1 ms is below any lap; the others keep their times)
        ranks = {cid: 1 for cid, g in cars.items() if pace and g == pace}
    if not ranks:
        return None
    err = await asyncio.to_thread(_write, inst.pid, inst.started_at, ranks, client.server_id, idx)
    if err is None and client.grid_last != ranks:   # said once per change, not every second
        client.grid_last = ranks
        log.info("server %s: grid times written for session %s: %s", client.server_id, idx, ranks)
    return err


def _write(pid: int, started_at: float | None, ranks: dict[int, int], server_id: int, idx: int) -> str | None:
    try:
        if not known_binary(pid, started_at):
            return "this acServer is not the build the memory addresses belong to"
        mem = Mem(pid)
        try:
            apply(mem, ranks, idx)
        finally:
            mem.close()
    except (GridError, OSError) as e:
        return str(e)
    return None
