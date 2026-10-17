"""Content indexer: cars/tracks under data/content, checksums, entry-list
building, and zip-based upload/download for content and skins."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import catalog, download, integrity, metrics, unpack, uploadguard
from app.auth import require
from app.config import settings

router = APIRouter(prefix="/content", tags=["content"])


def _safe(name: str) -> str:
    """A path segment, not a path: rejects traversal / separators."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, f"invalid name: {name!r}")
    return name


def _content_dir() -> Path:
    """The acServer's own `content/` when a binary is configured (what a server reads is what gets uploaded);
    otherwise a private library under the data dir."""
    ac = settings.acserver_dir()
    d = ac / "content" if ac else Path(settings.data_dir) / "content"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scratch() -> Path:
    """Work area for unpacking: on disk (Debian's /tmp is RAM) and on the same filesystem as the data."""
    d = Path(settings.data_dir) / "scratch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox_dir() -> Path:
    """Drop big archives here (scp/sftp) and import them by name: Cloudflare caps request bodies at 100 MB."""
    d = Path(settings.data_dir) / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setups_dir() -> Path:
    """Fixed setups, one `<name>.ini` each: acServer opens `setups/<FIXED_SETUP>` (the value WITH «.ini», checked against the real binary) from its working directory (every instance links this folder)."""
    ac = settings.acserver_dir()
    d = ac / "setups" if ac else Path(settings.data_dir) / "setups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cars_dir() -> Path:
    d = _content_dir() / "cars"
    d.mkdir(exist_ok=True)
    return d


def _tracks_dir() -> Path:
    d = _content_dir() / "tracks"
    d.mkdir(exist_ok=True)
    return d


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_cars() -> list[dict]:
    cars = []
    for d in sorted(_cars_dir().iterdir()):
        if not d.is_dir():
            continue
        ui = _read_json(d / "ui" / "ui_car.json")
        skins_dir = d / "skins"
        skins = sorted(p.name for p in skins_dir.iterdir() if p.is_dir()) if skins_dir.is_dir() else []
        cars.append(
            {
                "car": d.name,
                "usable": (d / "data.acd").is_file(),  # what acServer loads; folders without it are leftovers
                "name": ui.get("name"),
                "brand": ui.get("brand"),
                "class": ui.get("class"),
                "tags": ui.get("tags", []),
                "skins": skins,
            }
        )
    return cars


def list_tracks() -> list[dict]:
    tracks = []
    for d in sorted(_tracks_dir().iterdir()):
        if not d.is_dir():
            continue
        ui_dir = d / "ui"
        configs = []
        top = ui_dir / "ui_track.json"
        if top.exists():
            configs.append({"config": None, **_read_json(top)})
        if ui_dir.is_dir():
            for sub in sorted(ui_dir.iterdir()):
                layout_json = sub / "ui_track.json"
                if sub.is_dir() and layout_json.exists():
                    configs.append({"config": sub.name, **_read_json(layout_json)})
        # Server packs have no ui/: a layout is a folder with its own data/surfaces.ini (map-only folders are not).
        known = {c["config"] for c in configs}
        for sub in sorted(d.iterdir()):
            if sub.is_dir() and sub.name not in known and (sub / "data" / "surfaces.ini").is_file():
                configs.append({"config": sub.name})
        layouts = [c for c in configs if c["config"] and (d / c["config"] / "data" / "surfaces.ini").is_file()]
        base = (d / "data" / "surfaces.ini").is_file()  # the track itself (no layout) can be raced
        tracks.append({"track": d.name, "configs": configs, "base": base, "usable": base or bool(layouts)})
    return tracks


def _sha1(p: Path) -> str | None:
    if not p.is_file():
        return None
    return hashlib.sha1(p.read_bytes()).hexdigest()


def car_checksum(car: str) -> dict:
    return {"data_acd": _sha1(_cars_dir() / car / "data.acd")}


def track_checksum(track: str, config: str | None) -> dict:
    base = _tracks_dir() / track
    models_name = f"models_{config}.ini" if config else "models.ini"
    return {
        "surfaces_ini": _sha1(base / "data" / "surfaces.ini"),
        "models_ini": _sha1(base / models_name),
    }


class EntryListRow(BaseModel):
    car: str
    skin: str = ""


