"""Activity log + the aggregated numbers behind the admin metrics panel."""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app import discord
from app.auth import require
from app.db import SessionDep, engine
from app.models import Activity

log_ = logging.getLogger("acmanager.metrics")
router = APIRouter(prefix="/metrics", tags=["metrics"], dependencies=[Depends(require("steward"))])

KEEP_DAYS = 120
INCIDENTS = ("server_start", "server_stop", "server_crash", "import_ok", "import_error", "http_5xx")


def log(server_id: int, kind: str, *, guid=None, name=None, car=None, track=None, value=None, cuts: int | None = None, ts: float | None = None) -> None:
    """Record one event. Never raises: metrics must not be able to break a lap, a start or a request."""
    try:
        with Session(engine) as s:
            s.add(Activity(ts=ts if ts is not None else time.time(), server_id=server_id, kind=kind, guid=guid, name=name,
                           car=car, track=track, value=value, cuts=cuts))
            s.commit()
    except Exception:
        log_.exception("could not record %s", kind)
    try:
        discord.on_event(server_id, kind, name, value)
    except Exception:
        log_.exception("discord hook failed for %s", kind)


def purge(now: float | None = None) -> int:
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    with Session(engine) as s:
        old = s.exec(select(Activity).where(Activity.ts < cutoff)).all()
        for a in old:
            s.delete(a)
        s.commit()
    return len(old)


def _day(ts: float, off: int) -> int:
    return int((ts + off * 60) // 86400)


def summary(days: int = 14, tz_offset_min: int = 0, now: float | None = None, hours: int = 48) -> dict:
    """Per-day and recent numbers. `tz_offset_min` (minutes east of UTC, like -360 for Mexico City) decides where a day ends."""
    now = now or time.time()
    first_day = _day(now, tz_offset_min) - days + 1
    since = min((first_day * 86400) - tz_offset_min * 60, now - hours * 3600)
    with Session(engine) as s:
        rows = s.exec(select(Activity).where(Activity.ts >= since, Activity.ts <= now).order_by(Activity.ts)).all()

    daily = {first_day + i: {"peak": 0, "player_minutes": 0, "sessions": 0, "laps": 0, "drivers": set(), "crashes": 0,
                             "starts": 0, "import_errors": 0, "http_5xx": 0} for i in range(days)}
    tracks, cars, drivers = Counter(), Counter(), Counter()
    names: dict[str, str] = {}
    online_by_bucket: dict[int, dict[int, float]] = defaultdict(dict)  # 10-min bucket -> server -> max players
    for a in rows:
        d = daily.get(_day(a.ts, tz_offset_min))
        k = a.kind
        if k == "online":
            bucket = int(a.ts // 600)
            online_by_bucket[bucket][a.server_id] = max(online_by_bucket[bucket].get(a.server_id, 0), a.value or 0)
        if d is None:
            continue
        if k == "online":
            d["peak"] = max(d["peak"], int(a.value or 0))
            d["player_minutes"] += int(a.value or 0)  # one sample per minute
        elif k == "session":
            d["sessions"] += 1
            if a.track:
                tracks[a.track] += 1
        elif k == "lap":
            d["laps"] += 1
            if a.car:
                cars[a.car] += 1
            if a.guid:
                d["drivers"].add(a.guid)
                drivers[a.guid] += 1
                names[a.guid] = a.name or names.get(a.guid, "")
        elif k == "join" and a.guid:
            d["drivers"].add(a.guid)
        elif k == "server_crash":
            d["crashes"] += 1
        elif k == "server_start":
            d["starts"] += 1
        elif k == "import_error":
            d["import_errors"] += 1
        elif k == "http_5xx":
            d["http_5xx"] += 1

    series_from = int((now - hours * 3600) // 600)
    online = [{"t": b * 600, "v": int(sum(online_by_bucket[b].values())) if b in online_by_bucket else 0}
              for b in range(series_from, int(now // 600) + 1)]
    incidents = [{"ts": a.ts, "kind": a.kind, "server_id": a.server_id, "name": a.name, "value": a.value}
                 for a in rows if a.kind in INCIDENTS][-30:][::-1]
    return {
        "daily": [{"day": d, **{k: (len(v) if k == "drivers" else v) for k, v in daily[d].items()}} for d in sorted(daily)],
        "online": online,
        "top_tracks": [{"track": t, "sessions": n} for t, n in tracks.most_common(5)],
        "top_cars": [{"car": c, "laps": n} for c, n in cars.most_common(5)],
        "top_drivers": [{"guid": g, "name": names.get(g, g), "laps": n} for g, n in drivers.most_common(5)],
        "incidents": incidents,
        "first_day": first_day,
    }


@router.get("/activity")
def activity(sess: SessionDep, days: int = 14, tz: int = 0, hours: int = 48) -> dict:
    from app import supervisor  # lazy: supervisor logs through this module

    out = summary(max(1, min(days, KEEP_DAYS)), max(-840, min(tz, 840)), hours=max(1, min(hours, 24 * 14)))
    running = [i for i in supervisor._instances.values() if i.running]
    out["now"] = {
        "servers_running": len(running),
        "online": sum(sum(1 for d in i.acsp.board.drivers if d.connected) for i in running if i.acsp),
    }
    return out


@router.get("/now")
def now() -> dict:
    """What is happening this second, cheap enough for the panel to poll every few seconds."""
    from sqlmodel import select as _select

    from app import supervisor
    from app.models import Server

    with Session(engine) as sess:
        names = {s.id: s.name for s in sess.exec(_select(Server)).all()}
    servers = []
    for sid, inst in supervisor._instances.items():
        if not inst.running:
            continue
        board = inst.acsp.board if inst.acsp else None
        servers.append({
            "id": sid, "name": names.get(sid, f"#{sid}"), "uptime_s": int(inst.uptime),
            "online": sum(1 for d in board.drivers if d.connected) if board else 0,
            "track": board.session.get("track") if board else None,
            "session": board.session.get("name") if board else None,
        })
    return {"servers_running": len(servers), "online": sum(s["online"] for s in servers), "servers": servers}
