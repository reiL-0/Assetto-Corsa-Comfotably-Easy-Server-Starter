"""A customer's own cars and tracks: upload, quota, and the `content/` folder each of its servers sees.

- A customer uploads a `.zip` (one folder; always cut down to the server pack: `uploadguard.prune`). The pack is hashed; if the catalog already has that hash the
  upload is **discarded** and the customer simply becomes another holder (the upload proved possession); otherwise it is kept once in `<data>/blobs/<hash>/`.
  The holder of a customer is `t<tenant_id>`. The same hash is only accepted under the folder name it was first stored with.
- Quota: the sum of the size of everything the tenant holds must stay within its plan's `disk_mb` (None = unlimited).
- `compose(server)` builds `<instance>/content/{cars,tracks}/<name>` as symlinks to the blobs the tenant holds (and nothing else): a server cannot see another
  customer's content. `missing(server)` lists what its config asks for that the tenant does not hold (the start is refused with that list).
- Chunked uploads (a customer's zip may be hundreds of MB) mirror content.py's registry, with the owner checked on every call.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlmodel import Session, select

from app import catalog, content, tenancy, uploadguard
from app.auth import CurrentUser
from app.db import SessionDep, engine
from app.models import ContentBlob, ContentHolder, Plan, Server, Tenant

router = APIRouter(prefix="/tenant/content", tags=["tenant-content"])
MAX_UPLOAD = content.MAX_UPLOAD
_uploads: dict[str, dict] = {}   # uid -> {tenant, kind, path, size, state, result, error}


def holder_of(tenant_id: int) -> str:
    return f"t{tenant_id}"


def _tenant_id(user) -> int:
    if user.tenant_id is None:
        raise HTTPException(400, "this is for customer accounts")
    return user.tenant_id


def _held(sess: Session, tenant_id: int) -> list[tuple[ContentHolder, ContentBlob]]:
    out = []
    for h in sess.exec(select(ContentHolder).where(ContentHolder.holder == holder_of(tenant_id), ContentHolder.status == "active")):
        b = sess.get(ContentBlob, h.hash)
        if b:
            out.append((h, b))
    return out


def used_bytes(sess: Session, tenant_id: int) -> int:
    return sum(b.size for _, b in _held(sess, tenant_id))


def _quota(sess: Session, tenant_id: int) -> int | None:
    t = sess.get(Tenant, tenant_id)
    p = sess.get(Plan, t.plan_id) if t else None
    return p.disk_mb * 1024 * 1024 if p and p.disk_mb else None


# --- adding content -------------------------------------------------------------------------------------------------------

def add_from_zip(archive: Path, kind: str, tenant_id: int, actor: str) -> dict:
    """Unpack, cut to the server pack, hash, dedupe, keep (once) and make the tenant a holder. Returns the item."""
    scratch = Path(tempfile.mkdtemp(dir=content._scratch()))
    try:
        top = content.unpack_top(archive, scratch)
        name = content._safe(top.name)
        kind = uploadguard.detect_kind(top) or kind   # a car sent as a track (or the other way round) is kept as what it is
        uploadguard.prune(top, kind)
        digest, size, nfiles = catalog.digest_dir(top)
        catalog.check_not_blocked(digest)
        with Session(engine) as sess:
            quota = _quota(sess, tenant_id)
            held = {b.hash for _, b in _held(sess, tenant_id)}
            if quota is not None and digest not in held and used_bytes(sess, tenant_id) + size > quota:
                raise HTTPException(413, "your storage quota is full")
            blob = sess.get(ContentBlob, digest)
            if blob and (blob.name != name or blob.kind != kind):
                raise HTTPException(409, f"this content is already known as {blob.kind} {blob.name!r}: upload it with that folder name")
        if not blob:   # first copy of this content on the machine: kept once, in the blob store
            dest = catalog.blob_path(digest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(top), str(dest))
        catalog.record_upload(kind, name, digest, size, nfiles, holder_of(tenant_id), actor, store="blob")
        with Session(engine) as sess:
            return _item(sess.get(ContentBlob, digest), "active")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _item(b: ContentBlob, status: str) -> dict:
    return {"hash": b.hash, "kind": b.kind, "name": b.name, "size": b.size, "files": b.files, "source_url": b.source_url, "status": status}


# --- composing a server's content/ ----------------------------------------------------------------------------------------

def compose(s: Server, instance: Path) -> dict[str, list[str]]:
    """`<instance>/content/` made only of symlinks to what the server's tenant holds. Returns {"cars": [...], "tracks": [...]}."""
    root = instance / "content"
    if root.is_symlink():
        root.unlink()   # it pointed at the shared content/ before the server belonged to a customer
    out: dict[str, list[str]] = {"cars": [], "tracks": []}
    with Session(engine) as sess:
        held = _held(sess, s.tenant_id)
        for kind, folder in (("car", "cars"), ("track", "tracks")):
            d = root / folder
            d.mkdir(parents=True, exist_ok=True)
            for old in d.iterdir():
                old.unlink() if old.is_symlink() else shutil.rmtree(old)
            for _, b in held:
                if b.kind == kind:
                    (d / b.name).symlink_to(catalog._dir_of(b))
                    out[folder].append(b.name)
    return out


