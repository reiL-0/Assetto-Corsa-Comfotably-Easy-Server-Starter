"""A live map (map.png + data/map.ini) drawn from a track's 3D model (.kn5), for a track that came without a usable map.png.

Content Manager does this on the player's PC; a server pack keeps no .kn5, so it is done once, when the track is uploaded:
  * `stage(top, dest)`   BEFORE the upload is cut down to the server pack: for every layout without a good map.png, the .kn5 files its models ini names are
                         MOVED (a rename, no copy) into `dest`, with the layout's surface keys in plan.json. Nothing is kept in the track.
  * `start(stage, track)` AFTER the track is installed: a child process (`python -m app.kn5map`) draws the maps into `<data_dir>/track_maps/<track>/<layout or ->/`
                         with a time limit; the stage (and its .kn5) is deleted when it ends, whatever happens.
  * `generated(track, config, name)` the drawn map.png / map.ini, which the live map routes serve before the track's own (app/live/acsm.py).

The .kn5 (`sc6969`, version 5 or 6): textures (skipped with seek), materials (skipped), then a tree of nodes: a dummy carries a 4x4 matrix (row vectors, translation in
the last row), a mesh carries vertices (44 bytes: position, normal, uv, tangent) and uint16 indices in its parent's space. A mesh whose name is a digit then a surface
KEY of data/surfaces.ini with IS_VALID_TRACK=1 or IS_PITLANE=1 (`1ROAD`, `2KERB_A`) is driveable: those triangles, seen from above, are the map.
A model's POSITION of models.ini is added (its ROTATION is not: ponytail, every track seen so far has 0,0,0).
Untrusted file: counts, sizes, the tree's depth and the pixels filled are bounded, the child runs under a memory and CPU limit and one at a time; an encrypted or
broken .kn5 raises ValueError and the track keeps the fallback map (AI line / traced lap). ponytail: a pack's map.png is judged by its PNG header only (a truncated
file with a big header counts as a map).
"""

from __future__ import annotations

import json
import logging
from array import array
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

log = logging.getLogger("acm.kn5map")

MAX_PX = 2048            # the longer side of a drawn map
PAD_PX = 24
MAX_TEXTURES = 20_000
MAX_MATERIALS = 20_000
MAX_NODES = 400_000
MAX_DEPTH = 64
MAX_VERTS = 1_000_000    # in one mesh
MAX_TRIS = 2_000_000     # kept for one layout (24 bytes each in an array('f'): ~48 MB)
MAX_FILL = 300_000_000   # pixels tested by the triangle fill of one layout: a bound on the CPU whatever the triangles look like
CHILD_MEM = 1536 << 20   # the drawing child process may not grow past this (RLIMIT_AS): a hostile .kn5 cannot take the server's memory
MAX_STRING = 4096
TIMEOUT_S = 600
PLACEHOLDER_PX = 64      # a map.png smaller than this on a side is a placeholder (20 x 20 black squares exist)
NAME = re.compile(r"[\w][\w .\-]*")


def _plain(name: str) -> bool:
    """A folder or file name that cannot be a path (no separators, no «..»)."""
    return bool(NAME.fullmatch(name or "")) and ".." not in name


class _R:
    def __init__(self, f, size):
        self.f, self.size = f, size

    def read(self, n):
        b = self.f.read(n)
        if len(b) != n:
            raise ValueError("truncated .kn5")
        return b

    def i32(self):
        return struct.unpack("<i", self.read(4))[0]

    def count(self, cap, what):
        n = self.i32()
        if not 0 <= n <= cap:
            raise ValueError(f"bad {what} count {n}")
        return n

    def s(self):
        n = self.count(MAX_STRING, "string length")
        return self.read(n).decode("utf-8", "replace")

    def skip(self, n):
        if n < 0 or self.f.tell() + n > self.size:
            raise ValueError("skip past the end")
        self.f.seek(n, 1)


def _mul(a, b):
    """4x4 row-major product a·b (row vectors: world = local · parent)."""
    return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]


_ID = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def driveable(name: str, keys: tuple[str, ...]) -> bool:
    m = re.match(r"^\d+([A-Za-z_][\w]*)", name or "")
    return bool(m) and any(m.group(1).upper().startswith(k) for k in keys)


