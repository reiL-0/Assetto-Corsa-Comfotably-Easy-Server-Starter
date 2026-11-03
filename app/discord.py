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
import urllib.error
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
                                     headers={"Content-Type": "application/json", "User-Agent": settings.agent()})
        urllib.request.urlopen(req, timeout=8).close()
    except Exception:
        log.exception("discord status post failed")


def bot() -> dict | None:
    """The client's own bot (app/discord_config.py: set up in the admin panel), or None: then the environment's settings (the league's webhooks and bot) are in charge, exactly as before."""
    from app import discord_config
    return discord_config.active()


def _post_as_bot(cfg: dict, channel: str, text: str) -> None:
    """One message as the client's bot. A failure is logged WITHOUT the token and remembered for the panel (a short sentence); a success clears it."""
    from app import discord_config
    try:
        _api("POST", f"/channels/{channel}/messages", {"content": text[:2000]}, bearer=f"Bot {cfg['token']}")
        discord_config.note(None)
    except urllib.error.HTTPError as e:
        log.warning("discord bot post failed (%s)", e.code)
        discord_config.note({401: "El token del bot ya no vale: pega uno nuevo.", 403: "Al bot le falta permiso para publicar en el canal.", 404: "El canal ya no existe.",
                             429: "Discord pide esperar (demasiados mensajes)."}.get(e.code, f"Discord respondió con un error ({e.code})."))
    except Exception:
        log.warning("discord bot post failed (no answer)")
        discord_config.note("No se pudo contactar a Discord.")


def rsvp_enabled() -> bool:
    cfg = bot()
    return cfg["rsvp_enabled"] if cfg else bool(settings.discord_bot_token and settings.discord_channel)


def role_id() -> str:
    cfg = bot()
    return cfg["role_id"] if cfg else settings.discord_role


def bot_rev() -> int:
    """Which bot-and-channel is in use (it changes when the token or the channel does): schedule._rsvp compares it before and after a post."""
    cfg = bot()
    return cfg["bot_rev"] if cfg else 0


def _creds() -> tuple[str | None, str]:
    """(Authorization value, channel) read ONCE: an operation that takes several calls (a post and its three reactions) keeps one bot and one channel from start to end."""
    cfg = bot()
    return (f"Bot {cfg['token']}", cfg["channel_id"]) if cfg else (None, settings.discord_channel)


def on_event(server_id: int, kind: str, reason: str | None, value: float | None) -> None:
    cfg = bot()
    if kind not in KINDS or not (cfg["status_enabled"] if cfg else settings.discord_status_webhook):
        return
    with Session(engine) as s:
        srv = s.get(Server, server_id)
    text = message(srv.name if srv else f"Servidor #{server_id}", kind, reason, value)
    if cfg:
        threading.Thread(target=_post_as_bot, args=(cfg, cfg["status_channel_id"], text), daemon=True).start()
    else:
        threading.Thread(target=_send, args=(text,), daemon=True).start()


SESSIONS = {"Race": "Carrera", "Qualify": "Clasificación", "Practice": "Práctica"}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def effect(kind: str, value: int) -> str:
    """What a penalty does, in words. `value` as the API hands it out (seconds for `time`)."""
    return {"time": f"+{value} s", "stopgo": f"stop & go de {value} s (se suma al tiempo de carrera)", "position": f"pierde {_plural(value, 'posición', 'posiciones')}",
            "dsq": "descalificado", "grid": f"pierde {_plural(value, 'puesto', 'puestos')} en la parrilla de la próxima carrera",
            "points": f"-{value} puntos de campeonato"}[kind]


def penalty_message(server: str, parsed: dict, driver: str, kind: str, value: int, reason: str | None = None) -> str:
    """Stewards' decision (with `reason`) or its withdrawal (`reason` None) for the league channel."""
    where = f"{server} · {SESSIONS.get(parsed.get('type'), parsed.get('type') or 'Sesión')} en {parsed.get('track') or '?'}"
    if reason is None:
        return f"↩️ Sanción retirada a **{driver}** ({effect(kind, value)}) · {where}"
    return f"⚖️ **Sanción** · {where}\n👤 **{driver}**: {effect(kind, value)}\n📝 {reason}"


