"""What an uploaded content archive may contain, and the «server pack» that keeps only what acServer reads.

Pure functions (no I/O besides the files they are given), used by `content._extract`:
- `check_names` / `check_zip`: refuse unsafe member names (absolute, `..`, backslash tricks, drive letters, NUL) and zip bombs (too many files, too many
  bytes once unpacked, a single file too big, an absurd compression ratio).
- `keep_in_pack`: with `pack=True` an upload is cut down to what the server verifies and lists (`data.acd`/`data/`, the track's `surfaces.ini` and
  `models*.ini`, the `ui_*.json` names, the skin folders): no `.kn5`, textures, sounds or `.psd`. A tested car went from 385 MB to 2.5 MB and the server
  started the same (see todo/docker-plan.md). Players keep the full content on their own PC; the server never sends it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath

MAX_FILES = 50_000
MAX_TOTAL = 6 * 1024**3         # unpacked bytes (same as the upload cap in content.py)
MAX_FILE = 2 * 1024**3
MAX_RATIO = 1000                # unpacked / packed for one file, only checked above MIN_RATIO_SIZE
MIN_RATIO_SIZE = 50 * 1024**2


class Rejected(ValueError):
    """The archive is not acceptable; the message is a sentence for the person who uploaded it."""


def safe_member(name: str) -> str:
    """The member name as a clean relative POSIX path, or `Rejected`. Backslashes count as separators (a Windows zip)."""
    n = name.replace("\\", "/")
    parts = PurePosixPath(n).parts
    if not n or "\0" in n or n.startswith("/") or (len(n) > 1 and n[1] == ":") or ".." in parts:
        raise Rejected(f"unsafe archive path: {name!r}")
    return "/".join(p for p in parts if p != ".")


def check_names(names: list[str]) -> None:
    for n in names:
        safe_member(n)


def check_zip(zf: zipfile.ZipFile, *, max_files: int = MAX_FILES, max_total: int = MAX_TOTAL, max_file: int = MAX_FILE) -> None:
    """Looks at the headers only (nothing is unpacked): names, count, sizes and ratios."""
    infos = zf.infolist()
    if len(infos) > max_files:
        raise Rejected(f"too many files in the archive ({len(infos)} > {max_files})")
    check_names([i.filename for i in infos])
    total = 0
    for i in infos:
        total += i.file_size
        if i.file_size > max_file:
            raise Rejected(f"{i.filename!r} is too big once unpacked")
        if i.file_size > MIN_RATIO_SIZE and i.file_size / max(i.compress_size, 1) > MAX_RATIO:
            raise Rejected(f"{i.filename!r} looks like a zip bomb (compression ratio over {MAX_RATIO}:1)")
    if total > max_total:
        raise Rejected(f"the archive unpacks to {total >> 20} MB, over the {max_total >> 20} MB limit")


def check_tree(root: Path, *, max_files: int = MAX_FILES, max_total: int = MAX_TOTAL) -> None:
    """The same count/size limits on what was really unpacked (a last look after the sandboxed unpacking)."""
    n = total = 0
    for f in root.rglob("*"):
        if f.is_file():
            n += 1
            total += f.stat().st_size
            if n > max_files or total > max_total:
                raise Rejected("the archive unpacks to more files or bytes than allowed")


def detect_kind(top: Path) -> str | None:
    """«car» or «track» from what the unpacked folder holds, or None when it is not clear (then the kind the uploader chose stands).
    A car has `ui/ui_car.json` or `data.acd` (or an unpacked `data/` with `car.ini`); a track has `models*.ini` or a `data/surfaces.ini` (also inside a layout folder)."""
    names = {p.name.lower() for p in top.iterdir()}
    car = (top / "ui" / "ui_car.json").is_file() or "data.acd" in names or (top / "data" / "car.ini").is_file()
    track = any(n.startswith("models") and n.endswith(".ini") for n in names) or any(top.glob("**/data/surfaces.ini")) or any(top.glob("ui/**/ui_track.json"))
    return "car" if car and not track else "track" if track and not car else None


def keep_in_pack(kind: str, rel: str) -> bool:
    """Is this file (path inside the car/track folder, `/`-separated) part of the server pack?"""
    p = PurePosixPath(rel)
    low = [x.lower() for x in p.parts]
    name = low[-1]
    if kind == "car":
        if low[0] == "data" or name == "data.acd" and len(low) == 1:
            return True
        return low == ["ui", "ui_car.json"] or (len(low) == 3 and low[0] == "skins" and name == "ui_skin.json")
    # track: base and layout folders alike (`<layout>/data/…`, `models_<layout>.ini`, `ui/<layout>/ui_track.json`)
    if "data" in low[:-1]:
        return True
    return (name.startswith("models") and name.endswith(".ini")) or name in ("ui_track.json", "map.png", "outline.png", "preview.png")


def prune(root: Path, kind: str) -> tuple[int, int]:
    """Deletes what is not in the server pack from an unpacked car/track folder. Empty folders are removed too, except the skin folders (their names are the skins). Returns (files, bytes) removed."""
    files = size = 0
    for f in sorted(root.rglob("*")):
        if f.is_file() and not keep_in_pack(kind, f.relative_to(root).as_posix()):
            size += f.stat().st_size
            files += 1
            f.unlink()
    for d in sorted((x for x in root.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):   # empty folders go, except skin names
        rel = d.relative_to(root).parts
        if not any(d.iterdir()) and not (kind == "car" and rel[0].lower() == "skins"):
            d.rmdir()
    return files, size
