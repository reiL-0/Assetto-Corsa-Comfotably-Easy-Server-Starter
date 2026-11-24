"""Scheduled starts: a saved event runs on a server at a set time, with Discord reminders before it, and ends at its end.

`run_forever` ticks every TICK seconds from the app lifespan. Each tick, for every pending schedule: post the nearest
reminder that has come due (older missed ones are marked sent, not posted), and at `start_at` load the event onto the
server and restart it (`server_service.apply`) unless a player already woke it with the event loaded (`loaded`). A start
that is more than LATE seconds overdue (the manager was down) is marked `missed` instead of surprising the players.
Starting restarts the server: whoever is on it is disconnected.

With a `duration_min` the schedule stays `running` until `start_at + duration`: 5 minutes before, the in-game chat is
told; at the end the server is stopped as soon as nobody is on it (at most END_GRACE later, whoever is still there) (a Discord notice goes out) and the schedule is `done`. Without a duration it is `done` as soon as it has
started and only the idle stop ends the session.

The window of an event (`open_window`) is from EARLY minutes before the start until its end (3 h after the start when no
duration is set). Inside it a stopped server is woken by `wake` when a player tries to connect (app/wake.py); a server whose
`wake` mode is `always` is also started, as it was left, with no event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app import announcement, discord, metrics, supervisor
from app import league
from app import league
from app.league import session_for, settle_metrics
from app.config import settings
from app.db import SessionDep, engine
from app.models import Event, Rsvp, Schedule, Server, User
from app.live import acsp
from app.schemas.servers import UNLIMITED_MIN, SessionIn
from app.services import server_service
from app.services.server_service import ServerError

log = logging.getLogger("acmanager.schedule")
router = APIRouter(prefix="/schedules", tags=["schedules"])
TICK = 20
LATE = 600
EARLY = 60 * 60  # seconds before the start from which a connection attempt wakes the server
END_WARNING = 5 * 60
END_GRACE = 60 * 60  # at the end of the event the server waits for the people still on it, at most this long
NO_DURATION_WINDOW = 3 * 60 * 60
REALIGN_EVERY = 1.0   # s between looks at the events that have just begun (no Discord, no I/O but one small query: the practice must start ON the hour, not up to TICK s after it)
REALIGN_TOLERANCE = 1.0   # s: a practice whose clock began at most this long before the hour counts as begun at it
REALIGN_WINDOW = 90   # s after the hour in which a practice that began early is still put right (later: the manager was down; nobody resets a practice that has been running for minutes)
EARLY_STEP_MIN = 1440   # the longest a step of a session sequence can be: the early practice of a sequence


class ScheduleIn(BaseModel):
    event_id: int
    server_id: int
    start_at: float  # unix seconds
    reminders: list[int] = Field(default=[60, 10], max_length=5)  # minutes before; 1..1440
    duration_min: int | None = Field(default=None, ge=1, le=1440)  # minutes the event lasts; the server is stopped at the end
    info: str = Field(default="", max_length=600)  # appended to the Discord messages of this schedule
    notes: str = Field(default="", max_length=1000)  # the "Notas" section of the sign-up announcement
    silent_past: bool = False  # reminders whose time has already passed are marked sent (the caller announced the event itself)


class ScheduleOut(BaseModel):
    id: int
    event_id: int
    event_title: str
    server_id: int
    server_name: str
    start_at: float
    reminders: list[int]
    sent: list[int]
    duration_min: int | None
    info: str
    notes: str
    state: str
    result: str
    rsvp: dict[str, int]  # yes | maybe | no -> how many reacted that way


def _counts(sess: Session, schedule_id: int) -> dict[str, int]:
    rows = sess.exec(select(Rsvp.status).where(Rsvp.schedule_id == schedule_id)).all()
    return {st: rows.count(st) for st in discord.RSVP.values()}


def _out(sess: Session, sc: Schedule) -> ScheduleOut:
    ev, srv = sess.get(Event, sc.event_id), sess.get(Server, sc.server_id)
    return ScheduleOut(id=sc.id, event_id=sc.event_id, event_title=ev.title if ev else "(borrado)", server_id=sc.server_id,
                       server_name=srv.name if srv else "(borrado)", start_at=sc.start_at, reminders=sc.reminders, sent=sc.sent,
                       duration_min=sc.duration_min, info=sc.info, notes=sc.notes, state=sc.state, result=sc.result,
                       rsvp=_counts(sess, sc.id))


@router.get("", response_model=list[ScheduleOut])
def list_schedules(sess: SessionDep) -> list[ScheduleOut]:
    """Pending ones first (soonest on top), then the 10 most recent finished ones."""
    rows = sess.exec(select(Schedule).order_by(Schedule.start_at)).all()
    pending = [r for r in rows if r.state in ("pending", "running")]
    done = [r for r in rows if r.state not in ("pending", "running")][-10:]
    return [_out(sess, r) for r in pending + done[::-1]]


@router.post("", response_model=ScheduleOut, status_code=201)
def create(body: ScheduleIn, sess: SessionDep) -> ScheduleOut:
    if not sess.get(Event, body.event_id) or not sess.get(Server, body.server_id):
        raise HTTPException(404, "event or server not found")
    if body.start_at <= time.time():
        raise HTTPException(422, "start_at must be in the future")
    if any(not 1 <= m <= 1440 for m in body.reminders):
        raise HTTPException(422, "reminders are minutes between 1 and 1440")
    new_end = body.start_at + (body.duration_min or NO_DURATION_WINDOW // 60) * 60
    for other in sess.exec(select(Schedule).where(Schedule.server_id == body.server_id, Schedule.state.in_(("pending", "running")))).all():
        if body.start_at < end_at(other) and other.start_at < new_end:   # queued one after another is fine; on top of each other is not
            ev = sess.get(Event, other.event_id)
            raise HTTPException(409, f"overlaps with «{ev.title if ev else other.event_id}» (schedule {other.id}) on that server")
    from app import discord_config
    reminders = sorted(set(body.reminders if "reminders" in body.model_fields_set else discord_config.reminders()), reverse=True)   # (not asked: the client's own cadence, else 24 h / 1 h / 10 min)
    sc = Schedule(event_id=body.event_id, server_id=body.server_id, start_at=body.start_at, duration_min=body.duration_min,
                  info=body.info, notes=body.notes, reminders=reminders,
                  sent=[m for m in reminders if body.silent_past and body.start_at - m * 60 <= time.time()])
    sess.add(sc)
    sess.commit()
    sess.refresh(sc)
    return _out(sess, sc)


@router.delete("/{schedule_id}", status_code=204)
def delete(schedule_id: int, sess: SessionDep) -> None:
    sc = sess.get(Schedule, schedule_id)
    if not sc:
        raise HTTPException(404, "schedule not found")
    for r in sess.exec(select(Rsvp).where(Rsvp.schedule_id == schedule_id)):
        sess.delete(r)
    sess.delete(sc)
    sess.commit()


@router.get("/{schedule_id}/rsvps")
def rsvps(schedule_id: int, sess: SessionDep) -> list[dict]:
    """Who answered the announcement: Discord id, the linked username (None until they link their account) and the status."""
    rows = sess.exec(select(Rsvp, User.username).join(User, User.id == Rsvp.user_id, isouter=True).where(Rsvp.schedule_id == schedule_id)).all()
    return [{"discord_id": r.discord_id, "username": name, "status": r.status} for r, name in rows]


def _extra(sc: Schedule) -> str:
    return f"\n{sc.info}" if sc.info else ""


def _when(sc: Schedule) -> str:
    return f"<t:{int(sc.start_at)}:F> (<t:{int(sc.start_at)}:R>)"  # Discord shows it in each reader's own time zone


def end_at(sc: Schedule) -> float:
    return sc.start_at + (sc.duration_min or 0) * 60 if sc.duration_min else sc.start_at + NO_DURATION_WINDOW


def open_window(sess: Session, server_id: int, now: float) -> Schedule | None:
    """The schedule whose window this server is in right now: from EARLY before the start (still pending) or running, until its end."""
    for sc in sess.exec(select(Schedule).where(Schedule.server_id == server_id, Schedule.state.in_(("pending", "running")))).all():
        if sc.start_at - EARLY <= now < end_at(sc):
            return sc
    return None


def _practice_index(applied: SessionIn) -> int | None:
    """0 when the cycle OPENS with a practice (a plain event with a practice, or a sequence whose first step is one); None otherwise. A practice that comes later in a sequence is no use to wait
    in: the sessions before it would begin before the hour."""
    if applied.sequence:
        return 0 if applied.sequence[0].kind == "practice" else None
    return 0 if applied.practice_min else None


def has_practice(applied: SessionIn) -> bool:
    return _practice_index(applied) is not None


def nominal_practice_min(applied: SessionIn) -> int:
    """Minutes of the event's own opening practice; 0 = it has none, or it is unlimited (nothing to put right at the hour)."""
    if _practice_index(applied) is None:
        return 0
    m = applied.sequence[0].time_min if applied.sequence else applied.practice_min or 0
    return m if 0 < m < (EARLY_STEP_MIN if applied.sequence else UNLIMITED_MIN) else 0


