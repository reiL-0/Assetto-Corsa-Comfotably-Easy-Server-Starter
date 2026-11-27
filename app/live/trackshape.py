"""The shape of a track and its corners (T1..Tn), learnt from the cars themselves (stracker's LapBinBlob idea, kept to one lap per layout).

Pieces, all plain:
  * `Tracer`        per running server: the (spline, x, z) positions of each car in the lap it is driving (5 Hz from ACSP). When the
                    spline wraps the lap is put aside; `take(car)` hands it over when acServer's lap_completed arrives.
  * `clean(samples)`  a trace good enough for the geometry: starts near the line, ends near it, no hole in the spline (> 3 % of the lap).
  * `resample`      the trace on N equal steps of the spline (x, z).
  * `find_corners`  where the line bends: curvature over distance, smoothed over ±SMOOTH_M metres; a corner is a stretch tighter than
                    MAX_RADIUS_M that turns at least MIN_TURN_DEG in all; its apex is the tightest point; stretches closer than MERGE_M are one.
                    Numbered from the start line in the order of the lap. A chicane is two corners.
  * `submit` / `get` the best shape of a layout: the quickest clean lap seen (a quicker one replaces it), in the `track_shapes` table.

ponytail: the thresholds (200 m, 25°, 30 m) are first guesses for road courses; an oval's long bends may read as one or two corners, and a
track tighter than 200 m all the way round (a kart ring) has no straight to split it and gets no corners.
"""

from __future__ import annotations

import json
import logging
import math
import time

log = logging.getLogger("acm.trackshape")

N = 500                 # points of a stored shape
GAP_S = 1.5             # a hole longer than this in a car's packets: the lap is not traced
MAX_SPLINE_HOLE = 0.03  # nor if two packets are further apart than this share of the lap
MAX_RADIUS_M = 200.0
MIN_TURN_DEG = 25.0
SMOOTH_M = 15.0
MERGE_M = 30.0
MIN_LAP_M = 400.0       # a "lap" shorter than this is no track
MAX_SAMPLES = 6000      # 20 min of packets: a car parked on track stops adding (its lap is no trace anyway)
PAIR_S = 5.0            # acServer's lap_completed and the packet that crosses the line come in either order, this close


class Tracer:
    """Fed on the event loop by ACSPClient: `update` with every car_update, `lap_done` with every lap_completed. Whichever of the two comes
    second returns (trace, info) — the lap's line with the time and cuts acServer gave for it — and the other returns None."""

    def __init__(self) -> None:
        self.cur: dict[int, list[tuple[float, float, float, float]]] = {}    # car -> [(t, spline, x, z)] of the lap being driven
        self.done: dict[int, tuple[float, list]] = {}                         # car -> (when it crossed, the lap just finished, waiting for its lap_completed)
        self.pending: dict[int, tuple[float, dict]] = {}                      # car -> (when, the lap_completed info), waiting for the packet past the line

    def update(self, car: int, spline: float, x: float, z: float, t: float) -> tuple[list, dict] | None:
        lap = self.cur.setdefault(car, [])
        if lap:
            t0, s0 = lap[-1][0], lap[-1][1]
            if t - t0 > GAP_S:   # a hole in the packets (checked first: a hole across the line is no crossing either)
                self.cur[car] = [(t, spline, x, z)]
                self.done.pop(car, None)
                return None
            if s0 > 0.9 and spline < 0.1:   # the line
                trace = lap + [(t, 1.0 + spline, x, z)]
                self.cur[car] = [(t, spline, x, z)]
                info = self.pending.pop(car, None)
                if info and t - info[0] < PAIR_S:
                    return trace, info[1]
                self.done[car] = (t, trace)
                return None
            if spline < s0 - 0.02 or spline - s0 > MAX_SPLINE_HOLE or len(lap) >= MAX_SAMPLES:   # backwards, a teleport, parked for ages: this lap is no trace
                self.cur[car] = [(t, spline, x, z)]
                return None
        lap.append((t, spline, x, z))
        return None

    def lap_done(self, car: int, info: dict, now: float) -> tuple[list, dict] | None:
        got = self.done.pop(car, None)
        if got and now - got[0] < PAIR_S:
            return got[1], info
        self.pending[car] = (now, info)
        return None

    def drop(self, car: int) -> None:
        for m in (self.cur, self.done, self.pending):
            m.pop(car, None)

    def clear(self) -> None:
        for m in (self.cur, self.done, self.pending):
            m.clear()


