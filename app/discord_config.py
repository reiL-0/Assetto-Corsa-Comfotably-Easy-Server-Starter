"""The hosting client's OWN Discord bot, set up from the admin panel instead of the server's environment (a client cannot touch ACM_DISCORD_* or restart anything).

* GET    /api/v1/discord       what is configured: never the token (only `token_last4`), the bot's and the channels' names as Discord gave them when they were saved, the reminders, the switches
* PUT    /api/v1/discord       partial update; the token, when sent, is checked against Discord (/users/@me) together with every channel (/channels/<id>) BEFORE anything is stored, and a refusal
                               says what to fix; nothing is stored when Discord cannot be reached
* DELETE /api/v1/discord       forgets the bot (token file and settings): the environment's webhooks (the league's setup) are in charge again
* POST   /api/v1/discord/test  one test message to the channel(s), at most one every TEST_EVERY seconds

Where things live: the token in its own file per version (`<data dir>/secrets/discord_bot_token.<token_rev>`, folder 0700, file 0600), everything else in the `settings` table under the key `discord` (no
migration). The settings row is the ONE point where a change becomes live: it names the token version, so a reader never combines a new token with an old channel, a failed save leaves the old bot
working, and the old token files are deleted after it. Two changes at once cannot overwrite each other (the second gets a 409 and tries again). Nothing here is ever logged, audited or answered with the token; the request body is read by hand (a validation error of a framework would echo the value). `active()` is what
app/discord.py asks on every send: None means «no bot configured here», and the environment settings (the league's webhooks and bot) work exactly as before.
Changing the token or the channel clears the sign-up messages of the events that have not started (they are posted again by the new bot; the old ones stay in the old channel). Changing the reminders
rewrites those of the events that have not started and keeps which were already sent. Check: tests/test_discord_config.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.auth import require
from app.config import settings
from app.db import engine
from app.models import Schedule, Setting

log = logging.getLogger("acmanager.discord_config")
router = APIRouter(prefix="/discord", tags=["discord"], dependencies=[Depends(require("admin"))])

KEY = "discord"
DEFAULT_REMINDERS = [1440, 60, 10]
TEST_EVERY = 10.0
_ID = re.compile(r"^\d{17,20}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._\-]{50,120}$")
_write = threading.Lock()   # one change at a time (the checks against Discord happen before it, outside any lock)
_test_lock = threading.Lock()
_last_test = 0.0


def _token_path(rev: int) -> Path:
    return Path(settings.data_dir) / "secrets" / f"discord_bot_token.{rev}"


def _read_token(rev: int) -> str | None:
    try:
        return _token_path(rev).read_text().strip() or None
    except OSError:
        return None


def _write_token(rev: int, token: str) -> None:
    path = _token_path(rev)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)


def _forget_tokens(keep: int | None = None) -> None:
    for p in _token_path(0).parent.glob("discord_bot_token.*"):
        if keep is None or p.name != f"discord_bot_token.{keep}":
            p.unlink(missing_ok=True)


def _row(sess: Session) -> dict:
    try:
        s = sess.get(Setting, KEY)
    except OperationalError:   # the table does not exist yet: this is asked at import time (the sample announcement asks for the role) and on a fresh database before init_db has made it. No bot configured.
        sess.rollback()
        return {}
    return dict(s.value) if s else {}


def _save(sess: Session, value: dict) -> None:
    s = sess.get(Setting, KEY)
    if s is None:
        sess.add(Setting(key=KEY, value=value))
    else:
        s.value = value
        sess.add(s)


def active() -> dict | None:
    """The bot this manager posts with, or None when none was set up here (then the environment's settings are in charge). Read on every send: a change is live at once. Row and token come
    from the same version, so the pair is always one that was saved together."""
    with Session(engine) as sess:
        v = _row(sess)
    token = _read_token(v.get("token_rev", 0)) if v else None
    if not v or not token or not v.get("channel_id"):
        return None
    return {"token": token, "channel_id": v["channel_id"], "status_channel_id": v.get("status_channel_id") or v["channel_id"], "role_id": v.get("role_id") or "",
            "reminders": v.get("reminders", DEFAULT_REMINDERS), "rsvp_enabled": v.get("rsvp_enabled", True), "announce_enabled": v.get("announce_enabled", True),
            "status_enabled": v.get("status_enabled", True), "bot_rev": v.get("bot_rev", 0)}


def reminders() -> list[int]:
    """The minutes before an event at which the Discord reminders go out: the client's choice, else the usual 24 h / 1 h / 10 min."""
    with Session(engine) as sess:
        return list(_row(sess).get("reminders", DEFAULT_REMINDERS))


def note(error: str | None) -> None:
    """Remember the last reason a post failed (shown in the panel), or clear it. Never holds the token: callers pass a short sentence. Under the same lock as a change, and re-reading the row
    inside it, so it can never put an older version of the settings back."""
    with _write, Session(engine) as sess:
        v = _row(sess)
        if v and v.get("last_error") != error:
            v["last_error"] = error
            _save(sess, v)
            sess.commit()


def _view(v: dict) -> dict:
    token = _read_token(v.get("token_rev", 0)) if v else None
    return {"configured": bool(v and token and v.get("channel_id")), "bot_name": v.get("bot_name"), "token_last4": v.get("token_last4") if token else None, "channel_id": v.get("channel_id", ""),
            "channel_name": v.get("channel_name"), "status_channel_id": v.get("status_channel_id", ""), "status_channel_name": v.get("status_channel_name"), "role_id": v.get("role_id", ""),
            "reminders": v.get("reminders", DEFAULT_REMINDERS), "rsvp_enabled": v.get("rsvp_enabled", True), "announce_enabled": v.get("announce_enabled", True),
            "status_enabled": v.get("status_enabled", True), "last_error": v.get("last_error"), "default_reminders": DEFAULT_REMINDERS}


@router.get("")
def get() -> dict:
    with Session(engine) as sess:
        return _view(_row(sess))


def _discord(token: str, path: str) -> dict:
    """One GET to Discord as the bot, with the sentence a person needs when it fails. The token never leaves this call."""
    from app import discord   # (late: app.discord asks this module)
    try:
        return discord._api("GET", path, bearer=f"Bot {token}") or {}
    except urllib.error.HTTPError as e:
        what = {401: "Discord no aceptó ese token: cópialo de nuevo desde el portal de desarrolladores (Bot, Reset Token).",
                403: "El bot no tiene permiso en ese canal: invítalo al servidor y dale permiso para ver el canal, enviar mensajes y añadir reacciones.",
                404: "Ese canal no existe, o el bot no lo ve: revisa el id y que el bot esté en ese servidor."}.get(e.code)
        raise HTTPException(422, what or f"Discord respondió con un error ({e.code}); no se guardó nada.") from None
    except Exception:
        raise HTTPException(502, "No se pudo contactar a Discord; no se guardó nada. Inténtalo de nuevo en un momento.") from None


def _ids(body: dict, name: str, required: bool) -> str:
    v = body.get(name)
    v = "" if v is None else str(v).strip()
    if not v and required:
        raise HTTPException(422, "Falta el id del canal de anuncios.")
    if v and not _ID.match(v):
        raise HTTPException(422, "Un id de Discord son solo números (17 a 20): actívalo en Ajustes > Avanzado > Modo desarrollador y copia el id con clic derecho.")
    return v


def parse(body: object, current: dict) -> dict:
    """The new settings from a request body, or HTTPException(422) with a sentence that never repeats what was sent."""
    if not isinstance(body, dict):
        raise HTTPException(422, "El cuerpo debe ser un objeto JSON.")
    new = dict(current)
    new["channel_id"] = _ids({"channel_id": body.get("channel_id", current.get("channel_id"))}, "channel_id", True)
    new["status_channel_id"] = _ids({"v": body.get("status_channel_id", current.get("status_channel_id"))}, "v", False)
    new["role_id"] = _ids({"v": body.get("role_id", current.get("role_id"))}, "v", False)
    rem = body.get("reminders", current.get("reminders", DEFAULT_REMINDERS))
    if (not isinstance(rem, list) or len(rem) > 5 or not all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 1440 for m in rem)):
        raise HTTPException(422, "Los recordatorios son hasta 5 valores de 1 a 1440 minutos antes del evento.")
    new["reminders"] = sorted(set(rem), reverse=True)
    for flag in ("rsvp_enabled", "announce_enabled", "status_enabled"):
        v = body.get(flag, current.get(flag, True))
        if not isinstance(v, bool):
            raise HTTPException(422, "Los interruptores son verdadero o falso.")
        new[flag] = v
    return new


