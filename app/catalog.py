"""Catalog of the cars and tracks on this machine, by the SHA-256 of their files.

Three levels (see todo/docker-plan.md 2c/2e): `ContentBlob` (a folder known by its hash, stored once), `ContentHolder` (who has it enabled, with their
licence declaration and a status) and `BlockedHash` (removed for everyone after a claim against the file itself). Every change leaves a `CatalogEvent`.

- `digest_dir`: the hash of a folder = SHA-256 over the sorted `path NUL sha256(file)` lines, so it does not depend on file order or timestamps.
- `record_upload`: called by `content._extract` after an install; `check_not_blocked` runs before it.
- Rights claims: `revoke` (one holder), `block` (the file, for everybody), `dispute` / `restore` (the holder shows a licence). A file is deleted from disk only
  when no holder is left active (`purge_if_orphan`).
- Proof of possession without uploading again: `make_challenge` / `check_proof`. The server picks random byte ranges of the stored files and a nonce; the client
  answers with SHA-256(nonce + those bytes) (`answer_for`, also what a client tool runs). Possession is not a licence: the holder still declares it.
- `source_url` is the modder's official page, shown to players as «Descargar»; the files themselves are never served to players.

Single process, challenges live in memory (like the upload registry in content.py).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.db import SessionDep, engine
from app.models import BlockedHash, CatalogEvent, ContentBlob, ContentHolder

router = APIRouter(prefix="/catalog", tags=["catalog"])
LEAGUE = "league"
CHALLENGE_TTL = 600
RANGES, RANGE_LEN = 16, 4096
_challenges: dict[str, dict] = {}   # nonce -> {hash, ranges, expires}; one use each


def _now() -> datetime:
    return datetime.now(UTC)


def digest_dir(root: Path) -> tuple[str, int, int]:
    """(hash, total bytes, file count) of a car/track folder."""
    h, size, n = hashlib.sha256(), 0, 0
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        fh = hashlib.sha256()
        with f.open("rb") as fp:
            while chunk := fp.read(1 << 20):
                fh.update(chunk)
        h.update(f"{f.relative_to(root).as_posix()}\0{fh.hexdigest()}\n".encode())
        size += f.stat().st_size
        n += 1
    return h.hexdigest(), size, n


def _log(sess: Session, actor: str, action: str, hash_: str = "", holder: str = "", detail: str = "") -> None:
    sess.add(CatalogEvent(actor=actor, action=action, hash=hash_, holder=holder, detail=detail[:500]))


def _dir_of(blob: ContentBlob) -> Path:
    from app import content   # lazy: content imports this module
    return (content._cars_dir() if blob.kind == "car" else content._tracks_dir()) / blob.name


def check_not_blocked(hash_: str) -> None:
    with Session(engine) as sess:
        b = sess.get(BlockedHash, hash_)
        if b:
            raise HTTPException(403, f"this content was removed after a rights claim ({b.reason or 'no reason given'}) and cannot be uploaded again")


_FILE_EXT = (".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".msi")


def check_source(url: str, official: bool = False) -> str:
    """The modder's page for the «Descargar» button: http(s) and a page, never a direct link to the archive (we point to the author, we do not hand out the file).
    `official`: whoever gives the link confirms it is the author's official download page (kept in the catalog log)."""
    url = url.strip()
    if url and not official:
        raise HTTPException(400, "confirm that source_url is the official download page (source_official=true)")
    if url and (not re.fullmatch(r"https?://\S+", url) or len(url) > 500 or urlsplit(url).path.lower().endswith(_FILE_EXT)):
        raise HTTPException(400, "source_url must be the modder's page (http/https), not a direct link to the file")
    return url