def early_practice(applied: SessionIn) -> SessionIn:
    """The session for a server that opens BEFORE the hour (a player woke it): its practice has no end until the hour, when `realign` gives it its own length and restarts it, so the cycle can
    never get ahead of what was announced however early it was woken."""
    if applied.sequence:
        steps = [applied.sequence[0].model_copy(update={"time_min": EARLY_STEP_MIN}), *applied.sequence[1:]]
        return applied.model_copy(update={"sequence": steps})
    return applied.model_copy(update={"practice_min": UNLIMITED_MIN})


def early_config_in_force(srv: Server, applied: SessionIn) -> bool:
    """The server still holds the configuration of an early opening (a practice with no end) although the event's own practice is bounded."""
    if not nominal_practice_min(applied):
        return False
    held = srv.session or {}
    if applied.sequence:
        steps = held.get("sequence") or []
        return bool(steps) and steps[0].get("time_min") == EARLY_STEP_MIN
    return held.get("practice_min") == UNLIMITED_MIN


async def _load_once(sess: Session, sc: Schedule, ev: Event, srv: Server) -> bool:
    """Loads the event on the server unless it already is; True if THIS call applied it. Decision and apply are one step under the server's lock:
    a wake and the tick that are both about to load the same event queue up, and the second one re-reads `loaded` after the first finished
    (so it does not apply again and kick whoever is joining, and if the first failed it tries itself).
    Before the hour an event with a bounded practice is loaded with a practice that does not end (`early_practice`; `realign` gives it its own length at the hour). A server that still holds that
    configuration while it is STOPPED, once the hour has come, is loaded again: whatever starts it (a wake, the tick) must not play a practice that never ends; a running one is `realign`'s."""
    await settle_metrics()   # (before the lock: a wait for the writer must not hold the server)
    async with server_service.server_lock(srv.id):
        sess.refresh(sc, ["loaded"])
        applied = session_for(sess, ev, sc.start_at).model_copy(update={"restart": True})
        inst = supervisor.get(srv.id)
        before_the_hour = time.time() < sc.start_at
        stale = sc.loaded and not before_the_hour and not (inst and inst.running) and early_config_in_force(srv, applied)
        if sc.loaded and not stale:
            return False
        if before_the_hour and nominal_practice_min(applied):   # woken early: the practice waits for the hour (`realign`)
            applied = early_practice(applied)
        result = await server_service.apply(sess, srv, applied, held=True)
        if result.start_error:   # the config is on the server but it did not start: not "loaded" (a wake / the next tick tries again), the reason is kept
            raise result.start_error
        sc.rulebook_snapshot = applied.rulebook_snapshot.model_dump(mode="json") if applied.rulebook_snapshot else None   # (frozen for THIS run: the next event applied on the server must not change how this one is counted)
        sc.loaded = True
        sess.add(sc)
        sess.commit()
        return True


