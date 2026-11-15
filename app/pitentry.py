"""Where the pit lane begins on the lap, worked out from a track's own AI lines when it is uploaded (before the pack is cut down: `ai/` is not kept on the server).

`ai/fast_lane.ai` is the racing line and `ai/pit_lane.ai` the pit lane axis (format in `read_ai`). The entry is the place of the racing line nearest to the first point of the pit axis that has
left it (more than OFF_M away; the first point of the axis when it never gets that far), as a fraction of the lap (0..1). `write_all` leaves it in `<layout>/data/pit_entry.json` ({"entry": 0.4312}),
which the race state hands to the hub with the pit-lane speed limit (the limit is announced a little before the entry). An estimate: the hub corrects it with the real entry as soon as it sees one."""
import json
import math
import struct
from pathlib import Path

OFF_M = 8.0     # the pit axis is this far from the racing line: it has left the track
FILE = "pit_entry.json"
MAX_BYTES = 64 << 20


def read_ai(path: Path) -> tuple[list[tuple[float, float]], list[float]]:
    """(x, z) and the distance from the start of every point of a version 7 .ai file: a 16-byte header (version, count, 0, 0) and `count` points of 20 bytes (x, y, z, length, id)."""
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("too big")
    b = path.read_bytes()
    ver, cnt = struct.unpack_from("<2i", b, 0)
    if ver != 7 or cnt <= 1 or 16 + cnt * 20 > len(b):
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


def write_all(track: Path) -> int:
    """Every layout of an unpacked track (the folder itself and its sub-folders) that has `ai/pit_lane.ai` and `data/` gets its `data/pit_entry.json`. Returns how many were written."""
    n = 0
    for layout in [track, *(d for d in sorted(track.iterdir()) if d.is_dir())]:
        if not (layout / "data").is_dir() or not (layout / "ai" / "pit_lane.ai").is_file():
            continue
        entry = entry_of(layout)
        if entry is not None:
            (layout / "data" / FILE).write_text(json.dumps({"entry": entry}))
            n += 1
    return n


def read(track_dir: Path, config: str = "") -> float | None:
    """The entry saved for a track on the server (`config` = its layout, "" when it has none)."""
    try:
        v = json.loads(((track_dir / config if config else track_dir) / "data" / FILE).read_text())["entry"]
        return float(v) if isinstance(v, (int, float)) and 0 <= v < 1 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
