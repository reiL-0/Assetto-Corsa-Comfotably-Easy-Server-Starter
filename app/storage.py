"""How much disk the hosting client uses (the admin panel's home): the cars, the tracks, the setups, the manager's own data (database, results, logs) and the acServer program, against the plan's
quota (`settings.storage_quota_mb`, shown only: nothing refuses an upload for it). The size or free space of the machine's disk is NOT in the answer: that is the provider's, not the client's.

GET /api/v1/storage  (admin) -> {used_bytes, quota_bytes|null, parts: {cars, tracks, setups, data, program}, counted_at}

Counting walks the folders (symbolic links are not followed, so nothing outside them is ever counted or read), so the answer is kept for CACHE_SECONDS: however many times the panel asks, the disk
is walked once per period, and one walk at a time. Check: tests/test_storage.py.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app import content
from app.auth import require
from app.config import settings

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require("admin"))])

CACHE_SECONDS = 120
_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def folder_bytes(root: Path) -> int:
    """Bytes in the regular files under `root` (iterative, never follows a link, a vanished file counts for nothing)."""
    total, stack = 0, [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def measure() -> dict:
    data = Path(settings.data_dir)
    program = 0
    if settings.acserver_cmd:
        exe = settings.acserver_dir() / Path(settings.acserver_cmd.split()[0]).name
        program = exe.stat().st_size if exe.is_file() else 0
    parts = {"cars": folder_bytes(content._cars_dir()), "tracks": folder_bytes(content._tracks_dir()), "setups": folder_bytes(content.setups_dir()),
             "data": folder_bytes(data), "program": program}
    return {"used_bytes": sum(parts.values()), "quota_bytes": settings.storage_quota_mb * 1024 * 1024 or None, "parts": parts, "counted_at": int(time.time())}


def current() -> dict:
    global _cache
    with _lock:   # one walk at a time: the others wait for it and are given its answer
        if _cache is None or time.monotonic() - _cache[0] > CACHE_SECONDS:
            _cache = (time.monotonic(), measure())
        return _cache[1]


def forget() -> None:
    """Content was installed or removed: the next question walks the disk again instead of waiting out the cache."""
    global _cache
    with _lock:
        _cache = None


def room() -> int | None:
    """Bytes the plan still has, or None when no quota is set (the league's own manager: no limit). 0 when it is full or over."""
    quota = settings.storage_quota_mb * 1024 * 1024
    return max(0, quota - current()["used_bytes"]) if quota else None


def check_room(extra: int = 0) -> None:
    """Refuse (413, a sentence for the client) when `extra` more bytes would not fit in the plan's space. Called as an upload starts and as each part arrives, so a client can neither start nor keep
    sending past its quota; with no quota set it never refuses. The figure is the cached count (a few minutes old at worst) plus this upload: uploads running side by side are not summed, but each one
    is stopped at the quota, and the unpacker checks the real free disk as well."""
    left = room()
    if left is not None and (left == 0 or extra > left):   # (a full plan refuses even to start: 0 bytes extra would otherwise pass)
        q = settings.storage_quota_mb * 1024 * 1024
        raise HTTPException(413, f"No queda espacio en tu plan: usas {current()['used_bytes'] / 1024**3:.1f} GB de {q / 1024**3:.0f} GB. Retira contenido que no uses o pide ampliar el plan.")


@router.get("")
def usage() -> dict:
    return current()
