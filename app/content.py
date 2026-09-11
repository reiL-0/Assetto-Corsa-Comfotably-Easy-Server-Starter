"""Content indexer: cars/tracks under data/content, checksums, entry-list
building, and zip-based upload/download for content and skins."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.config import settings

router = APIRouter(prefix="/content", tags=["content"])


def _safe(name: str) -> str:
    """A path segment, not a path: rejects traversal / separators."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, f"invalid name: {name!r}")
    return name


def _content_dir() -> Path:
    d = Path(settings.data_dir) / "content"
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


async def _unzip_upload(file: UploadFile, dest_parent: Path) -> str:
    """Extracts an uploaded zip (single top-level dir = the content's name)."""
    data = await file.read()
    with zipfile.ZipFile(BytesIO(data)) as zf:
        names = zf.namelist()
        if not names:
            raise HTTPException(400, "empty archive")
        root = _safe(names[0].split("/")[0])
        for n in names:
            if n.startswith("/") or ".." in Path(n).parts:
                raise HTTPException(400, f"unsafe archive path: {n!r}")
        dest_parent.mkdir(parents=True, exist_ok=True)
        zf.extractall(dest_parent)
    return root


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


@router.post("/tracks", status_code=201)
async def upload_track(file: UploadFile) -> dict:
    return {"track": await _unzip_upload(file, _tracks_dir())}


@router.post("/entry_list")
def entry_list_builder(rows: list[EntryListRow]) -> list[dict]:
    return build_entry_list(rows)
