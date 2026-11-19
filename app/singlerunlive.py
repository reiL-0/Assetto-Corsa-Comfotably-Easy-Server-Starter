"""The qualifying by single-car runs (app/singlerun.py: the logic) while it is played.

Nothing is stored: everything is derived on each call from the clock of the session (`session_t0`), the Activity rows (laps) and, once it is over, the result file. So a manager restart in the middle loses nothing.
- RELEASE TIMES: given once per driver and remembered in memory (`client.run_release`): by slot the first time the session is seen, later arrivals after the last one. Nobody's turn moves when another leaves.
- `hold_view` is the `pit_hold` of race-state (the hub keeps the brakes of those cars on, like the knockout's): the cars not released yet and the ones whose run is over. Without a clock nothing is held.
- `grid_now` is the grid of the finished qualifying: ranked by the total time of the timed laps (= average speed) with the stewards' grid penalties applied.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlmodel import Session, select

from app import results, singlerun
from app.db import engine
from app.models import Activity

log = logging.getLogger("acm.singlerunlive")

RUN_STAGE = 9   # the `stage` of this qualifying in `pit_hold`: the hub says «wait for your turn» for it (the knockout's 2 and 3 say «eliminated»)


def _release(client, connected: dict[str, int], t0: float, interval: float, now: float) -> dict[str, float]:
    """The unix time each driver is let out, given ONCE and remembered (`client.run_release`, one per session): the first sight of the session gives the slot order (the first at `t0`, one every `interval`); whoever
    is seen later goes after the last one assigned, and never in the past, so a late driver does not go out at once nor together with another. Nobody's turn moves when someone else leaves. ponytail: lost on a manager
    restart (rebuilt by slot, so cars that already ran would run again... they are held by their laps as soon as these are read)."""
    rel = client.run_release
    first = not rel
    for g in sorted((g for g in connected if g not in rel), key=lambda g: connected[g]):
        rel[g] = t0 + len(rel) * interval if first else max(now, max(rel.values()) + interval)
    return rel


def _laps(server_id: int, since: float, order: list[str], t_release: dict[str, float]) -> dict[str, singlerun.Run]:
    """The laps each car completed since ITS release: from the Activity rows of this session."""
    runs = {g: singlerun.Run(g) for g in order}
    with Session(engine) as s:
        rows = s.exec(select(Activity.guid, Activity.ts, Activity.value, Activity.cuts).where(Activity.server_id == server_id, Activity.kind == "lap", Activity.ts >= since).order_by(Activity.ts)).all()
    for guid, ts, ms, cuts in rows:
        if guid in runs and ts >= t_release[guid] and ms:
            runs[guid].laps_ms.append(int(ms))
            runs[guid].cuts.append(cuts or 0)
    return runs


def hold_view(server_id: int, client, now: float | None = None) -> dict | None:
    """`pit_hold` of race-state ({"stage": RUN_STAGE, "cars": [car ids]}) while a single-run qualifying is played, None otherwise. `client`: the live ACSP client (session, session_t0, cars). Reads the database: not on the event loop."""
    from app import sessionpatch   # late: it reads the sequence of the server
    session = client.session
    if not session or not client.session_t0:
        return None
    idx = session.get("current_session_index", session.get("session_index"))
    if idx is None:
        return None
    step, _ = sessionpatch.step_and_original(server_id, int(idx))
    interval = (step or {}).get("run_interval_s")
    if not interval or (session.get("name") and session.get("name") != step.get("name")):
        return None
    connected = {c["driver_guid"]: cid for cid, c in list(client.cars.items()) if c.get("driver_guid")}
    t0 = client.session_t0 + (step.get("wait_s") or 0)   # ponytail: the wait is assumed to be inside `elapsed_ms`; check it on a real session
    now = time.time() if now is None else now
    release = _release(client, connected, t0, interval, now)
    runs = _laps(server_id, client.session_t0, list(release), release)
    warmup, laps = step.get("run_warmup", 1), step.get("run_laps", 2)
    finished = {g for g, r in runs.items() if r.done(warmup, laps)}
    held = {g for g in connected if release[g] > now or g in finished}
    return {"stage": RUN_STAGE, "cars": sorted(cid for g, cid in connected.items() if g in held)}


def grid_now(server_id: int, since: float, step_idx: int | None = None) -> list[str]:
    """The grid of the single-run qualifying that ended at or after `since`, with the stewards' penalties; [] when there is no result file. The release order is the order of the first lap in the file. `step_idx`: the index of the qualifying in the sequence (acServer's file does not say which session it is); without it the only single-run step of the sequence."""
    from app import gridtimes
    from app.models import Server
    path = gridtimes.last_result_file(server_id, since, "QUALIFY")
    if path is None:
        return []
    with Session(engine) as s:
        srv = s.get(Server, server_id)
        steps = ((srv.session if srv else None) or {}).get("sequence") or []
        parsed = results.parse_result_file(path)
        runs_steps = [st for st in steps if st.get("run_interval_s")]
        step = (steps[step_idx] if step_idx is not None and 0 <= step_idx < len(steps) else (runs_steps[0] if len(runs_steps) == 1 else None))
        if step is not None and not step.get("run_interval_s"):
            step = None
        if step is None:
            return []
        laps: dict[str, list[int]] = {}
        cuts: dict[str, list[int]] = {}
        for lap in parsed["laps"]:
            if lap["driver_guid"] and lap["lap_time_ms"]:
                laps.setdefault(lap["driver_guid"], []).append(lap["lap_time_ms"])
                cuts.setdefault(lap["driver_guid"], []).append(lap["cuts"] or 0)
        runs = {g: singlerun.Run(g, l, cuts[g]) for g, l in laps.items()}
        ranked = singlerun.classify(runs, step.get("run_warmup", 1), step.get("run_laps", 2))
        ranked += [e["driver_guid"] for e in parsed["classification"] if e["driver_guid"] and e["driver_guid"] not in ranked]   # connected but never ran: behind everyone
        rows = [{"position": i + 1, "driver_guid": g, "driver_name": "", "car_model": "", "best_lap_ms": None, "total_time_ms": None} for i, g in enumerate(ranked)]
        pens = results.penalties_for(s, server_id, path.name)
        non_racing = results.non_racing_for(s, server_id, path.name)
    return results.apply_penalties({"type": "Qualify", "laps": [], "classification": rows}, pens, non_racing)["grid"]
