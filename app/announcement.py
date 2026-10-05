"""The sign-up announcement of a scheduled event, as a Discord message payload an admin can edit.

The template is JSON in the shape Discord takes (`{"content": "...", "embeds": [...]}`); every string in it may use `{variables}`
(see `VARS`), filled in from the event's saved session and the schedule. It lives in `Setting` key `announcement`; with none
saved, `DEFAULT` is used. `schedule._rsvp` renders it for each pending schedule and edits the message when the result changes
(a new template reaches the messages already posted on the next tick).
"""

from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app import content
from app.config import settings
from app.db import SessionDep
from app.models import Setting

router = APIRouter(prefix="/announcement", tags=["announcement"])
KEY = "announcement"
MAX_JSON = 6000   # Discord: 6000 characters across all embeds, 2000 in content
RSVP = {"✅": "yes", "❔": "maybe", "❌": "no"}   # reaction -> status; this order is the priority when someone has several
LINE = "━━━━━━━━━━━━━━━━━━"

# name -> what it holds. The blocks (horario, formato, reacciones, notas) come with their own heading and are empty when there is nothing to say.
VARS = {
    "title": "Título del evento", "server": "Nombre del servidor", "track": "Circuito (nombre del ui_track.json)", "cars": "Autos, separados por coma",
    "role": "Mención del rol (ACM_DISCORD_ROLE), vacío si no hay", "role_line": "«🚨 ATENCIÓN @rol 🚨», vacío si no hay rol",
    "start": "Inicio de la sesión, fecha completa (cada lector la ve en su zona)", "start_rel": "«en 2 horas»", "start_time": "Solo la hora",
    "practice_min": "Minutos de práctica", "qualify_min": "Minutos de clasificación", "race": "«15 vueltas» o «60 min»",
    "horario": "Bloque HORARIO", "formato": "Bloque FORMATO: cada sesión con su hora", "reacciones": "Invitación a reaccionar y los contadores",
    "counts": "Solo los contadores «✅ 3 · ❔ 1 · ❌ 0»", "yes": "Cuántos van", "maybe": "Cuántos indecisos", "no": "Cuántos no pueden",
    "notas": "Bloque NOTAS (las notas y el info del calendario), vacío si no hay", "notes": "Las notas tal cual", "info": "El info del calendario tal cual",
}
DEFAULT = {"content": "🏁 **{title}** | {server}\n{role_line}\n\n" + LINE + "\n\n📍 Circuito: {track}\n🏎️ Auto: {cars}\n\n{horario}\n\n{formato}\n\n"
                      + LINE + "\n\n{reacciones}\n\n{notas}"}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _track_name(track: str, config: str) -> str:
    ui = content._tracks_dir() / track / "ui"
    d = content._read_json((ui / config if config else ui) / "ui_track.json")
    return d.get("name") or (f"{track} ({config})" if config else track)


def _car_names(session: dict) -> str:
    cars = session.get("cars") or sorted({e["model"] for e in session.get("entries", [])})
    return ", ".join(content._read_json(content._cars_dir() / c / "ui" / "ui_car.json").get("name") or c for c in cars)


