"""How much disk the hosting client uses (the admin panel's home): the cars, the tracks, the setups, the manager's own data (database, results, logs) and the acServer program, against the plan's
quota (`settings.storage_quota_mb`, shown only: nothing refuses an upload for it). The size or free space of the machine's disk is NOT in the answer: that is the provider's, not the client's.

GET /api/v1/storage  (admin) -> {used_bytes, quota_bytes|null, parts: {cars, tracks, setups, data, program}, counted_at}

Counting walks the folders (symbolic links are not followed, so nothing outside them is ever counted or read), so the answer is kept for CACHE_SECONDS: however many times the panel asks, the disk
is walked once per period, and one walk at a time. Check: tests/test_storage.py.
"""
from __future__ import annotations

import os
import shutil
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


def folder_bytes(root: Path, skip: frozenset[str] = frozenset()) -> int:
    """Bytes in the regular files under `root` (iterative, never follows a link, a vanished file counts for nothing, the folders in `skip` are not entered)."""
    total, stack = 0, [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if e.path not in skip:
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
             "data": folder_bytes(data, frozenset({str(data / "scratch")})), "program": program}   # (the unpacking area is not content: what is being uploaded is counted by its reservation)
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


STALE_SECONDS = 6 * 3600   # what an abandoned upload left in the unpacking area is deleted after this long


def _scratch_dir() -> Path:
    return Path(settings.data_dir) / "scratch"


def clean_scratch() -> None:
    """Delete what has sat untouched in the unpacking area for STALE_SECONDS (an upload nobody finished, a manager restart in the middle of one): it counts against the plan while it is there."""
    now = time.time()
    try:
        entries = list(os.scandir(_scratch_dir()))
    except OSError:
        return
    for e in entries:
        try:
            if now - e.stat(follow_symlinks=False).st_mtime > STALE_SECONDS:
                shutil.rmtree(e.path, ignore_errors=True) if e.is_dir(follow_symlinks=False) else os.unlink(e.path)
        except OSError:
            continue


def _no_room(used: int) -> HTTPException:
    return HTTPException(413, f"No queda espacio en tu plan: usas {used / 1024**3:.1f} GB de {settings.storage_quota_mb / 1024:.0f} GB. Retira contenido que no uses o pide ampliar el plan.")


def _used_now() -> int:
    """What the plan is using, for a decision: the installed content and data (the cached count, a few minutes old at worst) plus what is in the unpacking area RIGHT NOW (counted live, it is a handful
    of files): every upload sees the bytes the others have already written, so several at once cannot jointly pass the quota by more than one part."""
    return current()["used_bytes"] + folder_bytes(_scratch_dir())


def room() -> int | None:
    """Bytes the plan still has, or None when no quota is set (the league's own manager: no limit)."""
    quota = settings.storage_quota_mb * 1024 * 1024
    if not quota:
        return None
    clean_scratch()
    return max(0, quota - _used_now())


def check_room(extra: int = 0) -> None:
    """Refuse (413, a sentence for the client) when `extra` MORE bytes would not fit in the plan next to what is installed and what is already in the unpacking area; with 0 it asks only whether
    there is any room at all. Called as an upload starts and as each part arrives (and while a download progresses), so a client can neither start nor keep sending past its quota. No quota = never
    refuses. (The installed size, after unpacking and cutting down, is checked again by `check_install`.)"""
    left = room()
    if left is not None and (left == 0 or extra > left):
        raise _no_room(_used_now())


def check_install(size: int, replacing: Path | None = None) -> None:
    """The size of what is about to be installed (already cut down to the server pack) against the plan, from a fresh count: a small archive that expands into kept files cannot pass the quota. If
    it replaces a folder that is already installed, only the difference counts."""
    quota = settings.storage_quota_mb * 1024 * 1024
    if not quota:
        return
    gone = folder_bytes(replacing) if replacing is not None and replacing.is_dir() else 0
    if (used := measure()["used_bytes"]) + max(0, size - gone) > quota:
        raise _no_room(used)


@router.get("")
def usage() -> dict:
    return current()