def read_triangles(path: Path, keys: tuple[str, ...], offset=(0.0, 0.0, 0.0), budget: list | None = None, out: array | None = None) -> array:
    """x1, z1, x2, z2, x3, z3 of every triangle of the driveable meshes, in world metres, flat in an array('f') (appended to `out`). `budget` = [triangles still allowed]."""
    budget = budget if budget is not None else [MAX_TRIS]
    out = out if out is not None else array("f")
    size = path.stat().st_size
    with open(path, "rb") as f:
        r = _R(f, size)
        if r.read(6) != b"sc6969":
            raise ValueError("not a .kn5 (or an encrypted one)")
        ver = r.i32()
        if ver not in (5, 6):
            raise ValueError(f"unknown .kn5 version {ver}")
        if ver > 5:
            r.i32()
        for _ in range(r.count(MAX_TEXTURES, "texture")):
            r.i32()
            r.s()
            r.skip(r.count((1 << 31) - 1, "texture size"))
        for _ in range(r.count(MAX_MATERIALS, "material")):
            r.s(); r.s()
            r.read(1); r.read(1); r.i32()   # blend mode, alpha tested, depth mode
            for _ in range(r.count(1000, "material property")):
                r.s(); r.read(4 + 36)
            for _ in range(r.count(1000, "sampler")):
                r.s(); r.i32(); r.s()
        nodes = [0]

        def node(parent, depth):
            if depth > MAX_DEPTH:
                raise ValueError("node tree too deep")
            nodes[0] += 1
            if nodes[0] > MAX_NODES:
                raise ValueError("too many nodes")
            kind = r.i32()
            name = r.s()
            children = r.count(100_000, "child")
            r.read(1)   # active
            world = parent
            if kind == 1:   # dummy: its matrix
                m = list(struct.unpack("<16f", r.read(64)))
                if not all(math.isfinite(v) for v in m):
                    raise ValueError("bad matrix")
                world = _mul(m, parent)
            elif kind == 2:   # mesh
                r.read(3)
                nv = r.count(MAX_VERTS, "vertex")
                keep = driveable(name, keys)
                if keep:
                    raw = r.read(nv * 44)
                    xs = [struct.unpack_from("<3f", raw, i * 44) for i in range(nv)]
                    pts = [(x * world[0] + y * world[4] + z * world[8] + world[12] + offset[0],
                            x * world[2] + y * world[6] + z * world[10] + world[14] + offset[2]) for x, y, z in xs]
                else:
                    r.skip(nv * 44)
                ni = r.count(MAX_VERTS * 3, "index")
                if keep:
                    idx = struct.unpack(f"<{ni}H", r.read(ni * 2))
                    for t in range(0, ni - 2, 3):
                        a, b, c = idx[t], idx[t + 1], idx[t + 2]
                        if a < nv and b < nv and c < nv:
                            if budget[0] <= 0:
                                raise ValueError("too many triangles")
                            budget[0] -= 1
                            out.extend((*pts[a], *pts[b], *pts[c]))
                else:
                    r.skip(ni * 2)
                r.read(4 + 4 + 8 + 16 + 1)   # material, layer, lod in/out, bounding sphere, renderable
            elif kind == 3:   # skinned mesh (cars, flags): never road
                r.read(3)
                for _ in range(r.count(10_000, "bone")):
                    r.s(); r.read(64)
                r.skip(r.count(MAX_VERTS, "vertex") * 76)
                r.skip(r.count(MAX_VERTS * 3, "index") * 2)
                r.read(4 + 4 + 8)
            else:
                raise ValueError(f"unknown node kind {kind}")
            for _ in range(children):
                node(world, depth + 1)

        node(_ID, 0)
    return out


def surface_keys(surfaces_ini: Path) -> tuple[str, ...]:
    """KEYs of the surfaces you drive on (IS_VALID_TRACK=1) and the pit lane (IS_PITLANE=1)."""
    keys, cur = [], {}
    try:
        text = surfaces_ini.read_text(encoding="utf-8", errors="replace") if surfaces_ini.stat().st_size < 1_000_000 else ""
    except OSError:
        return ()
    for line in text.splitlines() + ["[END]"]:
        line = line.split(";")[0].strip()
        if line.startswith("["):
            if cur.get("KEY") and (cur.get("IS_VALID_TRACK") == "1" or cur.get("IS_PITLANE") == "1"):
                keys.append(cur["KEY"].upper())
            cur = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            cur[k.strip().upper()] = v.strip()
    return tuple(dict.fromkeys(keys))


def models(ini: Path) -> list[tuple[str, tuple[float, float, float]]]:
    """(file, position) of each [MODEL_n] of a models*.ini."""
    out, cur = [], {}
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines() + ["[END]"]:
        line = line.split(";")[0].strip()
        if line.startswith("["):
            f = cur.get("FILE", "")
            if f and _plain(f) and f.lower().endswith(".kn5"):
                try:
                    pos = tuple(float(v) for v in (cur.get("POSITION") or "0,0,0").split(",")[:3])
                except ValueError:
                    pos = (0.0, 0.0, 0.0)
                out.append((f, pos if len(pos) == 3 else (0.0, 0.0, 0.0)))
            cur = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            cur[k.strip().upper()] = v.strip()
    return out


def png_size(p: Path) -> tuple[int, int] | None:
    try:
        with open(p, "rb") as f:
            b = f.read(24)
    except OSError:
        return None
    if b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", b[16:24])