def record_upload(kind: str, name: str, hash_: str, size: int, files: int, holder: str = LEAGUE, actor: str = "", source_url: str = "") -> None:
    """An install just happened: note the blob and make `holder` an active holder (uploading counts as declaring the licence).
    `source_url`: the modder's page given with the upload; it only fills a blob that has none (an admin's link is not overwritten)."""
    with Session(engine) as sess:
        if not sess.get(ContentBlob, hash_):
            sess.add(ContentBlob(hash=hash_, kind=kind, name=name, size=size, files=files))
        blob = sess.get(ContentBlob, hash_)
        if source_url and not blob.source_url:
            blob.source_url = source_url
            _log(sess, actor, "source", hash_, holder, f"{source_url} (confirmed as the official download page by the uploader)")
        row = sess.exec(select(ContentHolder).where(ContentHolder.hash == hash_, ContentHolder.holder == holder)).first()
        if row and row.status in ("revoked", "disputed"):
            raise HTTPException(403, f"{name!r} was {row.status} for this account after a rights claim; ask the administrator")
        if not row:
            row = ContentHolder(hash=hash_, holder=holder, uploaded_by=actor)
            sess.add(row)
        row.status, row.attested_at, row.updated_at = "active", _now(), _now()
        for old in sess.exec(select(ContentHolder).where(ContentHolder.holder == holder, ContentHolder.hash != hash_, ContentHolder.status == "active")):
            ob = sess.get(ContentBlob, old.hash)
            if ob and ob.kind == kind and ob.name == name:
                old.status, old.updated_at = "superseded", _now()
        _log(sess, actor, "upload", hash_, holder, f"{kind} {name} ({files} files, {size} bytes)")
        sess.commit()


def purge_if_orphan(sess: Session, hash_: str, actor: str = "") -> bool:
    """Deletes the folder from disk when no holder is active any more. True if it was deleted."""
    blob = sess.get(ContentBlob, hash_)
    if not blob or sess.exec(select(ContentHolder).where(ContentHolder.hash == hash_, ContentHolder.status == "active")).first():
        return False
    d = _dir_of(blob)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        _log(sess, actor, "purge", hash_, "", f"{blob.kind} {blob.name} deleted from disk")
        return True
    return False


def _holder(sess: Session, holder_id: int) -> ContentHolder:
    h = sess.get(ContentHolder, holder_id)
    if not h:
        raise HTTPException(404, "unknown holder")
    return h


def _set_status(sess: Session, holder_id: int, status: str, action: str, note: str, actor: str) -> dict:
    h = _holder(sess, holder_id)
    h.status, h.note, h.updated_at = status, note[:300], _now()
    _log(sess, actor, action, h.hash, h.holder, note)
    sess.commit()
    return {"holder": h.id, "status": h.status, "purged": purge_if_orphan(sess, h.hash, actor) if status in ("revoked", "disputed") else False}


# --- where players get the content ---------------------------------------------------------------------------------------------

def download_links(track: str, cars: list[str]) -> list[dict]:
    """The official pages (set by an admin, `source_url`) of the track and cars of a session, for the «Descargar» buttons. We never serve the files:
    only content with a known page is listed; the rest is left out (the organiser shares it)."""
    out, seen = [], set()
    try:
        with Session(engine) as sess:
            for b in sess.exec(select(ContentBlob).where(ContentBlob.source_url != "")):
                if ((b.kind == "track" and b.name == track) or (b.kind == "car" and b.name in cars)) and (b.kind, b.name) not in seen:
                    seen.add((b.kind, b.name))
                    out.append({"kind": b.kind, "name": b.name, "url": b.source_url})
    except OperationalError:   # the module-level SAMPLE of announcement.py is built before the tables exist
        return []
    return sorted(out, key=lambda x: (x["kind"] != "track", x["name"]))


# --- proof of possession ------------------------------------------------------------------------------------------

def _files(blob: ContentBlob) -> list[tuple[str, int]]:
    d = _dir_of(blob)
    return [(f.relative_to(d).as_posix(), f.stat().st_size) for f in sorted(p for p in d.rglob("*") if p.is_file() and p.stat().st_size)]


