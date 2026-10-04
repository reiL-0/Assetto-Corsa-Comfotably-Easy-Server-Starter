"""Content indexer: cars/tracks under data/content, checksums, entry-list
building, and zip-based upload/download for content and skins."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

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
        tracks.append({"track": d.name, "configs": configs})
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


def _extract(archive: Path, dest_parent: Path) -> str:
    """Unpacks a .zip or .rar (single top-level dir = the content's name) into `dest_parent`. Returns that name.
    Extracts to a scratch dir first and only moves it in after checking every entry stayed inside it."""
    with archive.open("rb") as fh:
        magic = fh.read(8)
    scratch = Path(tempfile.mkdtemp(dir=_scratch()))
    try:
        if magic[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(archive) as zf:
                for n in zf.namelist():
                    if n.startswith("/") or ".." in Path(n).parts:
                        raise HTTPException(400, f"unsafe archive path: {n!r}")
                zf.extractall(scratch)
        elif magic[:6] == b"Rar!\x1a\x07":
            if not shutil.which("bsdtar"):
                raise HTTPException(501, "rar needs bsdtar (apt install libarchive-tools)")
            r = subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(scratch)], capture_output=True, text=True, check=False)
            if r.returncode:
                raise HTTPException(400, f"cannot read rar: {r.stderr.strip()[:200]}")
        else:
            raise HTTPException(400, "not a zip or rar archive")
        for f in scratch.rglob("*"):  # no symlinks, nothing outside the scratch dir
            if f.is_symlink() or scratch.resolve() not in f.resolve().parents:
                raise HTTPException(400, f"unsafe archive entry: {f.relative_to(scratch)}")
        tops = [p for p in scratch.iterdir()]
        if len(tops) != 1 or not tops[0].is_dir():
            raise HTTPException(400, "archive must contain exactly one top-level folder (the content's name)")
        root = _safe(tops[0].name)
        dest_parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tops[0], dest_parent / root, dirs_exist_ok=True)
        return root
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _unzip_upload(file: UploadFile, dest_parent: Path) -> str:
    """Streams an uploaded .zip/.rar to disk (tracks are hundreds of MB) and unpacks it."""
    with tempfile.NamedTemporaryFile(suffix=".upload", dir=_scratch(), delete=False) as tmp:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
    try:
        return await run_in_threadpool(_extract, Path(tmp.name), dest_parent)
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


@router.post("/cars", status_code=201)
async def upload_car(file: UploadFile) -> dict:
    return {"car": await _unzip_upload(file, _cars_dir())}


@router.post("/cars/{car}/skins", status_code=201)
async def upload_skin(car: str, file: UploadFile) -> dict:
    return {"car": _safe(car), "skin": await _unzip_upload(file, _cars_dir() / _safe(car) / "skins")}


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


class InboxIn(BaseModel):
    file: str  # a file name inside the inbox dir


@router.post("/tracks/import", status_code=201)
async def import_track(body: InboxIn) -> dict:
    """Unpack a .zip/.rar that was copied to the server's inbox (for archives over the proxy's upload limit)."""
    src = inbox_dir() / _safe(body.file)
    if not src.is_file():
        raise HTTPException(404, f"{body.file!r} is not in the inbox")
    return {"track": await run_in_threadpool(_extract, src, _tracks_dir())}


@router.post("/tracks", status_code=201)
async def upload_track(file: UploadFile) -> dict:
    return {"track": await _unzip_upload(file, _tracks_dir())}


@router.post("/entry_list")
def entry_list_builder(rows: list[EntryListRow]) -> list[dict]:
    return build_entry_list(rows)