def build_entry_list(rows: list[EntryListRow]) -> list[dict]:
    out = []
    for row in rows:
        car, skin = _safe(row.car), (_safe(row.skin) if row.skin else "")
        car_dir = _cars_dir() / car
        if not car_dir.is_dir():
            raise HTTPException(404, f"car not found: {car}")
        if skin and not (car_dir / "skins" / skin).is_dir():
            raise HTTPException(404, f"skin not found: {car}/{skin}")
        out.append({"MODEL": car, "SKIN": skin})
    return out


def _zip_response(d: Path, filename: str) -> FileResponse:
    if not d.is_dir():
        raise HTTPException(404, "not found")
    tmp_dir = Path(tempfile.mkdtemp())
    tmp = tmp_dir / filename
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in d.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(d.parent))
    return FileResponse(
        tmp, filename=filename, background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True)
    )


class Installed(str):
    """The name of what was installed; `.kind` is where it really went («car»/«track», or "" for skins) and `.note` says so when that is not what was asked."""

    kind: str = ""
    note: str = ""


def _extract(archive: Path, dest_parent: Path, pack: bool = False, source_url: str = "", auto: bool = False) -> str:
    """Unpacks a .zip (one top-level folder = the content's name; no .rar, no loose files) into `dest_parent`. Returns that name.
    Extracts to a scratch dir first and only moves it in after checking every entry stayed inside it (and the size limits of app/uploadguard.py).
    `pack`: keep only what acServer reads (a car's `data`, a track's `surfaces.ini`/`models.ini`…), not the 3D models and textures. Cars and tracks are cut down always: `pack` is kept only so older clients that send it still work.
    `source_url`: the modder's page for the «Descargar» button (without it the content has no button).
    `settings.upload_permissive` (the league's own panel): any archive format, the car/track folder may sit anywhere inside and the kind is detected from it; `auto` = the uploader did not say what it is."""
    permissive = settings.upload_permissive and dest_parent in (_cars_dir(), _tracks_dir())
    with archive.open("rb") as fh:
        magic = fh.read(512)   # a plain .tar only shows «ustar» at byte 257
    scratch = Path(tempfile.mkdtemp(dir=_scratch()))
    try:
        fmt = unpack.archive_format(magic)
        if fmt == "zip":
            with zipfile.ZipFile(archive) as zf:
                uploadguard.check_zip(zf)   # cheap early refusal from the headers (names, count, sizes, ratios); the real limits are enforced while unpacking
        elif not (fmt and permissive):   # other formats are listed by the child, which counts what it really writes
            raise HTTPException(400, unpack.NOT_ARCHIVE if permissive else unpack.NOT_ZIP)
        try:
            unpack.run_sandboxed(archive, scratch, permissive=permissive)   # in a limited child process with a byte counter and a timeout (app/unpack.py)
        except OSError as e:
            raise HTTPException(507, str(e)) from e
        uploadguard.check_tree(scratch)   # last look at what really landed
        for f in scratch.rglob("*"):  # no symlinks, nothing outside the scratch dir
            if f.is_symlink() or scratch.resolve() not in f.resolve().parents:
                raise HTTPException(400, f"unsafe archive entry: {f.relative_to(scratch)}")
        asked = None if dest_parent not in (_cars_dir(), _tracks_dir()) or auto else "car" if dest_parent == _cars_dir() else "track"
        if permissive:
            top, found = uploadguard.find_content_root(scratch, asked)
        else:
            tops = [p for p in scratch.iterdir()]
            if len(tops) != 1 or not tops[0].is_dir():
                raise HTTPException(400, "the .zip must hold exactly one folder at its root (the car's or track's name) and nothing loose beside it")
            top, found = tops[0], uploadguard.detect_kind(tops[0])
        root = _safe(top.name)
        note = ""
        if dest_parent in (_cars_dir(), _tracks_dir()) and found and found != asked:   # a car sent as a track (or the other way round) goes where it belongs
            dest_parent = _cars_dir() if found == "car" else _tracks_dir()
            note = f"looked like a {found}, not a {asked}: installed as a {found}" if asked else ""
        full_digest = ""
        if dest_parent in (_cars_dir(), _tracks_dir()):   # cars and tracks are ALWAYS cut down to the server pack (a track keeps its map.png/map.ini for the live map)
            full_digest = catalog.digest_dir(top)[0]   # hash of the WHOLE upload: a rights claim blocked that one, and cutting the pack down must not let it back in
            catalog.check_not_blocked(full_digest)
            uploadguard.prune(top, "car" if dest_parent == _cars_dir() else "track")
        cataloged = dest_parent in (_cars_dir(), _tracks_dir())   # not skins: the checksums cover only physics and track files
        kind = "car" if dest_parent == _cars_dir() else "track"
        with catalog.mutation_lock:   # ponytail: held during copytree/digest (one process, runs in the threadpool); uploads and scans queue up. Staging dir + short swap under the lock if that hurts
            if cataloged:
                digest, size, nfiles = catalog.digest_dir(top)
                if full_digest:
                    catalog.check_not_blocked(full_digest)   # again under the lock: a claim may have landed while the upload was cut down or waited
                catalog.check_not_blocked(digest)   # removed after a rights claim: refused before anything is copied
            dest_parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(top, dest_parent / root, dirs_exist_ok=True)
            res = Installed(root)
            if cataloged:
                res.kind = kind
                integrity.seal_installed(kind, root)
                catalog.record_upload(kind, root, digest, size, nfiles, catalog.LEAGUE, "upload", source_url)
        res.note = note
        return res
    except uploadguard.Rejected as e:
        raise HTTPException(400, str(e)) from e
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _unzip_upload(file: UploadFile, dest_parent: Path, pack: bool = False, source_url: str = "", official: bool = False) -> str:
    """Streams an uploaded .zip to disk (tracks are hundreds of MB) and unpacks it."""
    with tempfile.NamedTemporaryFile(suffix=".upload", dir=_scratch(), delete=False) as tmp:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
    try:
        return await run_in_threadpool(_extract, Path(tmp.name), dest_parent, pack, catalog.check_source(source_url, official))
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# --- routes ------------------------------------------------------------------

