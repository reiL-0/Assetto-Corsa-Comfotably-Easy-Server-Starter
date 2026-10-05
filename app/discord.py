"""Server status posts (started / stopped / crashed) to a Discord webhook.

`announce` is the second channel (league announcements). `metrics.log` calls `on_event` for every activity row; only the three server-lifecycle kinds are posted, and only when
`ACM_DISCORD_STATUS_WEBHOOK` is set. The post runs on a daemon thread so a slow Discord never delays a start or a stop,
and a failure is logged and dropped.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.auth import CurrentUser
from app.config import settings
from app.db import SessionDep, engine
from app.models import Rsvp, Server, User

log = logging.getLogger("acmanager.discord")
KINDS = ("server_start", "server_stop", "server_crash")
STOP_REASONS = {"manual": "detenido", "idle": "detenido por inactividad (sin pilotos)", "event_end": "detenido al terminar el evento"}


def _span(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def message(name: str, kind: str, reason: str | None, value: float | None) -> str:
    """The Discord text for one lifecycle event. `reason`/`value` are the activity row's `name`/`value`."""
    if kind == "server_start":
        return f"🟢 **{name}** iniciado"
    if kind == "server_stop":
        return f"🔴 **{name}** {STOP_REASONS.get(reason or '', 'detenido')}" + (f" · estuvo {_span(value)} en marcha" if value else "")
    return f"💥 **{name}** se cayó (código {int(value) if value is not None else '?'}, {reason or ''})".replace(", )", ")")


def _send(text: str, url: str | None = None) -> None:
    try:
        req = urllib.request.Request(url or settings.discord_status_webhook, data=json.dumps({"content": text[:2000]}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "OPR-AC-Manager"})
        urllib.request.urlopen(req, timeout=8).close()
    except Exception:
        log.exception("discord status post failed")


def on_event(server_id: int, kind: str, reason: str | None, value: float | None) -> None:
    if kind not in KINDS or not settings.discord_status_webhook:
        return
    with Session(engine) as s:
        srv = s.get(Server, server_id)
    text = message(srv.name if srv else f"Servidor #{server_id}", kind, reason, value)
    threading.Thread(target=_send, args=(text,), daemon=True).start()


SESSIONS = {"Race": "Carrera", "Qualify": "Clasificación", "Practice": "Práctica"}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def effect(kind: str, value: int) -> str:
    """What a penalty does, in words. `value` as the API hands it out (seconds for `time`)."""
    return {"time": f"+{value} s", "position": f"pierde {_plural(value, 'posición', 'posiciones')}",
            "dsq": "descalificado", "grid": f"pierde {_plural(value, 'puesto', 'puestos')} en la parrilla de la próxima carrera",
            "points": f"-{value} puntos de campeonato"}[kind]


def penalty_message(server: str, parsed: dict, driver: str, kind: str, value: int, reason: str | None = None) -> str:
    """Stewards' decision (with `reason`) or its withdrawal (`reason` None) for the league channel."""
    where = f"{server} · {SESSIONS.get(parsed.get('type'), parsed.get('type') or 'Sesión')} en {parsed.get('track') or '?'}"
    if reason is None:
        return f"↩️ Sanción retirada a **{driver}** ({effect(kind, value)}) · {where}"
    return f"⚖️ **Sanción** · {where}\n👤 **{driver}**: {effect(kind, value)}\n📝 {reason}"


def announce(text: str) -> None:
    """A league announcement (scheduled-start reminders) to `ACM_DISCORD_WEBHOOK`; nothing if it is not set."""
    if settings.discord_webhook:
        threading.Thread(target=_send, args=(text, settings.discord_webhook), daemon=True).start()


def alert(text: str) -> None:
    """A warning for the stewards on the server-status channel; nothing if it is not set."""
    if settings.discord_status_webhook:
        threading.Thread(target=_send, args=(text,), daemon=True).start()


# --- RSVP announcements: the bot posts, people react, schedule._rsvp reads the reactions ---

API = "https://discord.com/api/v10"
UA = "OPR-AC-Manager"
RSVP = {"✅": "yes", "❔": "maybe", "❌": "no"}  # reaction -> status; this order is the priority when someone has several