def _apply_to_schedules(old: dict, new: dict, bot_changed: bool) -> None:
    """Events that have not started follow the new settings: reminders rewritten (keeping which were already sent), sign-up messages dropped when the bot or the channel changed."""
    now = time.time()
    with Session(engine) as sess:
        for sc in sess.exec(select(Schedule).where(Schedule.state == "pending", Schedule.start_at > now)).all():
            if old.get("reminders", DEFAULT_REMINDERS) != new["reminders"]:
                sc.reminders = list(new["reminders"])
                sc.sent = [m for m in sc.sent if m in new["reminders"]]
            if bot_changed and sc.rsvp_message:
                sc.rsvp_message, sc.rsvp_text = None, ""
            sess.add(sc)
        sess.commit()


@router.put("")
async def put(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "El cuerpo debe ser JSON.") from None
    token_in = body.get("bot_token") if isinstance(body, dict) else None
    if token_in is not None and not (isinstance(token_in, str) and _TOKEN.match(token_in.strip())):
        raise HTTPException(422, "Ese no parece un token de bot de Discord (lo copias del portal de desarrolladores, en Bot).")

    def work() -> dict:
        with Session(engine) as sess:
            current = _row(sess)
        new = parse(body, current)
        token = token_in.strip() if token_in else (_read_token(current.get("token_rev", 0)) if current else None)
        if not token:
            raise HTTPException(422, "Falta el token del bot.")
        changed = bool(token_in) or any(new.get(k) != current.get(k) for k in ("channel_id", "status_channel_id"))
        if changed:   # asked of Discord BEFORE anything is stored and with no lock held: a slow Discord never blocks another change
            new["bot_name"] = _discord(token, "/users/@me").get("username") or "bot"
            new["channel_name"] = _discord(token, f"/channels/{new['channel_id']}").get("name")
            new["status_channel_name"] = _discord(token, f"/channels/{new['status_channel_id']}").get("name") if new["status_channel_id"] else None
        with _write:
            with Session(engine) as sess:
                latest = _row(sess)
            if latest.get("rev", 0) != current.get("rev", 0):   # someone saved while Discord was being asked: do not write over it with a version built from an older one
                raise HTTPException(409, "Otro cambio se guardó mientras tanto: recarga la página y vuelve a intentarlo.")
            new["rev"] = current.get("rev", 0) + 1
            new["token_rev"] = current.get("token_rev", 0)
            new["bot_rev"] = current.get("bot_rev", 0)
            bot_changed = bool(token_in) or new["channel_id"] != current.get("channel_id")
            if bot_changed:
                new["bot_rev"] += 1
            if token_in:
                new["token_rev"] += 1
                new["token_last4"] = token[-4:]
                _write_token(new["token_rev"], token)   # first: nothing reads it until the row below names this version
            new["last_error"] = None if changed else current.get("last_error")
            try:
                with Session(engine) as sess:
                    _save(sess, new)
                    sess.commit()
            except Exception:
                if token_in:
                    _token_path(new["token_rev"]).unlink(missing_ok=True)   # the save failed: the old bot (row and token) keeps working
                raise
            _forget_tokens(keep=new["token_rev"])
            _apply_to_schedules(current, new, bot_changed)
        return _view(new)
    return await run_in_threadpool(work)