async def wake(server_id: int, now: float | None = None) -> bool:
    """A player tried to connect to a stopped server: start it if its event window is open. With the event not on it yet
    it is loaded (as a start would); once loaded, a server stopped by idle or a crash is simply started again."""
    now = now if now is not None else time.time()
    inst = supervisor.get(server_id)
    if inst and inst.running:
        return False
    with Session(engine) as sess:
        sc = open_window(sess, server_id, now)
        ev, srv = (sess.get(Event, sc.event_id), sess.get(Server, sc.server_id)) if sc else (None, None)
        if sess.get(Server, server_id) and sess.get(Server, server_id).wake == "always" and not sc:   # no event: start it as it was left
            try:
                await server_service.start(sess, server_id)
            except ServerError as e:
                log.warning("wake of server %s failed: %s", server_id, e.detail)
                return False
            metrics.enqueue(server_id, "wake", name="always")
            return True
        if not sc or not ev or not srv:
            return False
        if now < sc.start_at:   # before the hour: an event with no practice has nothing to wait in, and its race or qualifying would begin early
            try:
                early_ok = has_practice(session_for(sess, ev, sc.start_at, notify=False))
            except HTTPException:
                early_ok = True   # (the load below says why the event cannot run)
            if not early_ok:
                return False
        try:
            if not await _load_once(sess, sc, ev, srv):   # already loaded (an earlier wake, the tick): just start the server
                await server_service.start(sess, server_id)
        except ServerError as e:
            log.warning("wake of server %s failed: %s", server_id, e.detail)
            return False
        metrics.enqueue(server_id, "wake", name=ev.title)
    return True


