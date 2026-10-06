"""The acServer builds we accept, by SHA-256, and the check of what a client uploads.

A client proves it owns the game by uploading its `acServer` (or `acServer.exe`): the file is hashed **while it streams in and is never stored**; the hash is
compared with the registered official builds. A server then runs OUR copy of the matching version (`dir_for`), so a modified binary can never run and the
package is always complete. Hash match proves the client has the file, not that it holds a licence (that is the terms of service, see todo/docker-plan.md).
"""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.config import settings
from app.db import SessionDep
from app.models import AcBinary

router = APIRouter(prefix="/binaries", tags=["binaries"])
PLATFORMS = ("linux", "windows")
NAMES = {"linux": "acServer", "windows": "acServer.exe"}


class BinaryIn(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    platform: Literal["linux", "windows"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dir: str = Field(default="", max_length=500)
    verified: bool = False
    note: str = Field(default="", max_length=300)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _out(b: AcBinary) -> dict:
    return {"id": b.id, "version": b.version, "platform": b.platform, "sha256": b.sha256, "dir": b.dir, "verified": b.verified, "note": b.note}


def _add(sess: Session, body: BinaryIn) -> AcBinary:
    if sess.exec(select(AcBinary).where(AcBinary.platform == body.platform, AcBinary.sha256 == body.sha256)).first():
        raise HTTPException(409, "that build is already registered")
    if body.dir and not (Path(body.dir) / NAMES["linux"]).is_file():
        raise HTTPException(400, f"{body.dir!r} does not hold an `acServer` binary")
    b = AcBinary(**body.model_dump())
    sess.add(b)
    sess.commit()
    sess.refresh(b)
    return b


@router.get("")
def list_binaries(sess: SessionDep) -> list[dict]:
    return [_out(b) for b in sess.exec(select(AcBinary).order_by(AcBinary.id))]


@router.post("", status_code=201)
def register(body: BinaryIn, sess: SessionDep) -> dict:
    return _out(_add(sess, body))


class InstalledIn(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    verified: bool = False


@router.post("/register-installed", status_code=201)
def register_installed(body: InstalledIn, sess: SessionDep) -> list[dict]:
    """Hash the acServer install this manager runs (the folder of ACM_ACSERVER_CMD): the Linux binary and, if the folder has it, `acServer.exe`."""
    root = settings.acserver_dir()
    if not root:
        raise HTTPException(400, "ACM_ACSERVER_CMD is not configured")
    out = []
    for plat, name in NAMES.items():
        f = root / name
        if f.is_file():
            out.append(_out(_add(sess, BinaryIn(version=body.version, platform=plat, sha256=sha256_file(f), dir=str(root) if plat == "linux" else "",
                                                verified=body.verified, note="hashed from this server's own folder"))))
    if not out:
        raise HTTPException(404, "no acServer binary found in the install folder")
    return out


@router.delete("/{binary_id}", status_code=204)
def unregister(binary_id: int, sess: SessionDep) -> None:
    b = sess.get(AcBinary, binary_id)
    if not b:
        raise HTTPException(404, "unknown binary")
    sess.delete(b)
    sess.commit()


@router.post("/verify")
async def verify_upload(file: UploadFile, sess: SessionDep) -> dict:
    """Hashes an uploaded `acServer`/`acServer.exe` as it streams in and **discards it** (nothing is written). `known` = it is a registered official build."""
    h, size = hashlib.sha256(), 0
    while chunk := await file.read(1 << 20):
        h.update(chunk)
        size += len(chunk)
        if size > 64 * 1024**2:
            raise HTTPException(413, "too big to be an acServer binary")
    digest = h.hexdigest()
    hits = list(sess.exec(select(AcBinary).where(AcBinary.sha256 == digest)))
    return {"sha256": digest, "size": size, "known": bool(hits), "matches": [_out(b) for b in hits]}


def dir_for(sess: Session, binary_id: int | None) -> Path | None:
    """The folder a server of this registered version runs from (its `acServer` and `system/`), or None for the global install. 409 if it cannot run."""
    if binary_id is None:
        return None
    b = sess.get(AcBinary, binary_id)
    if not b or b.platform != "linux" or not b.dir or not (Path(b.dir) / NAMES["linux"]).is_file():
        raise HTTPException(409, "the chosen acServer version cannot run here (unknown, not a Linux build, or its folder is gone)")
    return Path(b.dir)


def command_for(sess: Session, binary_id: int | None) -> str:
    """The argv string to start a server with: its version's `acServer`, or the global `ACM_ACSERVER_CMD`."""
    d = dir_for(sess, binary_id)
    return shlex.quote(str(d / NAMES["linux"])) if d else settings.acserver_cmd