@router.delete("", status_code=204)
def delete() -> None:
    with _write:
        with Session(engine) as sess:
            s = sess.get(Setting, KEY)
            if s is not None:
                sess.delete(s)
                sess.commit()
        _forget_tokens()


@router.post("/test")
def test() -> dict:
    global _last_test
    from app import discord
    cfg = active()
    if cfg is None:   # (a refusal for want of a saved bot does not use up the window: the next try, right after saving, must work)
        raise HTTPException(409, "Primero guarda el token y el canal.")
    with _test_lock:   # check and mark in one step: simultaneous clicks cannot both pass
        if time.monotonic() - _last_test < TEST_EVERY:
            raise HTTPException(429, "Espera unos segundos antes de otra prueba.")
        _last_test = time.monotonic()
    sent = []
    for ch in dict.fromkeys([cfg["channel_id"], cfg["status_channel_id"]]):
        try:
            discord._api("POST", f"/channels/{ch}/messages", {"content": "✅ Conexión correcta: este canal recibirá los anuncios del servidor."}, bearer=f"Bot {cfg['token']}")
            sent.append(ch)
        except urllib.error.HTTPError as e:
            raise HTTPException(422, "Discord no dejó publicar en el canal: " + {401: "el token ya no vale.", 403: "al bot le falta el permiso de enviar mensajes en él.",
                                                                                   404: "el canal ya no existe."}.get(e.code, f"error {e.code}.")) from None
        except Exception:
            raise HTTPException(502, "No se pudo contactar a Discord. Inténtalo de nuevo.") from None
    note(None)
    return {"ok": True, "channels": sent}
