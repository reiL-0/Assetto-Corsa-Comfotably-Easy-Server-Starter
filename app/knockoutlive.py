"""The knockout qualifying (Q1, Q2, Q3: app/knockout.py) while it is being played.

What the manager keeps, per server, in the database (`KnockoutCycle`, one row; a restart in the middle of Q2 loses nothing, and everything is by Steam ID so a driver who leaves and comes back is still who he was):
- who ran Q1 (`entrants`: those connected when Q1 began and those who joined before it ended), and who came in AFTER it (`late`, in the order they came: they count as if they had set the slowest time, go last
  on the grid and never run a later stage);
- the result file of every stage, written down when the stage ends (ACSP `end_session`; the file's own session name says which stage it is).
Everything else is derived from those files (the best CLEAN lap of each driver, as the site classifies): who is cut after a stage, who must be held in the pits for the next one, the final grid.

`hold_view` is what `race-state` gives the hub (`pit_hold`): the cars that must not leave the pits in this stage (the hub keeps their brakes on, like the orderly start's hold; with no fresh state for 12 s it lets go). The Steam IDs
are turned into car ids NOW, so a driver who comes back in another slot stays held. If a stage's file is missing the answer is «nobody is held»: nothing is ever blocked without evidence.
`grid_now` is the grid of the finished knockout with the stewards' grid penalties of the three stages applied; app/gridtimes.py delivers it in the practice before the race.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from sqlmodel import Session

from app import knockout, results
from app.config import settings
from app.db import engine
from app.models import KnockoutCycle

log = logging.getLogger("acm.knockoutlive")


# --- the state (database) and its transitions ---------------------------------------------------------------------------------------------------------------------

def load(server_id: int) -> dict | None:
    with Session(engine) as s:
        row = s.get(KnockoutCycle, server_id)
        return dict(row.data) if row else None


def save(server_id: int, data: dict) -> None:
    with Session(engine) as s:
        s.merge(KnockoutCycle(server_id=server_id, data=data))
        s.commit()


def clear(server_id: int) -> bool:
    with Session(engine) as s:
        row = s.get(KnockoutCycle, server_id)
        if row:
            s.delete(row)
            s.commit()
        return row is not None


def new_cycle(connected: list[str], now: float, names: dict[str, str] | None = None) -> dict:
    """Q1 begins: the drivers connected now are the entrants. `names`: guid -> the name the game gave (a result row whose Steam ID was lost is matched by it)."""
    entrants = list(dict.fromkeys(g for g in connected if g))
    return {"id": now, "entrants": entrants, "late": [], "stages": {}, "names": {g: n for g, n in (names or {}).items() if g in entrants and n}}


def add_connection(data: dict, guid: str, name: str = "") -> bool:
    """A driver connected: an entrant while Q1 has not ended, a late one (in order of arrival) after. Known drivers (a reconnection) change nothing but their name. True when `data` changed."""
    if not guid:
        return False
    if name and data.setdefault("names", {}).get(guid) != name and (guid in data["entrants"] or guid in data["late"]):
        data["names"][guid] = name
    if guid in data["entrants"] or guid in data["late"]:
        return False
    data["late" if "1" in data["stages"] else "entrants"].append(guid)
    if name:
        data.setdefault("names", {})[guid] = name
    return True


# --- from the result files ------------------------------------------------------------------------------------------------------------------------------------------

def _folder(server_id: int) -> Path:
    return Path(settings.data_dir) / "instances" / str(server_id) / "results"


_times_cache: dict[tuple, list] = {}   # (path, mtime_ns, size) -> [(guid, driver name, best clean lap ms or None)]; a result file never changes once written


def stage_times(server_id: int, filename: str, names: dict[str, str] | None = None) -> dict[str, int | None]:
    """{guid: best clean lap ms or None} of a stage's file. A row whose Steam ID is not a Steam ID (acServer writes a placeholder when a driver reconnects after a kick) is matched to its driver by NAME."""
    path = _folder(server_id) / Path(filename).name
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _times_cache:
        if len(_times_cache) > 64:
            _times_cache.clear()
        parsed = results.parse_result_file(path)
        _times_cache[key] = [(e["driver_guid"], e.get("driver_name") or "", e["best_lap_ms"]) for e in parsed["classification"] if e["driver_guid"]]
    taken = [n for n in (names or {}).values() if n]
    by_name = {n: g for g, n in (names or {}).items() if n and taken.count(n) == 1}   # (two drivers with one name: nobody can say whose a lost-ID row is, so it is nobody's)
    out: dict[str, int | None] = {}
    for guid, name, best in _times_cache[key]:
        who = guid if str(guid).isdigit() or name not in by_name else by_name[name]
        old = out.get(who)
        out[who] = best if old is None else (old if best is None else min(old, best))   # one driver, two rows: his best
    return out


