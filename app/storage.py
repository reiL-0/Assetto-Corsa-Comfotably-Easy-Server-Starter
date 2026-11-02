"""How much disk the hosting client uses (the admin panel's home): the cars, the tracks, the setups, the manager's own data (database, results, logs) and the acServer program, against the plan's
quota (`settings.storage_quota_mb`, shown only: nothing refuses an upload for it) and the free space of the disk.

GET /api/v1/storage  (admin) -> {used_bytes, quota_bytes|null, parts: {cars, tracks, setups, data, program}, disk: {total, free}, counted_at}

Counting walks the folders (symbolic links are not followed, so nothing outside them is ever counted or read), so the answer is kept for CACHE_SECONDS: however many times the panel asks, the disk
is walked once per period, and one walk at a time. Check: tests/test_storage.py.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends

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
    du = shutil.disk_usage(data if data.exists() else ".")
    return {"used_bytes": sum(parts.values()), "quota_bytes": settings.storage_quota_mb * 1024 * 1024 or None, "parts": parts, "disk": {"total": du.total, "free": du.free},
            "counted_at": int(time.time())}


@router.get("")
def usage() -> dict:
    global _cache
    with _lock:   # one walk at a time: the others wait for it and are given its answer
        if _cache is None or time.monotonic() - _cache[0] > CACHE_SECONDS:
            _cache = (time.monotonic(), measure())
        return _cache[1]