async def tick(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    with Session(engine) as sess:
        for sc in sess.exec(select(Schedule).where(Schedule.state.in_(("pending", "running")))).all():
            ev, srv = sess.get(Event, sc.event_id), sess.get(Server, sc.server_id)
            if not ev or not srv:
                sc.state, sc.result = "failed", "event or server was deleted"
            elif sc.state == "pending":
                await _tick_pending(sess, sc, ev, srv, now)
            else:
                await _tick_running(sc, ev, srv, now)
            sess.add(sc)
            sess.commit()
        for sc in sess.exec(select(Schedule).where(Schedule.state.in_(("running", "done")), Schedule.start_at > now - 2 * 86400)).all():
            ev = sess.get(Event, sc.event_id)   # a league event's Race result counts for its league, whatever the event's calendar status
            if ev and (ev.league_id or ev.count_only_league_id):
                league.count_results(sess, sc, ev, end_at(sc) + END_GRACE)


def _result_s(srv: Server) -> int:
    """Seconds the server shows the results between two sessions (acServer's RESULT_SCREEN_TIME; its own default is 10)."""
    try:
        return max(0, int(float(((srv.config or {}).get("SERVER") or {}).get("RESULT_SCREEN_TIME", 10))))
    except (TypeError, ValueError, OverflowError):
        return 10


def _rsvp_payload(sess: Session, sc: Schedule, ev: Event, srv: Server, counts: dict[str, int]) -> dict:
    v = announcement.variables(ev.title, srv.name, ev.data, sc.start_at, counts, sc.notes, sc.info, ev.league_id, result_s=_result_s(srv))
    return announcement.render(announcement.current(sess), v)


async def _rsvp(sess: Session, sc: Schedule, ev: Event, srv: Server) -> None:
    """Post the sign-up announcement once, then each tick turn its reactions into `Rsvp` rows (linked to the user whose
    Discord account matches) and keep the counts on the message current. Needs ACM_DISCORD_BOT_TOKEN + ACM_DISCORD_CHANNEL."""
    if not discord.rsvp_enabled():
        return
    try:
        if not sc.rsvp_message:
            payload = _rsvp_payload(sess, sc, ev, srv, dict.fromkeys(discord.RSVP.values(), 0))
            before = discord.bot_rev()
            mid = await asyncio.to_thread(discord.rsvp_post, payload)
            if discord.bot_rev() != before:   # the bot or the channel was changed while this was being posted: that message is in the old place, it is not this event's announcement
                log.warning("rsvp of schedule %s: the bot changed while posting; it will be posted again", sc.id)
                return
            sc.rsvp_text = json.dumps(payload, sort_keys=True)
            sc.rsvp_message = mid
            return
        found = await asyncio.to_thread(discord.rsvp_read, sc.rsvp_message)
    except urllib.error.HTTPError as e:
        log.warning("rsvp of schedule %s: discord answered %s (tried again next tick)", sc.id, e.code)   # 429 is expected now and then
        return
    except Exception:
        log.exception("rsvp of schedule %s failed", sc.id)
        return
    mine: dict[str, set[str]] = {}
    for st, ids in found.items():
        for i in ids:
            mine.setdefault(i, set()).add(st)
    old = {r.discord_id: r for r in sess.exec(select(Rsvp).where(Rsvp.schedule_id == sc.id))}
    linked = {u.discord_id: u.id for u in sess.exec(select(User).where(User.discord_id.in_(list(mine))))}
    for i, sts in mine.items():
        prev = old.get(i)
        new = [s for s in sorted(sts, key=list(discord.RSVP.values()).index) if not prev or s != prev.status]   # several reactions: the newest one wins
        sess.merge(Rsvp(schedule_id=sc.id, discord_id=i, user_id=linked.get(i), status=new[0] if new else prev.status))
    for i, r in old.items():
        if i not in mine:   # took every reaction back
            sess.delete(r)
    sess.commit()
    payload = _rsvp_payload(sess, sc, ev, srv, _counts(sess, sc.id))
    text = json.dumps(payload, sort_keys=True)
    if text != sc.rsvp_text:
        try:
            await asyncio.to_thread(discord.rsvp_edit, sc.rsvp_message, payload)
            sc.rsvp_text = text
        except Exception:
            log.exception("rsvp edit of schedule %s failed", sc.id)


async def _tick_pending(sess: Session, sc: Schedule, ev: Event, srv: Server, now: float) -> None:
    await _rsvp(sess, sc, ev, srv)
    due = [m for m in sc.reminders if m not in sc.sent and now >= sc.start_at - m * 60]
    if due and now < sc.start_at:
        discord.announce(f"⏰ **{ev.title}** en {srv.name}: empieza {_when(sc)}" + _extra(sc))
        sc.sent = [*sc.sent, *due]
    if now < sc.start_at:
        return
    if not sc.loaded and now - sc.start_at > LATE:
        sc.state, sc.result = "missed", "the manager was not running at the start time"
        return
    try:
        await _load_once(sess, sc, ev, srv)   # (a player may have woken the server early with the event already on it: then nothing is applied again)
        sc.state = "running" if sc.duration_min else "done"
        discord.announce(f"🏁 **{ev.title}** ya está en marcha en {srv.name}" + _extra(sc))
    except ServerError as e:
        sc.state, sc.result = "failed", str(e.detail)
        discord.announce(f"⚠️ **{ev.title}** no pudo iniciarse en {srv.name}: {e.detail}")


async def _realign(sess: Session, sc: Schedule, now: float) -> None:
    """The hour of an event that was opened early: its practice must begin NOW, with its own length. A running server (woken by a player, practice without end) is, under the server's lock:
    1. given its own configuration again (apply without restart: what the next start and the session clock must see), 2. told the practice's length (SET_SESSION_INFO) and 3. restarted
    (RESTART_SESSION). Nobody is disconnected and the clock starts again (measured on acServer: elapsed back to 0, `time_min` the new one, the cars stay). It is idempotent: it asks only
    for a practice whose clock began before the hour, so it does not matter whether the tick already moved the schedule to `running`. A stopped server is `_load_once`'s (stale configuration)."""
    ev, srv = sess.get(Event, sc.event_id), sess.get(Server, sc.server_id)
    if ev is None or srv is None:
        return
    try:
        applied = session_for(sess, ev, sc.start_at, notify=False)
    except HTTPException as e:
        log.warning("schedule %s: the practice could not be put right at the hour: %s", sc.id, e.detail)
        return
    nominal = nominal_practice_min(applied)
    if not nominal:
        return

    def in_window() -> bool:
        return time.time() - sc.start_at <= REALIGN_WINDOW

    def needs_it(client) -> bool:   # the practice began before the hour and is still the one running
        return (client.session or {}).get("session_type") == 1 and client.session_t0 < sc.start_at - REALIGN_TOLERANCE and in_window()

    async def store_config() -> None:   # what the next start and the session clock must see: the event's own practice, only while the stored one is still the early one (an admin's own apply is not undone)
        if not early_config_in_force(srv, applied):
            return
        try:
            await server_service.apply(sess, srv, applied.model_copy(update={"restart": False}), held=True)
        except ServerError as e:   # (the practice is put right anyway; it is tried again at the next look while the window lasts)
            log.warning("schedule %s: the event's own configuration could not be stored at the hour: %s", sc.id, e.detail)

    inst = supervisor.get(sc.server_id)
    if not (inst and inst.running and inst.acsp):
        return
    if not needs_it(inst.acsp):
        if early_config_in_force(srv, applied) and in_window():   # (the practice already began at the hour but its configuration could not be stored the first time)
            async with server_service.server_lock(sc.server_id):
                await store_config()
        return
    async with server_service.server_lock(sc.server_id):
        inst = supervisor.get(sc.server_id)   # (the lock may have been waited for: what is there NOW is what counts)
        client = inst.acsp if inst and inst.running else None
        if client is None or not needs_it(client):
            return
        s = client.session or {}
        await store_config()
        for _ in range(3):   # (UDP: it is only done when acServer has begun the practice again, which `session_t0` says)
            if client.session_t0 >= sc.start_at - REALIGN_TOLERANCE:
                break
            client.send(acsp.encode_set_session_info(s.get("session_index") or 0, s.get("name") or "Practice", 1, 0, nominal, 0))
            client.send(acsp.encode_restart_session())
            await asyncio.sleep(1.5)
        client.keep_off = False   # (`apply` stops the practice being kept across an idle stop because it assumes the old practice goes on: this one is new)
    log.info("schedule %s: the practice of server %s began early and was started again at the hour (%s min)", sc.id, sc.server_id, nominal)


async def realign_tick(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    with Session(engine) as sess:
        for sc in sess.exec(select(Schedule).where(Schedule.state.in_(("pending", "running")), Schedule.loaded != False, Schedule.start_at <= now, Schedule.start_at > now - REALIGN_WINDOW)).all():   # noqa: E712
            await _realign(sess, sc, now)


async def realign_forever() -> None:
    while True:
        try:
            await realign_tick()
        except Exception:
            log.exception("schedule realign failed")
        await asyncio.sleep(REALIGN_EVERY)


async def _tick_running(sc: Schedule, ev: Event, srv: Server, now: float) -> None:
    end = end_at(sc)
    inst = supervisor.get(sc.server_id)
    if not sc.end_warned and now >= end - END_WARNING and now < end:
        sc.end_warned = True
        if inst and inst.running and inst.acsp:
            inst.acsp.send(acsp.encode_broadcast_chat("El evento termina en 5 minutos"))
    if now >= end:
        if inst and inst.running:
            if inst.acsp and inst.acsp.cars and now < end + END_GRACE:   # a race that overran: let it finish, stop when the last one leaves
                return
            await server_service.stop_instance(sc.server_id, inst, "event_end")
        sc.state = "done"
        discord.announce(f"🔚 **{ev.title}** terminó: servidor {srv.name} detenido")


def next_wait(now: float) -> float:
    """Seconds the loop sleeps: TICK, or less so that the next start hour is not slept through (an event nobody woke early begins ON the hour, not up to TICK s after it)."""
    try:
        with Session(engine) as sess:
            nxt = sess.exec(select(func.min(Schedule.start_at)).where(Schedule.state == "pending", Schedule.start_at > now)).one()
    except Exception:
        log.exception("schedule: next start could not be read")
        return TICK
    return TICK if nxt is None else max(0.2, min(TICK, nxt - now))


async def run_forever() -> None:
    while True:
        try:
            await tick()
        except Exception:
            log.exception("schedule tick failed")
        await asyncio.sleep(next_wait(time.time()))