def make_challenge(sess: Session, hash_: str) -> dict:
    blob = sess.get(ContentBlob, hash_)
    files = _files(blob) if blob else []
    if not files:
        raise HTTPException(404, "unknown content")
    total = sum(s for _, s in files)
    ranges = []
    for _ in range(RANGES):
        pos = secrets.randbelow(total)
        for path, size in files:
            if pos < size:
                off = min(pos, max(0, size - RANGE_LEN))
                ranges.append({"path": path, "offset": off, "length": min(RANGE_LEN, size)})
                break
            pos -= size
    nonce = secrets.token_hex(16)
    _challenges[nonce] = {"hash": hash_, "ranges": ranges, "expires": time.time() + CHALLENGE_TTL}
    return {"nonce": nonce, "ranges": ranges, "expires_in": CHALLENGE_TTL}


def answer_for(root: Path, nonce: str, ranges: list[dict]) -> str:
    """What a client computes from ITS copy of the folder: SHA-256(nonce + the requested byte ranges)."""
    h = hashlib.sha256(nonce.encode())
    for r in ranges:
        with (root / r["path"]).open("rb") as fp:
            fp.seek(r["offset"])
            h.update(fp.read(r["length"]))
    return h.hexdigest()


def check_proof(sess: Session, nonce: str, answer: str) -> str:
    """The hash the challenge was about, if the answer is right (the nonce is used up either way); HTTPException otherwise."""
    ch = _challenges.pop(nonce, None)
    if not ch or ch["expires"] < time.time():
        raise HTTPException(410, "unknown or expired challenge")
    blob = sess.get(ContentBlob, ch["hash"])
    if not blob or not secrets.compare_digest(answer_for(_dir_of(blob), nonce, ch["ranges"]), answer):
        raise HTTPException(403, "the answer does not match: you do not seem to have this exact content")
    return ch["hash"]


# --- routes ---------------------------------------------------------------------------------------------------------

def _item(sess: Session, b: ContentBlob) -> dict:
    hs = list(sess.exec(select(ContentHolder).where(ContentHolder.hash == b.hash)))
    return {"hash": b.hash, "kind": b.kind, "name": b.name, "size": b.size, "files": b.files, "source_url": b.source_url,
            "holders": [{"id": h.id, "holder": h.holder, "status": h.status, "uploaded_by": h.uploaded_by, "attested_at": h.attested_at, "note": h.note} for h in hs]}


@router.get("")
def list_items(sess: SessionDep) -> list[dict]:
    return [_item(sess, b) for b in sess.exec(select(ContentBlob).order_by(ContentBlob.kind, ContentBlob.name))]


@router.get("/downloads")
def downloads(track: str = "", cars: str = "") -> list[dict]:
    """Links to the official pages of a track and cars (comma-separated), for the event pages and the Discord announcement."""
    return download_links(track, [c for c in cars.split(",") if c])


@router.get("/events")
def events(sess: SessionDep, limit: int = 100) -> list[dict]:
    rows = sess.exec(select(CatalogEvent).order_by(CatalogEvent.id.desc()).limit(min(limit, 500)))
    return [r.model_dump(mode="json") for r in rows]


@router.post("/scan")
def scan_installed(sess: SessionDep, limit: int = 20) -> dict:
    """Hashes installed cars/tracks the catalog does not know yet and registers them for the league. Reading a folder takes a while: `limit` per call."""
    from app import content
    known = {(b.kind, b.name) for b in sess.exec(select(ContentBlob))}
    todo = [("car", c["car"]) for c in content.list_cars()] + [("track", t["track"]) for t in content.list_tracks()]
    todo = [x for x in todo if x not in known
            and any(f.is_file() for f in ((content._cars_dir() if x[0] == "car" else content._tracks_dir()) / x[1]).rglob("*"))]   # an empty folder (Kunos' ks_* stubs) has nothing to hash: all would collide on one digest and never count as done
    for kind, name in todo[:limit]:
        d = (content._cars_dir() if kind == "car" else content._tracks_dir()) / name
        h, size, n = digest_dir(d)
        record_upload(kind, name, h, size, n, LEAGUE, "scan")
    return {"registered": min(limit, len(todo)), "remaining": max(0, len(todo) - limit)}