@router.get("/cars")
def get_cars() -> list[dict]:
    return list_cars()


@router.get("/cars/{car}/checksum")
def get_car_checksum(car: str) -> dict:
    return car_checksum(_safe(car))


@router.get("/cars/{car}.zip")
def download_car(car: str) -> FileResponse:
    return _zip_response(_cars_dir() / _safe(car), f"{car}.zip")


def _installed(name: str, asked: str) -> dict:
    """The upload answer: {asked: name}, or {actual kind: name, "note": ...} when the content was installed as the other kind."""
    kind = getattr(name, "kind", "") or asked
    return {kind: str(name)} | ({"note": name.note} if getattr(name, "note", "") else {})


@router.post("/cars", status_code=201)
async def upload_car(file: UploadFile, pack: bool = False, source_url: str = "", source_official: bool = False) -> dict:
    return _installed(await _unzip_upload(file, _cars_dir(), pack, source_url, source_official), "car")


def _skin_folder(name: str) -> str:
    from app.schemas.servers import _folder_name
    try:
        return _folder_name(name) or _safe(name)
    except ValueError as e:
        raise HTTPException(400, f"invalid name: {name!r}") from e


def _find_skins(base: Path, car: str | None) -> list[tuple[str, Path]]:
    """[(car, skin folder)] inside an unpacked archive. The skins are the folders under a `skins` folder (the car is the one above it, or `car`),
    else the folders that hold a ui_skin.json, else the folders at the top. Ignores hidden and __MACOSX folders."""
    def dirs(d: Path) -> list[Path]:
        return sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith((".", "__MACOSX")))
    out, queue = [], [base]
    while queue:
        d = queue.pop(0)
        if d.name.lower() == "skins" and d != base:
            out += [(d.parent.name if d.parent != base else car, s) for s in dirs(d)]
            continue
        queue += dirs(d)
    if not out:
        found = [p for p in sorted(base.rglob("ui_skin.json")) if p.is_file() and p.parent != base   # one loose at the root has no folder to name the skin by
                 and not any(x.name.startswith((".", "__MACOSX")) for x in p.relative_to(base).parents)]
        tops = {p.parent for p in found if not any(q in p.parent.parents for q in {x.parent for x in found})}
        out = [(car, s) for s in sorted(tops)] or [(car, s) for s in dirs(base)]
    return out


