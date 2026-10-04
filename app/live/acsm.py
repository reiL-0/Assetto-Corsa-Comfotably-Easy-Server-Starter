"""The small slice of AC Server Manager (ACSM) that the league site reads, served from this manager's own state.

Point a site's `acsmUrl` at `<manager>/api/v1/servers/<id>/acsm` and its live timing, track map and telemetry
validation work with no ACSM or stracker. All routes are public reads (the manager listens on localhost only).
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import supervisor
from app.content import _tracks_dir

router = APIRouter(prefix="/servers/{server_id}/acsm", tags=["acsm-compat"])

_NAME = re.compile(r"^[\w.\-]+$")  # one path segment: no separators, no "..": the track/layout folders on disk


@router.get("/api/live-timings/leaderboard.json")
def leaderboard(server_id: int) -> dict:
    inst = supervisor.get(server_id)
    if not inst or not inst.running or not inst.acsp:
        raise HTTPException(503, "server not running")  # sites treat a failed fetch as "offline"
    return inst.acsp.board.leaderboard()


def _asset(track: str, config: str, rel: str) -> Path:
    if not _NAME.match(track) or (config and not _NAME.match(config)):
        raise HTTPException(404, "not found")
    base = _tracks_dir() / track
    p = base / config / rel if config else base / rel
    if not p.is_file():
        raise HTTPException(404, "not found")
    return p


@router.get("/content/tracks/{track}/map.png")
@router.get("/content/tracks/{track}/{config}/map.png")
def map_png(track: str, config: str = "") -> FileResponse:
    return FileResponse(_asset(track, config, "map.png"), media_type="image/png")


@router.get("/content/tracks/{track}/data/map.ini")
@router.get("/content/tracks/{track}/{config}/data/map.ini")
def map_ini(track: str, config: str = "") -> FileResponse:
    return FileResponse(_asset(track, config, "data/map.ini"), media_type="text/plain")