def clean(samples: list | None) -> bool:
    if not samples or len(samples) < 30:
        return False
    s = [p[1] for p in samples]
    return s[0] < MAX_SPLINE_HOLE * 2 and s[-1] > 1.0 - MAX_SPLINE_HOLE and all(0 <= b - a <= MAX_SPLINE_HOLE for a, b in zip(s, s[1:]))


def resample(samples: list, n: int = N) -> list[tuple[float, float]]:
    """(x, z) at spline 0, 1/n, ... (n-1)/n, linear between the two packets around each."""
    out, j = [], 0
    for i in range(n):
        target = i / n
        while j < len(samples) - 2 and samples[j + 1][1] < target:
            j += 1
        a, b = samples[j], samples[j + 1]
        f = 0.0 if b[1] == a[1] else min(1.0, max(0.0, (target - a[1]) / (b[1] - a[1])))
        out.append((a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f))
    return out


def find_corners(points: list[tuple[float, float]]) -> list[dict]:
    """[{name, x, z, spline, dir}] of a closed line (`dir` +1 left / -1 right, by the sign of the turn in AC's x/z)."""
    n = len(points)
    if n < 20:
        return []
    seg = [math.dist(points[i], points[(i + 1) % n]) for i in range(n)]
    length = sum(seg)
    if length < MIN_LAP_M:
        return []
    head = [math.atan2(points[(i + 1) % n][1] - points[i][1], points[(i + 1) % n][0] - points[i][0]) for i in range(n)]
    turn = [(head[i] - head[i - 1] + math.pi) % (2 * math.pi) - math.pi for i in range(n)]   # at point i, wrapped to ±π
    step = length / n
    w = max(1, round(SMOOTH_M / step))
    k = [sum(turn[(i + d) % n] for d in range(-w, w + 1)) / ((2 * w + 1) * step) for i in range(n)]   # rad per metre, smoothed
    tight = [abs(v) > 1.0 / MAX_RADIUS_M for v in k]
    if all(tight):
        return []
    start = next(i for i in range(n) if not tight[i])   # walk the loop from a straight bit, so no corner is split by index 0
    runs, cur = [], None
    for d in range(1, n + 1):
        i = (start + d) % n
        if tight[i] and (cur is None or k[i] * k[cur[-1]] > 0):
            cur = (cur or []) + [i]
        else:
            if cur:
                runs.append(cur)
            cur = [i] if tight[i] else None
    if cur:
        runs.append(cur)
    merged = []
    for r in runs:   # two stretches of the same hand with a short straight between: one corner
        if merged and k[r[0]] * k[merged[-1][-1]] > 0 and ((r[0] - merged[-1][-1]) % n) * step <= MERGE_M:
            merged[-1] = merged[-1] + r
        else:
            merged.append(r)
    if len(merged) > 1 and k[merged[0][0]] * k[merged[-1][-1]] > 0 and ((merged[0][0] - merged[-1][-1]) % n) * step <= MERGE_M:
        merged[0] = merged.pop() + merged[0]   # (the same, across the point the walk started from)
    out = []
    for r in merged:
        if abs(math.degrees(sum(turn[i] for i in r))) < MIN_TURN_DEG:
            continue
        apex = max(r, key=lambda i: abs(k[i]))
        out.append({"x": round(points[apex][0], 1), "z": round(points[apex][1], 1), "spline": round(apex / n, 4), "dir": 1 if k[apex] > 0 else -1})
    out.sort(key=lambda c: c["spline"])
    for i, c in enumerate(out):
        c["name"] = f"T{i + 1}"
    return out


# --- the best shape of each layout ------------------------------------------------------------------------------------------------
_best: dict[tuple[str, str], int] = {}       # (track, layout) -> lap ms of the stored shape (this process)
_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
CACHE_S = 30.0


def _key(track: str | None, config: str | None) -> tuple[str, str]:
    from app import csp
    return csp.clean_track(track or ""), config or ""


def _load_best(key) -> int:
    if key not in _best:
        row = _read(key)
        _best[key] = row["lapMs"] if row else 0
    return _best[key]


