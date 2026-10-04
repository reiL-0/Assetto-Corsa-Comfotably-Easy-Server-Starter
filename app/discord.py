"""Server status posts (started / stopped / crashed) to a Discord webhook.

`announce` is the second channel (league announcements). `metrics.log` calls `on_event` for every activity row; only the three server-lifecycle kinds are posted, and only when
`ACM_DISCORD_STATUS_WEBHOOK` is set. The post runs on a daemon thread so a slow Discord never delays a start or a stop,
and a failure is logged and dropped.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request

from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.models import Server

log = logging.getLogger("acmanager.discord")
KINDS = ("server_start", "server_stop", "server_crash")
STOP_REASONS = {"manual": "detenido", "idle": "detenido por inactividad (sin pilotos)"}


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


def announce(text: str) -> None:
    """A league announcement (scheduled-start reminders) to `ACM_DISCORD_WEBHOOK`; nothing if it is not set."""
    if settings.discord_webhook:
        threading.Thread(target=_send, args=(text, settings.discord_webhook), daemon=True).start()