def _layouts(top: Path) -> list[str]:
    """"" for a track that is one layout, else each layout folder (the ones with data/), as AC does."""
    subs = [d.name for d in sorted(top.iterdir()) if d.is_dir() and (d / "data").is_dir() and _plain(d.name)]
    return subs or [""]


def stage(top: Path, dest: Path) -> int:
    """Move the .kn5 files of every layout without a good map.png into `dest` (before the cut) and write its plan. Returns the layouts planned."""
    plan, moved = {}, set()
    for lay in _layouts(top):
        base = top / lay if lay else top
        size = png_size(base / "map.png")
        if size and min(size) >= PLACEHOLDER_PX:
            continue   # the pack has a real map: it is the one shown
        ini = top / f"models_{lay}.ini" if lay else top / "models.ini"
        mods = models(ini) if ini.is_file() else ([(f"{top.name}.kn5", (0.0, 0.0, 0.0))] if (top / f"{top.name}.kn5").is_file() else
                                                    [(p.name, (0.0, 0.0, 0.0)) for p in sorted(top.glob("*.kn5"))])
        keys = surface_keys(base / "data" / "surfaces.ini") or surface_keys(top / "data" / "surfaces.ini")
        mods = [(f, pos) for f, pos in mods if (top / f).is_file() or f in moved]
        if not mods or not keys:
            continue
        for f, _ in mods:
            if f not in moved:
                os.replace(top / f, dest / f)
                moved.add(f)
        plan[lay or "-"] = {"models": mods, "keys": list(keys)}
    if plan:
        (dest / "plan.json").write_text(json.dumps(plan))
    return len(plan)


def render(tris, png_path: Path, ini_path: Path) -> None:
    """The triangles, seen from above, filled in light grey on transparency, and the map.ini that puts world x/z on that picture as AC does:
    pixel = (x + X_OFFSET + MARGIN) / SCALE_FACTOR + PADDING (the site multiplies by its display width / WIDTH)."""
    if not tris:
        raise ValueError("no driveable surface found")
    x0 = x1 = tris[0]
    z0 = z1 = tris[1]
    for i in range(0, len(tris), 2):
        x, z = tris[i], tris[i + 1]
        x0, x1, z0, z1 = min(x0, x), max(x1, x), min(z0, z), max(z1, z)
    span = max(x1 - x0, z1 - z0, 1.0)
    scale = span / (MAX_PX - 2 * PAD_PX)   # metres per pixel
    w, h = int((x1 - x0) / scale) + 2 * PAD_PX + 1, int((z1 - z0) / scale) + 2 * PAD_PX + 1
    mask = bytearray(w * h)
    fill = MAX_FILL
    for j in range(0, len(tris), 6):   # edge-function fill of each triangle's bounding box
        t = tris[j:j + 6]
        ax, az = (t[0] - x0) / scale + PAD_PX, (t[1] - z0) / scale + PAD_PX
        bx, bz = (t[2] - x0) / scale + PAD_PX, (t[3] - z0) / scale + PAD_PX
        cx, cz = (t[4] - x0) / scale + PAD_PX, (t[5] - z0) / scale + PAD_PX
        area = (bx - ax) * (cz - az) - (bz - az) * (cx - ax)
        if abs(area) < 1e-9:
            continue
        minx, maxx = max(0, int(min(ax, bx, cx))), min(w - 1, int(max(ax, bx, cx)) + 1)
        minz, maxz = max(0, int(min(az, bz, cz))), min(h - 1, int(max(az, bz, cz)) + 1)
        fill -= (maxx - minx + 1) * (maxz - minz + 1)
        if fill < 0:
            raise ValueError("too much to draw")
        for py in range(minz, maxz + 1):
            row, y = py * w, py + 0.5
            for px in range(minx, maxx + 1):
                x = px + 0.5
                w0 = (bx - ax) * (y - az) - (bz - az) * (x - ax)
                w1 = (cx - bx) * (y - bz) - (cz - bz) * (x - bx)
                w2 = (ax - cx) * (y - cz) - (az - cz) * (x - cx)
                if (w0 >= 0 and w1 >= 0 and w2 >= 0) or (w0 <= 0 and w1 <= 0 and w2 <= 0):
                    mask[row + px] = 1
    raw = bytearray()
    for py in range(h):   # grey + alpha, filter byte 0 per row
        raw.append(0)
        row = mask[py * w:(py + 1) * w]
        raw += b"".join(b"\xe5\xff" if v else b"\x00\x00" for v in row)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 4, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    ini_path.write_text(f"[PARAMETERS]\nWIDTH={w}\nHEIGHT={h}\nMARGIN=0\nSCALE_FACTOR={scale:.6f}\nX_OFFSET={-x0:.3f}\nZ_OFFSET={-z0:.3f}\n"
                        f"DRAWING_SIZE=10\nPADDING={PAD_PX}\n")