class SourceIn(BaseModel):
    url: str = Field(default="", max_length=500)
    official: bool = False   # «this is the official download page»: required with a url


@router.put("/{hash_}/source")
def set_source(hash_: str, body: SourceIn, sess: SessionDep) -> dict:
    b = sess.get(ContentBlob, hash_)
    if not b:
        raise HTTPException(404, "unknown content")
    b.source_url = check_source(body.url, body.official)
    _log(sess, "admin", "source", hash_, "", body.url + " (confirmed as official)" if body.url else "removed")
    sess.commit()
    return _item(sess, b)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=300)


@router.post("/holders/{holder_id}/revoke")
def revoke(holder_id: int, body: NoteIn, sess: SessionDep) -> dict:
    """A claim against ONE holder (e.g. they uploaded a rip): only their row is revoked; the files stay while another holder is active."""
    return _set_status(sess, holder_id, "revoked", "revoke", body.note, "admin")


@router.post("/holders/{holder_id}/dispute")
def dispute(holder_id: int, body: NoteIn, sess: SessionDep) -> dict:
    return _set_status(sess, holder_id, "disputed", "dispute", body.note, "admin")


@router.post("/holders/{holder_id}/restore")
def restore(holder_id: int, body: NoteIn, sess: SessionDep) -> dict:
    """The holder showed a licence (or the claim fell): active again. Only possible while the hash is not blocked."""
    h = _holder(sess, holder_id)
    if sess.get(BlockedHash, h.hash):
        raise HTTPException(409, "the file is blocked for everybody: unblock it first")
    return _set_status(sess, holder_id, "active", "restore", body.note, "admin")


class BlockIn(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.post("/{hash_}/block")
def block(hash_: str, body: BlockIn, sess: SessionDep) -> dict:
    """A claim against the file itself: every holder goes to «disputed», the folder is deleted and the hash can no longer be uploaded."""
    if not sess.get(BlockedHash, hash_):
        sess.add(BlockedHash(hash=hash_, reason=body.reason))
    for h in sess.exec(select(ContentHolder).where(ContentHolder.hash == hash_, ContentHolder.status == "active")):
        h.status, h.updated_at = "disputed", _now()
    _log(sess, "admin", "block", hash_, "", body.reason)
    sess.commit()
    return {"blocked": True, "purged": purge_if_orphan(sess, hash_, "admin")}


@router.delete("/{hash_}/block", status_code=204)
def unblock(hash_: str, sess: SessionDep) -> None:
    b = sess.get(BlockedHash, hash_)
    if b:
        sess.delete(b)
        _log(sess, "admin", "unblock", hash_)
        sess.commit()


class ChallengeIn(BaseModel):
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.post("/challenge")
def challenge(body: ChallengeIn, sess: SessionDep) -> dict:
    return make_challenge(sess, body.hash)


class ProofIn(BaseModel):
    nonce: str
    answer: str
    holder: str = Field(min_length=1, max_length=60)
    license_attested: bool   # the holder declares they hold the licence for this content


@router.post("/prove", status_code=201)
def prove(body: ProofIn, sess: SessionDep) -> dict:
    """Possession shown by answering a challenge: the holder is enabled for the existing copy without uploading it again."""
    if not body.license_attested:
        raise HTTPException(400, "the licence declaration is required")
    hash_ = check_proof(sess, body.nonce, body.answer)
    check_not_blocked(hash_)
    blob = sess.get(ContentBlob, hash_)
    record_upload(blob.kind, blob.name, hash_, blob.size, blob.files, body.holder, "proof")
    _log(sess, body.holder, "proof", hash_, body.holder, "possession shown by challenge")
    sess.commit()
    return _item(sess, blob)