def _install_skins(archive: Path, car: str | None) -> dict:
    """The server only needs a skin's folder name (it goes into the entry list) and its ui_skin.json: whatever the archive holds, nothing else is kept
    (textures live on the players' PCs, shared by link). Returns {car, skins: ["car/skin", ...]}."""
    permissive = settings.upload_permissive
    with archive.open("rb") as fh:
        fmt = unpack.archive_format(fh.read(512))
    if fmt != "zip" and not (fmt and permissive):
        raise HTTPException(400, unpack.NOT_ARCHIVE if permissive else unpack.NOT_ZIP)
    scratch = Path(tempfile.mkdtemp(dir=_scratch()))
    try:
        try:
            unpack.run_sandboxed(archive, scratch, permissive=permissive)
        except OSError as e:
            raise HTTPException(507, str(e)) from e
        uploadguard.check_tree(scratch)
        for f in scratch.rglob("*"):
            if f.is_symlink() or scratch.resolve() not in f.resolve().parents:
                raise HTTPException(400, f"unsafe archive entry: {f.relative_to(scratch)}")
        given = _skin_folder(car) if car else None
        found = _find_skins(scratch, given)
        if not found:
            raise HTTPException(400, "no skin folders found in the archive")
        plan, seen = [], set()
        for owner, src in found:
            owner = _skin_folder(owner) if owner else None
            named = owner
            if owner not in (None, given) and not (_cars_dir() / owner).is_dir():   # the folder above `skins` was not a car we have: the car the uploader named
                owner = given
            if not owner:
                raise HTTPException(400, (f"car not installed: {named}; " if named else "") + "say which car these skins are for (the archive does not name it)")
            if not (_cars_dir() / owner).is_dir():
                raise HTTPException(400, f"car not installed: {owner}")
            name = _skin_folder(src.name)
            if (owner, name) in seen:
                raise HTTPException(400, f"the skin {owner}/{name} appears twice in the archive")
            seen.add((owner, name))
            plan.append((owner, name, src))
        for owner, name, src in plan:
            dest = _cars_dir() / owner / "skins" / name
            dest.mkdir(parents=True, exist_ok=True)
            if (src / "ui_skin.json").is_file():
                shutil.copyfile(src / "ui_skin.json", dest / "ui_skin.json")
        return {"car": plan[0][0], "skins": [f"{o}/{n}" for o, n, _ in plan]}
    except uploadguard.Rejected as e:
        raise HTTPException(400, str(e)) from e
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@router.post("/cars/{car}/skins", status_code=201)
async def upload_skin(car: str, file: UploadFile) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".upload", dir=_scratch(), delete=False) as tmp:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
    try:
        res = await run_in_threadpool(_install_skins, Path(tmp.name), _safe(car))
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return {**res, "skin": res["skins"][0].split("/", 1)[1]}   # `skin`: the first, for older clients


@router.get("/cars/{car}/skins/{skin}.zip")
def download_skin(car: str, skin: str) -> FileResponse:
    return _zip_response(_cars_dir() / _safe(car) / "skins" / _safe(skin), f"{skin}.zip")


@router.get("/tracks")
def get_tracks() -> list[dict]:
    return list_tracks()


@router.get("/tracks/{track}/checksum")
def get_track_checksum(track: str, config: str | None = None) -> dict:
    return track_checksum(_safe(track), _safe(config) if config else None)


@router.get("/tracks/{track}.zip")
def download_track(track: str) -> FileResponse:
    return _zip_response(_tracks_dir() / _safe(track), f"{track}.zip")


# --- chunked uploads (the browser sends parts under Cloudflare's 100 MB request cap) ------------------------
# ponytail: registry in memory, single process (like the supervisor); a restart forgets unfinished uploads.
MAX_UPLOAD = 6 * 1024**3
_uploads: dict[str, dict] = {}


class UploadIn(BaseModel):
    kind: Literal["track", "car", "skin"] | None = None   # None = detect it from the archive (only when settings.upload_permissive); "skin" = skins for `car`, kept as folder names + ui_skin.json
    car: str = Field(default="", max_length=80)   # the car model the skins are for (only with kind "skin", when the archive does not say)
    pack: bool = False   # keep only what acServer reads (app/uploadguard.py)
    source_url: str = ""   # the modder's page («Descargar» button); without it the content has no button
    source_official: bool = False


def _need_kind(kind: str | None) -> None:
    if kind is None and not settings.upload_permissive:
        raise HTTPException(422, "say whether it is a car or a track (kind)")


def _upload(uid: str) -> dict:
    u = _uploads.get(uid)
    if not u:
        raise HTTPException(404, "unknown upload")
    return u


