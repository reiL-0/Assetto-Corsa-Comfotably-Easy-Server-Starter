"""Where the pit lane begins on the lap, worked out from a track's own AI lines when it is uploaded (before the pack is cut down: `ai/` is not kept on the server).

`ai/fast_lane.ai` is the racing line and `ai/pit_lane.ai` the pit lane axis (format in `read_ai`). The entry is the place of the racing line nearest to the first point of the pit axis that has
left it (more than OFF_M away; the first point of the axis when it never gets that far), as a fraction of the lap (0..1). `write_all` keeps it in the manager's own data folder
(`<data_dir>/pit_entry/<track>/<layout>.json`, {"entry": 0.4312}), NEVER inside the track: the content folders are hashed, sealed (app/integrity.py hashes each layout's whole `data/`) and offered to
the drivers, and one more file there would change what they are compared with. The race state hands it to the hub with the pit-lane speed limit (the limit is announced a little before the entry).
An estimate: the hub corrects it with the real entry as soon as it sees one. The AI files and the saved value are never trusted: a size and a point limit, and anything unreadable is "not known"."""
import json
import math
import os
import re
import struct
from pathlib import Path

from app.config import settings

OFF_M = 8.0     # the pit axis is this far from the racing line: it has left the track
MAX_BYTES = 4 << 20     # a real racing line is a few hundred KB (VIR: 2 MB): more is not one
MAX_POINTS = 100_000
MAX_JSON = 256
NAME = re.compile(r"[\w][\w .\-]*")   # a track or layout folder name (layouts have spaces: «full course»): never a path


def read_ai(path: Path) -> tuple[list[tuple[float, float]], list[float]]:
    """(x, z) and the distance from the start of every point of a version 7 .ai file: a 16-byte header (version, count, 0, 0) and `count` points of 20 bytes (x, y, z, length, id)."""
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("too big")
    b = path.read_bytes()
    ver, cnt = struct.unpack_from("<2i", b, 0)
    if ver != 7 or not 1 < cnt <= MAX_POINTS or 16 + cnt * 20 > len(b):
        raise ValueError("not a version 7 AI file")
    pts = [struct.unpack_from("<4f", b, 16 + i * 20) for i in range(cnt)]
    if not all(math.isfinite(v) for p in pts for v in p):
        raise ValueError("not numbers")
    return [(p[0], p[2]) for p in pts], [p[3] for p in pts]


def entry_of(layout: Path) -> float | None:
    """The pit entry of a layout folder (the one that holds `ai/`) as a fraction of the lap; None when it has no pit axis or the files cannot be read."""
    try:
        line, length = read_ai(layout / "ai" / "fast_lane.ai")
        pit, _ = read_ai(layout / "ai" / "pit_lane.ai")
    except (OSError, ValueError, struct.error):
        return None
    total = length[-1]
    if total <= 0:
        return None
    step = max(1, len(line) // 4000)   # (a point every few metres is plenty: the answer is used to within tens of metres)
    idx = range(0, len(line), step)

    def nearest(p):
        i = min(idx, key=lambda i: (line[i][0] - p[0]) ** 2 + (line[i][1] - p[1]) ** 2)
        return i, math.hypot(line[i][0] - p[0], line[i][1] - p[1])

    first = nearest(pit[0])[0]
    for p in pit[::max(1, len(pit) // 400)]:
        i, d = nearest(p)
        if d > OFF_M:
            first = i
            break
    return round(min(max(length[first] / total, 0.0), 0.9999), 4)


def store_path(track: str, config: str) -> Path | None:
    """Where the entry of a layout is kept (`config` = "" for a track that is one layout itself); None for a name that could be a path."""
    if not NAME.fullmatch(track) or (config and not NAME.fullmatch(config)) or ".." in track + config:
        return None
    return Path(settings.data_dir) / "pit_entry" / track / f"{config or '-'}.json"   # (a folder per track: two different pairs never share a file; no name starts with «-»)


def write_all(track: Path) -> int:
    """Every layout of an unpacked track (the folder itself and its sub-folders) that has `ai/pit_lane.ai` and `data/` gets its pit entry saved. Returns how many were written."""
    n = 0
    for layout in [track, *(d for d in sorted(track.iterdir()) if d.is_dir())]:
        if not (layout / "data").is_dir() or not (layout / "ai" / "pit_lane.ai").is_file():
            continue
        dest = store_path(track.name, "" if layout == track else layout.name)
        entry = entry_of(layout)
        if dest is None or entry is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(json.dumps({"entry": entry}))
        os.replace(tmp, dest)   # (a reader never sees half a file, and a failed write keeps the old value)
        n += 1
    return n


def read(track: str, config: str = "") -> float | None:
    """The entry saved for a track of the server (`config` = its layout, "" when it has none), as a fraction of the lap rounded to 4 decimals (the hub reads plain decimals); None when it is not known or the file is not ours."""
    try:
        dest = store_path(track, config)
        if dest is None or not dest.is_file() or dest.stat().st_size > MAX_JSON:
            return None
        v = json.loads(dest.read_text())["entry"]
        return min(round(float(v), 4), 0.9999) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1 else None   # (0.99999 must not round up to 1)
    except Exception:   # noqa: BLE001 - OSError, a bad JSON, a nesting that is too deep: the state every hub reads must never fail because of this file
        return None