def played(server_id: int, data: dict, upto: int) -> list[dict] | None:
    """The times of stages 1..upto, or None when one of them has no file (nothing can be said then)."""
    out = []
    for k in range(1, upto + 1):
        fn = data["stages"].get(str(k))
        if not fn:
            return None
        try:
            out.append(stage_times(server_id, fn, data.get("names")))
        except (OSError, ValueError, KeyError) as e:
            log.warning("server %s: the file of stage %s (%s) cannot be read: %s", server_id, k, fn, e)
            return None
    return out


def held_guids(server_id: int, stage: int) -> list[str] | None:
    """Who must stay in the pits in `stage` (2 or 3): those cut in the stages before and the late ones. None: not known (no cycle, or a stage file missing). Reads the database and files: not on the event loop."""
    data = load(server_id)
    if not data or stage < 2:
        return None
    times = played(server_id, data, stage - 1)
    if times is None:
        return None
    out = knockout.run(data["entrants"], times)
    return list(dict.fromkeys(out["held"] + data["late"]))


def hold_view(server_id: int, session: dict | None, cars: dict) -> dict | None:
    """`pit_hold` of race-state: {"stage": n, "cars": [car ids]} in stages 2 and 3 of the knockout, None otherwise. `cars`: the ACSP car list (car id -> {driver_guid, ...})."""
    from app import sessionpatch   # late: it reads the sequence of the server
    if not session:
        return None
    idx = session.get("current_session_index", session.get("session_index"))
    if idx is None:
        return None
    step, _ = sessionpatch.step_and_original(server_id, int(idx))
    stage = (step or {}).get("stage")
    if stage not in (2, 3) or (session.get("name") and session.get("name") != step.get("name")):   # (the session being played is the step: a sequence changed under a cycle would not be)
        return None
    held = held_guids(server_id, stage)
    if held is None:
        return None
    held = set(held)
    return {"stage": stage, "cars": sorted(cid for cid, c in cars.items() if c.get("driver_guid") in held)}


def grid_now(server_id: int) -> list[str]:
    """The grid of the finished knockout (Q3 order, then the Q2 casualties, then the Q1 ones, then the late ones) with the stewards' grid penalties of the three stages applied; [] until Q3 has a file."""
    data = load(server_id)
    if not data:
        return []
    times = played(server_id, data, 3)
    if times is None:
        return []
    grid = knockout.run(data["entrants"], times, late=data["late"])["grid"]
    files = [Path(data["stages"][str(k)]).name for k in (1, 2, 3)]
    parsed = {"type": "Qualify", "laps": [], "classification": [
        {"position": i + 1, "driver_guid": g, "driver_name": "", "car_model": "", "best_lap_ms": None, "total_time_ms": None} for i, g in enumerate(grid)]}
    with Session(engine) as s:
        pens = [p for fn in files for p in results.penalties_for(s, server_id, fn)]
        non_racing = results.non_racing_for(s, server_id, files[-1])
    return results.apply_penalties(parsed, pens, non_racing)["grid"]


# --- ACSP events (ACSPClient calls these) ----------------------------------------------------------------------------------------------------------------------------