def missing(s: Server) -> list[str]:
    """What the server's config asks for that its tenant does not hold (cars, track)."""
    sv = (s.config or {}).get("SERVER", {})
    cars = {c for c in str(sv.get("CARS", "")).split(";") if c} | {e.get("MODEL") for e in s.entry_list if e.get("MODEL")}
    with Session(engine) as sess:
        have = {(b.kind, b.name) for _, b in _held(sess, s.tenant_id)}
    need = [("car", c) for c in sorted(cars)] + ([("track", str(sv["TRACK"]))] if sv.get("TRACK") else [])
    return [f"{k} {n}" for k, n in need if (k, n) not in have]


# --- routes ---------------------------------------------------------------------------------------------------------------

@router.get("")
def list_mine(user: CurrentUser, sess: SessionDep) -> dict:
    tid = _tenant_id(user)
    items = [_item(b, h.status) for h, b in _held(sess, tid)]
    return {"items": sorted(items, key=lambda i: (i["kind"], i["name"])), "used_bytes": used_bytes(sess, tid), "quota_bytes": _quota(sess, tid)}


@router.post("/{kind}", status_code=201)
async def upload_zip(kind: Literal["car", "track"], file: UploadFile, user: CurrentUser) -> dict:
    """A small .zip in one request (a big one goes through /uploads in parts)."""
    tid = _tenant_id(user)
    with tempfile.NamedTemporaryFile(suffix=".upload", dir=content._scratch(), delete=False) as tmp:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
            if tmp.tell() > MAX_UPLOAD:
                raise HTTPException(413, "archive too large")
    try:
        return await run_in_threadpool(add_from_zip, Path(tmp.name), kind, tid, user.username)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@router.delete("/{hash_}", status_code=204)
def remove_mine(hash_: str, user: CurrentUser, sess: SessionDep) -> None:
    """The customer stops holding this content; the files are deleted when nobody else holds them."""
    tid = _tenant_id(user)
    h = sess.exec(select(ContentHolder).where(ContentHolder.hash == hash_, ContentHolder.holder == holder_of(tid))).first()
    if not h:
        raise HTTPException(404, "you do not hold this content")
    sess.delete(h)
    sess.commit()
    catalog.purge_if_orphan(sess, hash_, user.username)
    sess.commit()


class UploadIn(BaseModel):
    kind: Literal["car", "track"]


def _mine(uid: str, tid: int) -> dict:
    u = _uploads.get(uid)
    if not u or u["tenant"] != tid:
        raise HTTPException(404, "unknown upload")
    return u


@router.post("/uploads/start", status_code=201)
def upload_start(body: UploadIn, user: CurrentUser) -> dict:
    tid = _tenant_id(user)
    uid = secrets.token_hex(8)
    path = content._scratch() / f"tenant-upload-{uid}"
    path.write_bytes(b"")
    _uploads[uid] = {"tenant": tid, "kind": body.kind, "path": path, "size": 0, "state": "uploading", "result": None, "error": None, "actor": user.username}
    return {"id": uid}


@router.put("/uploads/{uid}")
async def upload_chunk(uid: str, offset: int, request: Request, user: CurrentUser) -> dict:
    u = _mine(uid, _tenant_id(user))
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
    u = _uploads[uid]
    try:
        u["result"] = add_from_zip(u["path"], u["kind"], u["tenant"], u["actor"])
        u["state"] = "done"
    except HTTPException as e:
        u["state"], u["error"] = "error", str(e.detail)
    except Exception as e:  # noqa: BLE001 - a bad archive ends as an error state, not a lost thread
        u["state"], u["error"] = "error", f"{type(e).__name__}: {e}"
    finally:
        u["path"].unlink(missing_ok=True)


@router.post("/uploads/{uid}/complete", status_code=202)
def upload_complete(uid: str, user: CurrentUser) -> dict:
    u = _mine(uid, _tenant_id(user))
    if u["state"] != "uploading" or not u["size"]:
        raise HTTPException(409, "nothing to unpack")
    u["state"] = "extracting"
    threading.Thread(target=_finish, args=(uid,), daemon=True).start()
    return {"state": "extracting"}


@router.get("/uploads/{uid}")
def upload_status(uid: str, user: CurrentUser) -> dict:
    u = _mine(uid, _tenant_id(user))
    return {k: u.get(k) for k in ("kind", "size", "state", "result", "error")}
