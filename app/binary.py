"""The acServer binary a hosting client brings (Kunos' own: the platform never ships or sells it). `settings.acserver_cmd` says where it must live; until a file is there no server starts.

* GET  /api/v1/binary   what is installed: {installed, path, size, sha256, arch, system} (admin)
* POST /api/v1/binary   the body is EITHER the bare `acServer` file OR a .zip that holds it (and, optionally, the folder `system/` next to it). Admin only; nothing is installed unless the
                        file is really an ELF executable (a Windows `acServer.exe` is told apart and refused with a sentence that says so) and no server is running.

What is taken from a zip: the one entry named exactly `acServer` and the regular files under `system/` (size and count capped, names checked: no absolute path, no `..`). Everything else in
the archive (cars, tracks, plugins, the .exe) is ignored, never written. The file is written next to its destination, made executable and renamed over the old one in one step, so a crash never leaves
half a binary. The binary then runs as `settings.acserver_uid` with no capabilities (supervisor.spawn_prefix): this module's job is to refuse what is not an executable, not to vouch for it.
Check: tests/test_binary.py.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app import supervisor
from app.auth import require
from app.config import settings

router = APIRouter(prefix="/binary", tags=["binary"], dependencies=[Depends(require("admin"))])

MAX_UPLOAD = 64 * 1024 * 1024     # the Linux acServer is a few MB; a zip of the whole server folder (with the content) would be far over this: the message says what to zip
MAX_BINARY = 48 * 1024 * 1024
MAX_SYSTEM_BYTES = 32 * 1024 * 1024
MAX_SYSTEM_FILES = 400
_ARCH = {3: "x86 (32-bit)", 62: "x86-64"}


def _dest() -> Path:
    if not settings.acserver_cmd:
        raise HTTPException(409, "the manager has no acServer path configured (ACM_ACSERVER_CMD)")
    return Path(shlex.split(settings.acserver_cmd)[0])


def inspect(head: bytes) -> str:
    """The architecture of an ELF executable, or HTTPException(422) with a sentence for the person who uploaded the wrong file."""
    if head[:2] == b"MZ":
        raise HTTPException(422, "that is the Windows program (acServer.exe). This server needs the file named exactly «acServer», with no extension, from the same folder.")
    if head[:4] != b"\x7fELF" or len(head) < 20:
        raise HTTPException(422, "that file is not the acServer program for Linux (it must be the file named «acServer», with no extension).")
    if head[5] != 1:   # EI_DATA: little endian
        raise HTTPException(422, "that program is not for this kind of server.")
    kind, machine = int.from_bytes(head[16:18], "little"), int.from_bytes(head[18:20], "little")
    if kind not in (2, 3) or machine not in _ARCH or head[4] != (1 if machine == 3 else 2):
        raise HTTPException(422, "that program is not an executable for x86 Linux.")
    return _ARCH[machine]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@router.get("")
def status() -> dict:
    dest = _dest() if settings.acserver_cmd else None
    if dest is None or not dest.is_file():
        return {"installed": False, "configured": dest is not None}
    with dest.open("rb") as f:
        head = f.read(32)
    try:
        arch = inspect(head)
    except HTTPException:
        arch = None   # something is there but it is not a program (a manual copy gone wrong)
    return {"installed": True, "configured": True, "size": dest.stat().st_size, "sha256": _sha256(dest), "arch": arch,
            "system": (dest.parent / "system").is_dir(), "running": supervisor.any_running()}


def _safe_system_name(name: str) -> str | None:
    """«system/data/surfaces.ini» -> the same, or None when the entry is not a regular file under system/ with a clean name."""
    parts = name.replace("\\", "/").split("/")
    if parts[-1] == "" or name.startswith(("/", "\\")) or any(p in ("", ".", "..") or ":" in p for p in parts):
        return None
    return "/".join(parts)


def _pick_from_zip(zf: zipfile.ZipFile, scratch: Path) -> tuple[Path, Path | None]:
    """(the extracted acServer, the extracted system folder or None). Only those two things ever leave the archive."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > 200_000:
        raise HTTPException(422, "that archive holds too many files; zip only the file «acServer» and the folder «system».")
    exact = sorted((i for i in infos if i.filename.replace("\\", "/").split("/")[-1] == "acServer"), key=lambda i: i.filename.count("/"))
    if not exact:
        raise HTTPException(422, "the zip has no file named «acServer» (the Windows acServer.exe does not work here).")
    binary = exact[0]
    if binary.file_size > MAX_BINARY:
        raise HTTPException(422, "the acServer file is far bigger than expected.")
    root = binary.filename.replace("\\", "/").rsplit("/", 1)[0] + "/" if "/" in binary.filename.replace("\\", "/") else ""
    out_bin = scratch / "acServer"
    with zf.open(binary) as src, out_bin.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    sysdir, total, n = None, 0, 0
    for i in infos:
        rel = i.filename.replace("\\", "/")
        if not rel.startswith(root + "system/"):
            continue
        clean = _safe_system_name(rel[len(root):])
        if clean is None:
            raise HTTPException(422, "the folder «system» in the zip has a file name that is not allowed.")
        n += 1
        total += i.file_size
        if n > MAX_SYSTEM_FILES or total > MAX_SYSTEM_BYTES:
            raise HTTPException(422, "the folder «system» is far bigger than expected.")
        target = scratch / clean
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(i) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        sysdir = scratch / "system"
    return out_bin, sysdir


