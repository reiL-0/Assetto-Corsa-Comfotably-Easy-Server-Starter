"""Content integrity: what acServer checks on every driver, a sealed baseline of it, a gate before a server starts, and a
watch over the checksum failures it reports.

acServer itself, when a driver joins, asks the client for the MD5 of a few files and compares them with its own copies;
a mismatch gets the driver kicked (the server log shows «CHECKSUM: …», «Sending N checksum requests», «Car checksum <car> true»):
- `system/data/surfaces.ini`;
- the track's `data/surfaces.ini` (of the layout, when there is one), its `models.ini` (`models_<layout>.ini`) and `data/drs_zones.ini`;
- each car's `data.acd` (the physics: power, grip, weight…).
It does NOT check models (kn5), skins, apps, Custom Shaders Patch or any other plugin, and the server has no setting to add files:
those cannot be enforced from here. So the reference is whatever sits in the server's `content/`: if that copy is altered
(a wrong upload, a tampered file) every honest driver would be kicked and a cheat that matches the altered copy would pass.

This module keeps the reference honest:
- **Seal** (`ContentSeal`): an admin approves the current files (MD5, the same ones acServer logs). `check` compares the files on disk
  with the seal: `ok`, `changed`, `missing` or `unsealed`. Optional **extras** (any file or folder under the server directory, e.g.
  a server-side plugin) can be sealed too and are included when the server's `integrity_extras` is on.
- **Gate** (`gate`, called by `servers.start_server`): per server `integrity` = `off`; `warn` (default: a changed or missing file is
  reported to Discord, the server starts); `require` (the server does not start unless everything is sealed and unchanged).
- **Watch** (`on_log_line`, fed by the supervisor with every server log line): a checksum failure reported by acServer is recorded
  (`checksum_fail`) and announced on the status channel.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import discord, metrics
from app.auth import CurrentUser
from app.config import settings
from app.db import SessionDep, engine
from app.models import Activity, ContentSeal, Server

log = logging.getLogger("acmanager.integrity")
router = APIRouter(prefix="/integrity", tags=["integrity"])
MODES = ("off", "warn", "require")


def root() -> Path:
    """The directory that holds `content/` and `system/`: the acServer's own, or the data dir when no binary is configured."""
    return settings.acserver_dir() or Path(settings.data_dir)


def _md5(p: Path) -> str | None:
    return hashlib.md5(p.read_bytes()).hexdigest() if p.is_file() else None