@router.post("/uploads", status_code=201)
def upload_start(body: UploadIn) -> dict:
    _need_kind(body.kind)
    if body.car:
        _skin_folder(body.car)
    uid = secrets.token_hex(8)
    path = _scratch() / f"upload-{uid}"
    path.write_bytes(b"")
    _uploads[uid] = {"path": path, "kind": body.kind or "", "requested_kind": body.kind, "car": body.car, "pack": body.pack, "source": catalog.check_source(body.source_url, body.source_official), "size": 0, "state": "uploading", "result": None, "error": None}
    return {"id": uid}


@router.put("/uploads/{uid}")
async def upload_chunk(uid: str, offset: int, request: Request) -> dict:
    """Append the next part. `offset` must equal what the server already has (409 tells the client where to resume)."""
    u = _upload(uid)
    if u["state"] != "uploading":
        raise HTTPException(409, "upload is no longer accepting data")
    if offset != u["size"]:
        raise HTTPException(409, f"expected offset {u['size']}")
    data = await request.body()
    if u["size"] + len(data) > MAX_UPLOAD:
        raise HTTPException(413, "archive too large")
    with u["path"].open("ab") as fh:
        fh.write(data)
    u["size"] += len(data)
    return {"size": u["size"]}


def _finish(uid: str) -> None:
    """Runs in its own thread: it must outlive the request that started it."""
    u = _uploads[uid]
    dest = _cars_dir() if u["requested_kind"] == "car" else _tracks_dir()   # no kind: the folder found inside decides where it goes
    try:
        if u["requested_kind"] == "skin":
            res = _install_skins(u["path"], u.get("car") or None)
            u["result"], u["note"], u["kind"] = ", ".join(res["skins"]), "", "skin"
        else:
            res = _extract(u["path"], dest, u.get("pack", False), u.get("source", ""), auto=u["requested_kind"] is None)
            u["result"], u["note"] = str(res), getattr(res, "note", "")
            u["kind"] = getattr(res, "kind", "") or u["requested_kind"] or ""
        u["state"] = "done"
    except HTTPException as e:
        u["state"], u["error"] = "error", str(e.detail)
    except Exception as e:  # noqa: BLE001 - a bad archive must end as an error state, not a lost thread
        u["state"], u["error"] = "error", f"{type(e).__name__}: {e}"
    finally:
        u["path"].unlink(missing_ok=True)
    metrics.log(0, "import_ok" if u["state"] == "done" else "import_error", name=u["result"] or u["kind"], track=u["kind"])


@router.post("/uploads/{uid}/complete", status_code=202)
def upload_complete(uid: str) -> dict:
    """Unpack in the background (hundreds of MB take longer than a proxy waits); poll GET /uploads/{id}."""
    u = _upload(uid)
    if u["state"] != "uploading" or not u["size"]:
        raise HTTPException(409, "nothing to unpack")
    u["state"] = "extracting"
    threading.Thread(target=_finish, args=(uid,), daemon=True).start()
    return {"state": "extracting"}


class LinkIn(BaseModel):
    kind: Literal["track", "car", "skin"] | None = None
    car: str = Field(default="", max_length=80)
    url: str = Field(min_length=8, max_length=2000)
    pack: bool = False
    source_url: str = ""
    source_official: bool = False


def _fetch_then_finish(uid: str, url: str) -> None:
    u = _uploads[uid]

    def progress(done: int, total: int | None) -> None:
        u["size"], u["total"] = done, total

    try:
        download.fetch(download.resolve(url, download.opener()), u["path"], MAX_UPLOAD, progress)
    except Exception as e:  # noqa: BLE001 - whatever went wrong must reach the panel as text
        u["state"], u["error"] = "error", f"download failed: {e}"
        u["path"].unlink(missing_ok=True)
        metrics.log(0, "import_error", name=u["kind"], track="download")
        return
    u["state"] = "extracting"
    _finish(uid)


@router.post("/uploads/from-link", status_code=202)
def upload_from_link(body: LinkIn) -> dict:
    """The server downloads a MediaFire / Google Drive / Dropbox link itself, then unpacks it (poll GET /uploads/{id})."""
    _need_kind(body.kind)
    if body.car:
        _skin_folder(body.car)
    try:
        download.check_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    uid = secrets.token_hex(8)
    path = _scratch() / f"upload-{uid}"
    path.write_bytes(b"")
    _uploads[uid] = {"path": path, "kind": body.kind or "", "requested_kind": body.kind, "car": body.car, "pack": body.pack, "source": catalog.check_source(body.source_url, body.source_official), "size": 0, "total": None, "state": "downloading", "result": None, "error": None}
    threading.Thread(target=_fetch_then_finish, args=(uid, body.url), daemon=True).start()
    return {"id": uid}