def announce(text: str) -> None:
    """A league announcement (scheduled-start reminders): through the client's own bot when it has one (and its switch is on), else to `ACM_DISCORD_WEBHOOK`; nothing if neither is set."""
    cfg = bot()
    if cfg:
        if cfg["announce_enabled"]:
            threading.Thread(target=_post_as_bot, args=(cfg, cfg["channel_id"], text), daemon=True).start()
        return
    if settings.discord_webhook:
        threading.Thread(target=_send, args=(text, settings.discord_webhook), daemon=True).start()


def alert(text: str) -> None:
    """A warning for the stewards on the server-status channel; nothing if it is not set."""
    cfg = bot()
    if cfg:
        if cfg["status_enabled"]:
            threading.Thread(target=_post_as_bot, args=(cfg, cfg["status_channel_id"], text), daemon=True).start()
        return
    if settings.discord_status_webhook:
        threading.Thread(target=_send, args=(text,), daemon=True).start()


# --- RSVP announcements: the bot posts, people react, schedule._rsvp reads the reactions ---

API = "https://discord.com/api/v10"
RSVP = {"✅": "yes", "❔": "maybe", "❌": "no"}  # reaction -> status; this order is the priority when someone has several


def _api(method: str, path: str, body: dict | None = None, *, bearer: str | None = None, form: dict | None = None):
    data = urllib.parse.urlencode(form).encode() if form else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, method=method, data=data, headers={
        "Authorization": bearer or f"Bot {(bot() or {}).get('token') or settings.discord_bot_token}", "User-Agent": settings.agent(),
        "Content-Type": "application/x-www-form-urlencoded" if form else "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def rsvp_post(payload: dict) -> str:
    """Post the announcement (a Discord message payload, see app/announcement.py) and put the three reactions on it so people only have to click. Returns the message id (blocking: run in a thread)."""
    auth, channel = _creds()
    path = f"/channels/{channel}/messages"
    mid = _api("POST", path, payload, bearer=auth)["id"]
    try:
        for emoji in RSVP:
            _api("PUT", f"{path}/{mid}/reactions/{urllib.parse.quote(emoji)}/@me", bearer=auth)
            time.sleep(0.3)   # Discord rate-limits adding reactions
    except Exception:
        log.exception("discord reactions failed")   # the message exists: do not post it twice
    return mid


def rsvp_edit(mid: str, payload: dict) -> None:
    auth, channel = _creds()
    _api("PATCH", f"/channels/{channel}/messages/{mid}", payload, bearer=auth)


MAX_REACTION_PAGES = 20


def rsvp_read(mid: str) -> dict[str, list[str]]:
    """status -> Discord ids that reacted with its emoji (the bot excluded). One call for the message (it carries the counts) and one per
    emoji somebody besides the bot used: the reaction-list route is rate-limited hard (429), so an idle announcement costs a single call.
    More than 100 reactors of one emoji are read page by page with `after`, up to MAX_REACTION_PAGES pages (2,000 people)."""
    auth, channel = _creds()
    path = f"/channels/{channel}/messages/{mid}"
    used = {r["emoji"]["name"] for r in _api("GET", path, bearer=auth).get("reactions", []) if r["count"] - r["me"] > 0}   # `me`: the bot's own reaction
    out = {}
    for e, st in RSVP.items():
        out[st] = []
        if e in used:
            after = ""
            for _ in range(MAX_REACTION_PAGES):
                page = _api("GET", f"{path}/reactions/{urllib.parse.quote(e)}?limit=100{after}", bearer=auth) or []
                out[st] += [u["id"] for u in page if not u.get("bot")]
                time.sleep(0.5)
                if len(page) < 100:
                    break
                after = f"&after={page[-1]['id']}"   # Discord lists reactors by ascending id: the next page starts after the last one seen
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