def on_session(client, event: dict, is_new: bool) -> None:
    """`new_session` (and the description of the CURRENT session after a manager restart): Q1 begins a cycle; a race spends it."""
    idx = event.get("session_index")
    if not is_new and idx != event.get("current_session_index"):
        return
    _spawn(client, _session(client, idx, is_new))


def on_connection(client, guid: str, name: str = "") -> None:
    _spawn(client, _connection(client, guid, name))


def on_end(client, filename: str) -> None:
    _spawn(client, _end(client, filename))


def _spawn(client, coro) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:   # no loop (a test calling _apply directly)
        coro.close()


async def _session(client, idx: int, is_new: bool) -> None:
    from app import sessionpatch
    async with client.knockout_lock:
        try:
            step, _ = await asyncio.to_thread(sessionpatch.step_and_original, client.server_id, idx)
            if step is None:
                return
            if is_new and step["kind"] == "race":
                if await asyncio.to_thread(clear, client.server_id):
                    log.info("server %s: the race began, the knockout is over", client.server_id)
            elif step.get("stage") == 1 and (is_new or await asyncio.to_thread(load, client.server_id) is None):
                cars = list(client.cars.values())
                guids = [str(c.get("driver_guid") or "") for c in cars]
                names = {str(c.get("driver_guid") or ""): str(c.get("driver_name") or "") for c in cars}
                await asyncio.to_thread(save, client.server_id, new_cycle(guids, time.time(), names))
                log.info("server %s: Q1 begins with %s drivers", client.server_id, len(guids))
        except Exception:
            log.exception("server %s: the knockout could not follow the session", client.server_id)


async def _connection(client, guid: str, name: str = "") -> None:
    async with client.knockout_lock:
        try:
            def go() -> str | None:
                data = load(client.server_id)
                if data is None:
                    return None
                changed = add_connection(data, guid, name)
                before = load(client.server_id)
                if changed or data.get("names") != (before or {}).get("names"):
                    save(client.server_id, data)
                return "late" if changed and guid in data["late"] else None
            kind = await asyncio.to_thread(go)
            if kind == "late":
                log.info("server %s: %s came in after Q1: the slowest, held in the pits", client.server_id, guid)
        except Exception:
            log.exception("server %s: the knockout could not follow a connection", client.server_id)


def _stage_of(server_id: int, filename: str) -> int | None:
    """Which stage a result file is, from the session name written in it (the one the sequence gave the step). None while the file is not readable."""
    try:
        doc = json.loads((_folder(server_id) / Path(filename).name).read_text(encoding="utf-8-sig"))
        name = doc.get("Name") if str(doc.get("Type", "")).upper() == "QUALIFY" else None   # (a practice that happens to carry a stage's name is not that stage)
    except (OSError, ValueError, AttributeError):
        return None
    with Session(engine) as s:
        from app.models import Server
        srv = s.get(Server, server_id)
        seq = ((srv.session if srv else None) or {}).get("sequence") or []
    return next((st["stage"] for st in seq if st.get("stage") and st["name"] == name), None)


async def _end(client, filename: str) -> None:
    try:
        stage = None
        for _ in range(5):   # the file is written when the session ends: a moment may pass before it can be read (the lock is NOT held meanwhile: a connection in those seconds is processed as it comes)
            stage = await asyncio.to_thread(_stage_of, client.server_id, filename)
            if stage:
                break
            await asyncio.sleep(0.5)
        if not stage:
            log.warning("server %s: %s is not (or not yet) the result of a knockout stage", client.server_id, filename)
            return

        def go() -> None:
            data = load(client.server_id)
            if data is not None:
                data["stages"][str(stage)] = Path(filename).name
                save(client.server_id, data)
        async with client.knockout_lock:
            await asyncio.to_thread(go)
        log.info("server %s: stage %s ended (%s)", client.server_id, stage, Path(filename).name)
    except Exception:
        log.exception("server %s: the knockout could not follow the end of a session", client.server_id)