@router.get("/uploads/{uid}")
def upload_status(uid: str) -> dict:
    u = _upload(uid)
    return {k: u.get(k) for k in ("kind", "requested_kind", "size", "total", "state", "result", "note", "error")}


class InboxIn(BaseModel):
    file: str  # a file name inside the inbox dir
    pack: bool = False
    source_url: str = ""
    source_official: bool = False


@router.post("/tracks/import", status_code=201)
async def import_track(body: InboxIn) -> dict:
    """Unpack a .zip that was copied to the server's inbox (for archives over the proxy's upload limit)."""
    src = inbox_dir() / _safe(body.file)
    if not src.is_file():
        raise HTTPException(404, f"{body.file!r} is not in the inbox")
    return _installed(await run_in_threadpool(_extract, src, _tracks_dir(), body.pack, catalog.check_source(body.source_url, body.source_official)), "track")


@router.post("/tracks", status_code=201)
async def upload_track(file: UploadFile, pack: bool = False, source_url: str = "", source_official: bool = False) -> dict:
    return _installed(await _unzip_upload(file, _tracks_dir(), pack, source_url, source_official), "track")


@router.post("/entry_list")
def entry_list_builder(rows: list[EntryListRow]) -> list[dict]:
    return build_entry_list(rows)


# --- fixed setups (admin only): `FIXED_SETUP=<name>` in an entry makes acServer lock that car to setups/<name>.ini -------------------------------
MAX_SETUP = 256 * 1024
_admin = [Depends(require("admin"))]


def _setup_name(name: str) -> str:
    from app.schemas.servers import _setup_name as check   # one rule for the API, the entry list and the file name
    try:
        return check(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _wants_setup(session: dict | None, name: str) -> bool:
    session = session or {}
    return name in (session.get("car_setups") or {}).values() or any(e.get("fixed_setup") == name for e in session.get("entries") or [])


def setup_users(name: str) -> list[str]:
    """Where a setup is still chosen: a server's session or entry list, or a saved event. Used to refuse deleting it."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Championship, Event, LeagueMember, Server
    users = []
    with Session(engine) as sess:
        users += [f"league {c.name}" for c in sess.exec(select(Championship)).all() if name in (c.car_setups or {}).values()]
        users += [f"driver {m.name or m.guid} (league {m.championship_id})" for m in sess.exec(select(LeagueMember).where(LeagueMember.fixed_setup == name)).all()]
        users += [f"server {s.name}" for s in sess.exec(select(Server)).all() if _wants_setup(s.session, name) or any(e.get("FIXED_SETUP") == f"{name}.ini" for e in s.entry_list or [])]
        users += [f"event {ev.title}" for ev in sess.exec(select(Event)).all() if _wants_setup(ev.data, name)]
    return users


@router.get("/setups", dependencies=_admin)
def list_setups() -> list[dict]:
    return [{"name": p.stem, "size": p.stat().st_size} for p in sorted(setups_dir().glob("*.ini")) if p.is_file() and not p.is_symlink()]


@router.put("/setups/{name}", status_code=201, dependencies=_admin)
async def put_setup(name: str, request: Request) -> dict:
    """The body is the setup's .ini as it comes out of the game. Replacing one that is in use is fine (the swap is atomic); the next session start reads the new one."""
    name = _setup_name(name)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_SETUP:
            raise HTTPException(413, f"a setup is a small text file ({MAX_SETUP >> 10} KB at most)")
    raw = bytes(data).removeprefix(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "the setup is not a text (UTF-8) file") from None
    if not text.strip() or "\0" in text:
        raise HTTPException(400, "the setup is empty or not a text file")
    d = setups_dir()
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, d / f"{name}.ini")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"name": name, "size": len(raw)}


@router.delete("/setups/{name}", dependencies=_admin)
def delete_setup(name: str) -> dict:
    name = _setup_name(name)
    path = setups_dir() / f"{name}.ini"
    if not path.is_file():
        raise HTTPException(404, f"no setup called {name!r}")
    users = setup_users(name)
    if users:
        raise HTTPException(409, f"{name!r} is still chosen by: {', '.join(users)}")
    path.unlink()
    return {"deleted": name}