def _read(key) -> dict | None:
    from sqlalchemy import text
    from sqlmodel import Session

    from app.db import engine
    with Session(engine) as db:
        r = db.execute(text("SELECT lap_ms, points, corners FROM track_shapes WHERE track = :t AND track_config = :c"), {"t": key[0], "c": key[1]}).first()
    return {"track": key[0], "config": key[1], "lapMs": r[0], "points": json.loads(r[1]), "corners": json.loads(r[2])} if r else None


def submit(track: str | None, config: str | None, lap_ms: int, samples: list | None) -> bool:
    """Keep this lap's shape if it is clean and quicker than the stored one. Never raises (the lap's other handlers come after)."""
    key = _key(track, config)
    if not key[0] or not 0 < lap_ms < 3_600_000 or not clean(samples):
        return False
    try:
        best = _load_best(key)
        if best and best <= lap_ms:
            return False
        points = resample(samples)
        corners = find_corners(points)
        from sqlalchemy import text
        from sqlmodel import Session

        from app.db import engine
        with Session(engine) as db:
            written = db.execute(text("""INSERT INTO track_shapes (track, track_config, lap_ms, points, corners, updated_at) VALUES (:t, :c, :ms, :p, :k, CURRENT_TIMESTAMP)
                               ON CONFLICT (track, track_config) DO UPDATE SET lap_ms = excluded.lap_ms, points = excluded.points, corners = excluded.corners,
                               updated_at = excluded.updated_at WHERE excluded.lap_ms < track_shapes.lap_ms"""),
                       {"t": key[0], "c": key[1], "ms": lap_ms, "p": json.dumps([[round(x, 1), round(z, 1)] for x, z in points]), "k": json.dumps(corners)}).rowcount > 0
            db.commit()
        _cache.pop(key, None)
        if not written:   # the database had a quicker one (another process, an old cache): learn it
            _best.pop(key, None)
            return False
        _best[key] = min(lap_ms, _best.get(key) or lap_ms)
        log.info("track shape of %s/%s from a %d ms lap: %d corners", key[0], key[1] or "-", lap_ms, len(corners))
        return True
    except Exception:   # noqa: BLE001 - a lost shape costs nothing; the lap's record, result and flags must still run
        log.exception("track shape of %s/%s not kept", *key)
        return False


_marks: dict[tuple[str, str], tuple[float, dict]] = {}
PIT_STEP_M = 4.0   # the pit lane drawn every few metres: enough for a map, a fraction of the file


def landmarks(track: str | None, config: str | None) -> dict:
    """{start: [x, z, dx, dz], pit: [[x, z], ...]} from the layout's AI lines the manager keeps for the pace bot (app/pitentry.py): the racing line
    starts at the start/finish line, and pit_lane.ai is the axis of the pit lane. {} when the track came without them. Read at most every CACHE_S."""
    key = _key(track, config)
    hit = _marks.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    out: dict = {}
    from app import pitentry
    d = pitentry.lines_path(*key) if key[0] else None
    import struct

    def read(name):   # one file at a time: a bad racing line must not hide a good pit lane
        try:
            return pitentry.read_ai(d / name)[0] if d and (d / name).is_file() else None
        except (OSError, ValueError, struct.error) as e:
            log.warning("AI line %s of %s/%s unreadable for the map: %s", name, key[0], key[1] or "-", e)
            return None

    line, pit = read("fast_lane.ai"), read("pit_lane.ai")
    if line:
        j = next((i for i in range(1, len(line)) if math.dist(line[0], line[i]) > 2.0), None)   # (a few metres on, for the direction of the line)
        if j:
            out["start"] = [round(line[0][0], 1), round(line[0][1], 1), round(line[j][0] - line[0][0], 2), round(line[j][1] - line[0][1], 2)]
    if pit:
        keep, last = [], None
        for p in pit:
            if last is None or math.dist(p, last) >= PIT_STEP_M:
                keep.append([round(p[0], 1), round(p[1], 1)])
                last = p
        if keep[-1] != [round(pit[-1][0], 1), round(pit[-1][1], 1)]:
            keep.append([round(pit[-1][0], 1), round(pit[-1][1], 1)])
        out["pit"] = keep
    _marks[key] = (time.monotonic(), out)
    return out


def get(track: str | None, config: str | None) -> dict | None:
    """The stored shape of a layout (read at most every CACHE_S), or None."""
    key = _key(track, config)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    try:
        shape = _read(key)
    except Exception:   # noqa: BLE001
        log.exception("track shape of %s/%s unreadable", *key)
        shape = None
    _cache[key] = (time.monotonic(), shape)
    return shape