def draw(stage_dir: Path, out_dir: Path) -> dict:
    """Every planned layout of a stage drawn into `out_dir/<layout or ->/`. Returns {layout: "ok" | the reason it was not}."""
    plan = json.loads((stage_dir / "plan.json").read_text())
    done = {}
    for lay, p in plan.items():
        try:
            if not _plain(lay) and lay != "-":
                raise ValueError("bad layout name")
            budget, tris = [MAX_TRIS], array("f")
            for f, pos in p["models"]:
                if not _plain(f):
                    raise ValueError("bad model file name")
                read_triangles(stage_dir / f, tuple(p["keys"]), tuple(pos), budget, tris)
            d = out_dir / lay
            d.mkdir(parents=True, exist_ok=True)
            render(tris, d / "map.png", d / "map.ini")   # (out_dir is the child's own folder in the stage: the manager publishes it whole)
            done[lay] = "ok"
        except (OSError, ValueError, struct.error, KeyError, TypeError) as e:
            done[lay] = str(e)[:200]
    return done


# --- the manager's side ------------------------------------------------------------------------------------------------------------
_lock = threading.Lock()        # the generations and every publish
_gen: dict[str, int] = {}       # track -> bumped by every new upload: a drawing of an older upload that ends late publishes nothing
_one = threading.Semaphore(1)   # one drawing at a time, whatever the number of uploads


def maps_dir(track: str) -> Path | None:
    from app.config import settings
    if not _plain(track):
        return None
    return Path(settings.data_dir) / "track_maps" / track


def generated(track: str, config: str, name: str) -> Path | None:
    """The drawn map.png / map.ini of a layout, if there is one."""
    d = maps_dir(track)
    if not d or (config and not _plain(config)) or name not in ("map.png", "map.ini"):
        return None
    p = d / (config or "-") / name
    return p if p.is_file() else None


def forget(track: str) -> None:
    """A new upload of the track: the maps drawn for the old one go (a good map.png in the new pack must win)."""
    d = maps_dir(track)
    with _lock:
        _gen[track] = _gen.get(track, 0) + 1
        if d:
            shutil.rmtree(d, ignore_errors=True)


def publish(track: str, gen: int, drawn: Path) -> bool:
    """Put the layouts drawn by the child (`drawn/<layout>/map.png + map.ini`) in place, if the track was not uploaded again meanwhile. Each layout folder
    is swapped whole (a reader sees the old pair or the new one)."""
    d = maps_dir(track)
    with _lock:
        if not d or _gen.get(track, 0) != gen:
            log.info("map of %s: drawn for an older upload, not published", track)
            return False
        n = 0
        for lay in sorted(drawn.iterdir()) if drawn.is_dir() else ():
            if lay.is_dir() and (lay / "map.png").is_file() and (lay / "map.ini").is_file() and (_plain(lay.name) or lay.name == "-"):
                d.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(d / lay.name, ignore_errors=True)
                os.replace(lay, d / lay.name)
                n += 1
        return n > 0


def _limits():   # (in the child, before it runs)
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEM, CHILD_MEM))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S, TIMEOUT_S + 5))


def cleanup(scratch: Path, older_s: float = 7200) -> None:
    """Stages left by a manager that stopped in the middle (their .kn5 are big)."""
    for d in scratch.glob("kn5-*"):
        try:
            if time.time() - d.stat().st_mtime > older_s:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def start(stage_dir: Path, track: str) -> threading.Thread | None:
    """Draw the maps of an installed track in a child process (time limit TIMEOUT_S), then delete the stage. Returns the thread (tests wait on it)."""
    if not maps_dir(track) or not (stage_dir / "plan.json").is_file():
        shutil.rmtree(stage_dir, ignore_errors=True)
        return None
    with _lock:
        gen = _gen.get(track, 0)

    def run():
        try:
            with _one:
                r = subprocess.run([sys.executable, "-m", "app.kn5map", str(stage_dir), str(stage_dir / "drawn")], capture_output=True, text=True,
                                   timeout=TIMEOUT_S, cwd=str(Path(__file__).resolve().parent.parent), preexec_fn=_limits)
            log.info("map of %s drawn from its .kn5: %s", track, (r.stdout or r.stderr).strip()[-500:])
            publish(track, gen, stage_dir / "drawn")
        except subprocess.TimeoutExpired:
            log.warning("map of %s: the .kn5 took longer than %d s, left as it was (AI line / traced lap)", track, TIMEOUT_S)
        except OSError:
            log.exception("map of %s could not be drawn", track)
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    t = threading.Thread(target=run, name=f"kn5map-{track}", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    print(json.dumps(draw(Path(sys.argv[1]), Path(sys.argv[2]))))