def _folder_md5(p: Path) -> str | None:
    """One hash for a folder: of every file's relative path and content, in order."""
    if not p.is_dir():
        return None
    h = hashlib.md5()
    for f in sorted(x for x in p.rglob("*") if x.is_file()):
        h.update(str(f.relative_to(p)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _rel(p: Path) -> str:
    return str(p.relative_to(root()))


def car_files(car: str) -> dict[str, str | None]:
    d = root() / "content" / "cars" / car
    return {_rel(d / "data.acd"): _md5(d / "data.acd")} if (d / "data.acd").is_file() or not (d / "data").is_dir() else {_rel(d / "data"): _folder_md5(d / "data")}


def track_files(track: str, config: str) -> dict[str, str | None]:
    d = root() / "content" / "tracks" / track
    data = d / config / "data" if config else d / "data"
    models = d / f"models_{config}.ini" if config and (d / f"models_{config}.ini").is_file() else d / "models.ini"
    out = {_rel(data / "surfaces.ini"): _md5(data / "surfaces.ini"), _rel(models): _md5(models)}
    if (data / "drs_zones.ini").is_file():
        out[_rel(data / "drs_zones.ini")] = _md5(data / "drs_zones.ini")
    return out


def system_files() -> dict[str, str | None]:
    p = root() / "system" / "data" / "surfaces.ini"
    return {_rel(p): _md5(p)}


def extra_files(path: str) -> dict[str, str | None]:
    p = (root() / path).resolve()
    if root().resolve() not in p.parents or ".." in Path(path).parts:
        raise HTTPException(400, "an extra must be a file or folder inside the server directory")
    return {path: _md5(p) if p.is_file() else _folder_md5(p)}


def items_for(cars: list[str], track: str, config: str, extras: list[str] = ()) -> dict[str, dict]:
    """What would be verified: {key: {label, files}}."""
    out = {"system": {"label": "Sistema (surfaces.ini)", "files": system_files()}}
    if track:
        out[f"track:{track}:{config}"] = {"label": f"Pista {track}" + (f" / {config}" if config else ""), "files": track_files(track, config)}
    for car in cars:
        out[f"car:{car}"] = {"label": f"Auto {car}", "files": car_files(car)}
    for path in extras:
        out[f"extra:{path}"] = {"label": f"Extra {path}", "files": extra_files(path)}
    return out


def server_items(s: Server, sess: Session, with_extras: bool) -> dict[str, dict]:
    srv = s.config.get("SERVER", {})
    cars = [c for c in str(srv.get("CARS", "")).split(";") if c]
    extras = [k.split(":", 1)[1] for k in _seals(sess) if k.startswith("extra:")] if with_extras else []
    return items_for(cars, srv.get("TRACK", ""), srv.get("CONFIG_TRACK") or "", extras)


def _seals(sess: Session) -> dict[str, ContentSeal]:
    return {x.key: x for x in sess.exec(select(ContentSeal)).all()}


def check(items: dict[str, dict], seals: dict[str, ContentSeal]) -> list[dict]:
    """Each item against its seal. status: ok | changed | missing | unsealed."""
    out = []
    for key, it in items.items():
        seal = seals.get(key)
        changed = sorted(f for f, h in it["files"].items() if seal and seal.files.get(f) != h)
        missing = sorted(f for f, h in it["files"].items() if h is None)
        status = "missing" if missing else "unsealed" if not seal else "changed" if changed else "ok"
        out.append({"key": key, "label": it["label"], "status": status, "files": it["files"], "changed": changed, "missing": missing,
                    "sealed_at": seal.sealed_at.isoformat() if seal else None})
    return out


def problems(results: list[dict], mode: str) -> list[str]:
    """What stops («require») or is reported («warn»): changed and missing always; unsealed only when sealing is required."""
    bad = {"changed", "missing"} | ({"unsealed"} if mode == "require" else set())
    return [f"{r['label']}: {r['status']}" + (f" ({', '.join(r['changed'] or r['missing'])})" if r["status"] in ("changed", "missing") else "") for r in results if r["status"] in bad]


def gate(sess: Session, s: Server) -> None:
    """Called right before a server starts. Raises 409 in `require` mode; reports in `warn` mode."""
    if s.integrity == "off":
        return
    found = problems(check(server_items(s, sess, s.integrity_extras), _seals(sess)), s.integrity)
    if not found:
        return
    if s.integrity == "require":
        raise HTTPException(409, "contenido sin verificar, el servidor no arranca: " + "; ".join(found))
    discord.alert(f"⚠️ **{s.name}** arranca con contenido distinto al sellado: " + "; ".join(found))


# --- the watch over acServer's own checksum verdicts ---------------------------------------------------------------

_FAIL = re.compile(r"checksum.*(fail|mismatch|kick|wrong|invalid|error)|(fail|mismatch|kick).*checksum|Car checksum\s+\S+\s+false", re.I)


def is_failure(line: str) -> bool:
    return bool(_FAIL.search(line))


def on_log_line(server_id: int, line: str) -> None:
    if not is_failure(line):
        return
    metrics.log(server_id, "checksum_fail", name=line.strip()[:300])
    with Session(engine) as sess:
        srv = sess.get(Server, server_id)
    discord.alert(f"🚨 **{srv.name if srv else f'Servidor #{server_id}'}**: fallo de checksum, posible contenido modificado — `{line.strip()[:200]}`")


# --- API -----------------------------------------------------------------------------------------------------------

class SealIn(BaseModel):
    cars: list[str] = []
    track: str = ""
    config: str = ""
    extras: list[str] = Field(default_factory=list, max_length=50)
    server_id: int | None = None   # alternatively, seal what this server is set up with


class ModeIn(BaseModel):
    mode: Literal["off", "warn", "require"]
    extras: bool = False


@router.get("/check")
def get_check(sess: SessionDep, server_id: int) -> dict:
    s = sess.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    results = check(server_items(s, sess, True), _seals(sess))
    return {"mode": s.integrity, "extras": s.integrity_extras, "results": results, "problems": problems(results, s.integrity)}


@router.post("/seal")
def seal(body: SealIn, sess: SessionDep, user: CurrentUser) -> dict:
    """Approve the current files of these cars / track / extras (or of what a server is set up with) as the reference."""
    if body.server_id is not None:
        s = sess.get(Server, body.server_id)
        if not s:
            raise HTTPException(404, "server not found")
        items = server_items(s, sess, False)
        items.update({f"extra:{p}": {"label": f"Extra {p}", "files": extra_files(p)} for p in body.extras})
    else:
        items = items_for(body.cars, body.track, body.config, body.extras)
    for key, it in items.items():
        if any(h is None for h in it["files"].values()):
            raise HTTPException(400, f"{it['label']}: files missing on the server ({', '.join(f for f, h in it['files'].items() if h is None)})")
    seals = _seals(sess)
    for key, it in items.items():
        row = seals.get(key) or ContentSeal(key=key, files={}, sealed_at=datetime.now(UTC), sealed_by="")
        row.files, row.sealed_at, row.sealed_by = it["files"], datetime.now(UTC), user.username
        sess.add(row)
    sess.commit()
    return {"sealed": sorted(items)}


@router.delete("/seal/{key:path}", status_code=204)
def unseal(key: str, sess: SessionDep) -> None:
    row = sess.get(ContentSeal, key)
    if not row:
        raise HTTPException(404, "seal not found")
    sess.delete(row)
    sess.commit()


@router.get("/seals")
def list_seals(sess: SessionDep) -> list[dict]:
    return [{"key": x.key, "files": x.files, "sealed_at": x.sealed_at.isoformat(), "sealed_by": x.sealed_by} for x in _seals(sess).values()]


@router.put("/servers/{server_id}")
def set_mode(server_id: int, body: ModeIn, sess: SessionDep) -> dict:
    s = sess.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    s.integrity, s.integrity_extras = body.mode, body.extras
    sess.add(s)
    sess.commit()
    return {"mode": s.integrity, "extras": s.integrity_extras}


@router.get("/failures")
def failures(sess: SessionDep, days: int = 14) -> list[dict]:
    import time
    rows = sess.exec(select(Activity).where(Activity.kind == "checksum_fail", Activity.ts > time.time() - days * 86400).order_by(Activity.ts.desc()).limit(200)).all()
    return [{"ts": r.ts, "server_id": r.server_id, "line": r.name} for r in rows]
