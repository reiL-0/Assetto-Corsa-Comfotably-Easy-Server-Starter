"""The session clock of a server that is «off» but still looks open.

A stopped server that the manager shows in the lobby (app/wake.py) must not freeze its session: if practice started at 17:10 and lasts
15 minutes, at 17:25 it is qualifying whether or not anybody came, and whoever joins sees the time that is really left.

- `Server.anchor_index` / `anchor_at` remember the last session start seen (a `new_session` or the current `session_info` of the real
  acServer: `anchor_at` is when that session started). `position` walks forward from there through the server's sessions (practice,
  qualify, race, in that order, as acServer plays them), looping when `LOOP_MODE` is on; it says which session it is and how long is left
  (None: never ran, or the cycle ended with no loop).
- The lobby (`wake.facade_info`) shows that position.
- On a start (`resume`, after `servers.start_server`, which reads the clock before spawning acServer) the real acServer is moved to that position through ACSP: the target session is
  redefined to the minutes that are left (`SET_SESSION_INFO`; its unit is minutes, and the running session's time left is that
  length minus what has elapsed) and `NEXT_SESSION` is sent until it is the current one. The original definition is put back
  as soon as the next session starts. Granularity: a minute.
- A race by laps has no known length: it counts as `RACE_LAPS_EST_MIN` minutes (plus its wait).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlmodel import Session

from app import supervisor
from app.db import engine
from app.live import acsp
from app.models import Server

log = logging.getLogger("acmanager.timeline")
RACE_LAPS_EST_MIN = 20
TYPES = (("PRACTICE", 1), ("QUALIFY", 2), ("RACE", 3))
_tasks: set[asyncio.Task] = set()


@dataclass
class Seg:
    index: int
    type: int
    name: str
    secs: int    # length on the clock, race wait included
    minutes: int  # the TIME of the section (0 for a race by laps)
    laps: int
    wait: int    # seconds


def segments(config: dict) -> list[Seg]:
    out = []
    for section, typ in TYPES:
        sec = config.get(section)
        if not sec:
            continue
        minutes, laps, wait = int(sec.get("TIME") or 0), int(sec.get("LAPS") or 0), int(sec.get("WAIT_TIME") or 0) if section == "RACE" else 0
        length = minutes * 60 if minutes and not laps else RACE_LAPS_EST_MIN * 60 if laps else 0
        if length:
            out.append(Seg(len(out), typ, str(sec.get("NAME") or section.title()), length + wait, minutes, laps, wait))
    return out


def position(config: dict, anchor_index: int | None, anchor_at: float | None, now: float | None = None) -> dict | None:
    """{index, remaining_s, elapsed_s} of the clock now, or None when it has no position (never ran, or ended)."""
    segs = segments(config)
    if not segs or anchor_index is None or anchor_at is None:
        return None
    now = time.time() if now is None else now
    loop = bool(int(config.get("SERVER", {}).get("LOOP_MODE", 1)))
    t, i = max(0.0, now - anchor_at), min(anchor_index, len(segs) - 1)
    while t >= segs[i].secs:
        t -= segs[i].secs
        i += 1
        if i == len(segs):
            if not loop:
                return None
            i, t = 0, t % sum(s.secs for s in segs)
    return {"index": i, "remaining_s": segs[i].secs - t, "elapsed_s": t}


def server_position(s: Server, now: float | None = None) -> dict | None:
    return position(s.config, s.anchor_index, s.anchor_at, now)


def set_anchor(server_id: int, index: int, at: float) -> None:
    with Session(engine) as sess:
        s = sess.get(Server, server_id)
        if s and (s.anchor_index, s.anchor_at) != (index, at):
            s.anchor_index, s.anchor_at = index, at
            sess.add(s)
            sess.commit()


def on_session_event(server_id: int, event: dict) -> None:
    """From the ACSP client: a session started, or the current session was described. Never raises."""
    try:
        cur = event.get("current_session_index", event.get("session_index"))
        if event["type"] == "new_session" or event.get("session_index") == cur:
            set_anchor(server_id, int(cur), time.time() - event.get("elapsed_ms", 0) / 1000)
    except Exception:
        log.exception("could not store the session anchor")


def definition(seg: Seg, minutes: int | None = None) -> bytes:
    return acsp.encode_set_session_info(seg.index, seg.name, seg.type, seg.laps, seg.minutes if minutes is None else minutes, seg.wait)


async def resume(server_id: int, planned: dict | None, planned_at: float, settle: float = 0.8) -> dict | None:
    """After a start: move the real acServer to where the clock was. `planned` is the position read BEFORE the server was started
    (`planned_at`: when): the real server's own first `new_session` re-anchors the clock to «now», so the clock cannot be read afterwards.
    Returns the position used (None: it starts as it is)."""
    if not planned:
        return None
    inst = supervisor.get(server_id)
    for _ in range(60):   # the plugin socket needs a moment to hear the server
        if inst and inst.acsp and inst.acsp.session:
            break
        await asyncio.sleep(0.5)
    else:
        return None
    with Session(engine) as sess:
        s = sess.get(Server, server_id)
        segs = segments(s.config) if s else []
    if not segs:
        return None
    cur = int(inst.acsp.session.get("current_session_index", 0))
    target, left = planned["index"], planned["remaining_s"] - (time.time() - planned_at)   # what was left, minus the time the start took
    elapsed = segs[target].secs - left
    if left < 30:   # about to end: the next one, in full
        target, left, elapsed = (target + 1) % len(segs), None, 0
    if target == cur and elapsed < 20:
        return None
    seg = segs[target]
    if left is not None and seg.minutes and not seg.laps:   # redefine the length to what is left; the original comes back when the next session starts
        already = inst.acsp.session.get("elapsed_ms", 0) / 1000 if target == cur else 0   # a session already running keeps counting from its own start
        inst.acsp.send(definition(seg, max(1, round((left + already) / 60))))
        inst.acsp.restore_when_session_changes = (target, definition(seg))
    for _ in range((target - cur) % len(segs)):
        await asyncio.sleep(settle)
        inst.acsp.send(acsp.encode_next_session())
    log.info("server %s resumed at session %s, %s s left", server_id, target, None if left is None else int(left))
    return {"index": target, "remaining_s": left}


def start_resume(server_id: int, planned: dict | None, planned_at: float) -> None:
    """Fire and forget (keeps a reference so the task is not collected)."""
    task = asyncio.create_task(resume(server_id, planned, planned_at))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