def _install(upload: Path) -> dict:
    dest = _dest()
    if not supervisor.program_lock.acquire(blocking=False):   # one install at a time, and no start while it runs (supervisor.start refuses)
        raise HTTPException(409, "another upload is being installed: try again in a moment.")
    try:
        with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".binary-") as td:   # next to the destination: the final renames never cross a file system, and nothing is left behind on a failure
            scratch = Path(td)
            with upload.open("rb") as f:
                head = f.read(4)
            if head == b"PK\x03\x04":
                (scratch / "unzipped").mkdir()
                try:
                    with zipfile.ZipFile(upload) as zf:
                        new_bin, new_sys = _pick_from_zip(zf, scratch / "unzipped")
                except zipfile.BadZipFile:
                    raise HTTPException(422, "that zip is damaged.") from None
            else:
                new_bin, new_sys = upload, None
            with new_bin.open("rb") as f:
                arch = inspect(f.read(32))
            if supervisor.any_running():   # replacing the program under a running race is never what an admin wants: stop the servers first
                raise HTTPException(409, "a server is running: stop every server before installing the program.")
            staged = scratch / "acServer.staged"
            shutil.copyfile(new_bin, staged)
            os.chmod(staged, 0o755)
            staged_sys = None
            if new_sys is not None:   # everything is prepared BEFORE anything is swapped: a failed copy changes nothing
                staged_sys = dest.parent / ".system.staged"
                shutil.rmtree(staged_sys, ignore_errors=True)
                shutil.copytree(new_sys, staged_sys)
            system, keep = dest.parent / "system", scratch / "system.old"
            swapped = False
            try:
                if staged_sys is not None:
                    if system.exists():
                        os.replace(system, keep)
                    swapped = True
                    os.replace(staged_sys, system)
                os.replace(staged, dest)
            except BaseException:   # the binary could not be put in place: give the old system folder back, so program and resources still match
                if swapped:
                    shutil.rmtree(system, ignore_errors=True)
                    if keep.exists():
                        os.replace(keep, system)
                shutil.rmtree(dest.parent / ".system.staged", ignore_errors=True)
                raise
        return {"installed": True, "arch": arch, "sha256": _sha256(dest), "size": dest.stat().st_size, "system": new_sys is not None}
    finally:
        supervisor.program_lock.release()


@router.post("", status_code=201)
async def upload(request: Request) -> dict:
    _dest()   # (409 before reading a byte when nothing is configured)
    with tempfile.NamedTemporaryFile(prefix="acserver-upload-", delete=False) as tmp:
        path = Path(tmp.name)
        size = 0
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "that is too big for the acServer program; zip only the file «acServer» and the folder «system», not the whole server folder.")
                tmp.write(chunk)
        except BaseException:
            tmp.close()
            path.unlink(missing_ok=True)
            raise
    try:
        if size == 0:
            raise HTTPException(422, "the file is empty.")
        return await run_in_threadpool(_install, path)
    finally:
        path.unlink(missing_ok=True)