def _api(method: str, path: str, body: dict | None = None, *, bearer: str | None = None, form: dict | None = None):
    data = urllib.parse.urlencode(form).encode() if form else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, method=method, data=data, headers={
        "Authorization": bearer or f"Bot {settings.discord_bot_token}", "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded" if form else "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def rsvp_post(payload: dict) -> str:
    """Post the announcement (a Discord message payload, see app/announcement.py) and put the three reactions on it so people only have to click. Returns the message id (blocking: run in a thread)."""
    path = f"/channels/{settings.discord_channel}/messages"
    mid = _api("POST", path, payload)["id"]
    try:
        for emoji in RSVP:
            _api("PUT", f"{path}/{mid}/reactions/{urllib.parse.quote(emoji)}/@me")
            time.sleep(0.3)   # Discord rate-limits adding reactions
    except Exception:
        log.exception("discord reactions failed")   # the message exists: do not post it twice
    return mid


def rsvp_edit(mid: str, payload: dict) -> None:
    _api("PATCH", f"/channels/{settings.discord_channel}/messages/{mid}", payload)


def rsvp_read(mid: str) -> dict[str, list[str]]:
    """status -> Discord ids that reacted with its emoji (the bot excluded). One call for the message (it carries the counts) and one per
    emoji somebody besides the bot used: the reaction-list route is rate-limited hard (429), so an idle announcement costs a single call.
    ponytail: first 100 per emoji, paginate with `after` if a league outgrows that."""
    path = f"/channels/{settings.discord_channel}/messages/{mid}"
    used = {r["emoji"]["name"] for r in _api("GET", path).get("reactions", []) if r["count"] - r["me"] > 0}   # `me`: the bot's own reaction
    out = {}
    for e, st in RSVP.items():
        out[st] = []
        if e in used:
            out[st] = [u["id"] for u in _api("GET", f"{path}/reactions/{urllib.parse.quote(e)}?limit=100") if not u.get("bot")]
            time.sleep(0.5)
    return out


# --- Linking a Discord account to the logged-in user (OAuth2, scope identify) ---

router = APIRouter(prefix="/auth/discord", tags=["auth"])
_states: dict[str, tuple[int, float]] = {}  # state -> (user id, expiry); ponytail: in memory, a manager restart mid-link just means trying again


def _redirect_uri() -> str:
    return f"{settings.public_url.rstrip('/')}/api/v1/auth/discord/callback"


@router.get("/link", include_in_schema=False)
def link(user: CurrentUser) -> RedirectResponse:
    """Open this in the browser while logged in: sends the user to Discord to approve, which returns to /callback."""
    if not (settings.discord_client_id and settings.discord_client_secret and settings.public_url):
        raise HTTPException(503, "discord linking is not configured")
    now = time.time()
    for k in [k for k, (_, exp) in _states.items() if exp < now]:
        del _states[k]
    state = secrets.token_urlsafe(16)
    _states[state] = (user.id, now + 600)
    q = urllib.parse.urlencode({"client_id": settings.discord_client_id, "redirect_uri": _redirect_uri(), "response_type": "code",
                                "scope": "identify", "state": state})
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{q}")


@router.get("/callback", include_in_schema=False)
def callback(code: str, state: str, sess: SessionDep) -> RedirectResponse:
    uid, exp = _states.pop(state, (0, 0))
    if not uid or exp < time.time():
        raise HTTPException(400, "link expired, start again")
    try:
        tok = _api("POST", "/oauth2/token", form={"client_id": settings.discord_client_id, "client_secret": settings.discord_client_secret,
                                                  "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri()})
        did = _api("GET", "/users/@me", bearer=f"Bearer {tok['access_token']}")["id"]
    except Exception:
        log.exception("discord link failed")
        raise HTTPException(502, "discord refused the link")
    if (other := sess.exec(select(User).where(User.discord_id == did)).first()) and other.id != uid:
        raise HTTPException(409, "that Discord account is already linked to another user")
    user = sess.get(User, uid)
    user.discord_id = did
    sess.add(user)
    for r in sess.exec(select(Rsvp).where(Rsvp.discord_id == did)):   # what they already answered becomes theirs
        r.user_id = uid
        sess.add(r)
    sess.commit()
    return RedirectResponse("/")


@router.delete("", status_code=204)
def unlink(user: CurrentUser, sess: SessionDep) -> None:
    user.discord_id = None
    sess.add(user)
    sess.commit()