def variables(title: str, server: str, session: dict, start_at: float, counts: dict[str, int], notes: str = "", info: str = "") -> dict[str, str]:
    """Every `{name}` of `VARS` for one event. `start_at` is when the practice opens; qualifying and race follow one after the other."""
    at = int(start_at)
    fmt = []
    if session.get("practice_min"):
        fmt.append(f"🟢 Práctica: {session['practice_min']} min · <t:{at}:t>")
        at += session["practice_min"] * 60
    if session.get("qualify_min"):
        fmt.append(f"⏱️ Clasificación: {session['qualify_min']} min · <t:{at}:t>")
        at += session["qualify_min"] * 60
    race = _plural(session["race_laps"], "vuelta", "vueltas") if session.get("race_laps") else f"{session['race_min']} min" if session.get("race_min") else ""
    if race:
        fmt.append(f"🏁 Carrera: {race} · <t:{at}:t>")
    if rg := session.get("reversed_grid"):
        fmt.append("🔄 Parrilla invertida" + (" (toda)" if rg == -1 else f" (los primeros {rg})"))
    c = " · ".join(f"{e} {counts[st]}" for e, st in RSVP.items())
    role = f"<@&{settings.discord_role}>" if settings.discord_role else ""
    extra = "\n".join(x for x in (notes.strip(), info.strip()) if x)
    return {
        "title": title, "server": server, "track": _track_name(session.get("track", "?"), session.get("track_config", "")), "cars": _car_names(session),
        "role": role, "role_line": f"🚨 ATENCIÓN {role} 🚨" if role else "",
        "start": f"<t:{int(start_at)}:F>", "start_rel": f"<t:{int(start_at)}:R>", "start_time": f"<t:{int(start_at)}:t>",
        "practice_min": str(session.get("practice_min") or 0), "qualify_min": str(session.get("qualify_min") or 0), "race": race,
        "horario": f"⏰ **HORARIO**\n<t:{int(start_at)}:F> (<t:{int(start_at)}:R>)", "formato": ("🏁 **FORMATO**\n" + "\n".join(fmt)) if fmt else "",
        "reacciones": f"Reacciona para inscribirte: ✅ voy · ❔ indeciso · ❌ no puedo\n{c}", "counts": c,
        "yes": str(counts["yes"]), "maybe": str(counts["maybe"]), "no": str(counts["no"]),
        "notas": f"📋 **NOTAS**\n{extra}" if extra else "", "notes": notes.strip(), "info": info.strip(),
    }


def _fill(node, v: dict[str, str]):
    if isinstance(node, str):
        return re.sub(r"\{(\w+)\}", lambda m: v.get(m[1], m[0]), node)   # an unknown {name} stays as typed
    if isinstance(node, list):
        return [_fill(x, v) for x in node]
    if isinstance(node, dict):
        return {k: _fill(x, v) for k, x in node.items()}
    return node


def render(template: dict, v: dict[str, str]) -> dict:
    """The payload to send: the template with its variables filled in, empty blocks' blank lines collapsed, `content` cut at Discord's
    2000 characters (the end goes first, so keep the notes last), and mentions limited to roles (no @everyone)."""
    out = _fill(template, v)
    if "content" in out:
        out["content"] = re.sub(r"\n{3,}", "\n\n", out["content"]).strip()[:2000]
    out["allowed_mentions"] = {"parse": ["roles"]}
    return out


def validate(template: object) -> dict:
    """Raises HTTPException(422) with a sentence unless `template` is a payload we can send."""
    if not isinstance(template, dict) or set(template) - {"content", "embeds"}:
        raise HTTPException(422, 'el mensaje es un objeto JSON con solo "content" y/o "embeds"')
    if not (isinstance(template.get("content", ""), str) and isinstance(template.get("embeds", []), list)):
        raise HTTPException(422, '"content" es texto y "embeds" una lista')
    if not template.get("content", "").strip() and not template.get("embeds"):
        raise HTTPException(422, "el mensaje está vacío")
    if len(json.dumps(template, ensure_ascii=False)) > MAX_JSON or len(template.get("embeds", [])) > 10:
        raise HTTPException(422, f"demasiado largo (máximo {MAX_JSON} caracteres y 10 embeds)")
    return template


def current(sess: Session) -> dict:
    row = sess.get(Setting, KEY)
    return row.value if row and row.value else DEFAULT


SAMPLE = variables("Fun Race | Spa PetitChamps", "Servidor 1", {"track": "spa", "cars": ["clio"], "practice_min": 15, "qualify_min": 15, "race_laps": 15},
                   time.time() + 7200, {"yes": 3, "maybe": 1, "no": 0}, "Descarga: https://ejemplo.com")


class TemplateIn(BaseModel):
    template: dict


@router.get("")
def get(sess: SessionDep) -> dict:
    """The template in use, the default and the variables you can write as `{name}`."""
    return {"template": current(sess), "default": DEFAULT, "variables": VARS}


@router.put("")
def put(body: TemplateIn, sess: SessionDep) -> dict:
    sess.merge(Setting(key=KEY, value=validate(body.template)))
    sess.commit()
    return {"template": body.template}


@router.delete("", status_code=204)
def reset(sess: SessionDep) -> None:
    if row := sess.get(Setting, KEY):
        sess.delete(row)
        sess.commit()


@router.post("/preview")
def preview(body: TemplateIn) -> dict:
    """What the template would send, with sample data (nothing is posted)."""
    return render(validate(body.template), SAMPLE)
