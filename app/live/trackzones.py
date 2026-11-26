"""The zones of a track for the director's sector flags: the track's own `data/sections.ini` (what its author named: [SECTION_n] IN=0.055 OUT=0.080 TEXT=Jones, IN/OUT = fraction
of the lap; OUT < IN = the zone crosses the line); a track without it (many have none: the file is optional) gets the lap in three equal parts, "Sector 1/2/3".

Read in a request thread (app/racecontrol.py `refresh_zones`), never under the race control lock. The names reach every driver's hub, which reads the state with Lua patterns: no quotes,
backslashes or control characters in them.
"""
import configparser
import math
import re
from pathlib import Path

from app.content import _tracks_dir
from app.pitentry import NAME

MAX_BYTES = 32 << 10   # a real sections.ini is a few KB
MAX_ZONES = 64
FALLBACK = [{"name": f"Sector {i + 1}", "from": round(i / 3, 4), "to": round((i + 1) / 3, 4)} for i in range(3)]
_BAD = re.compile(r'["\\\[\]{}\x00-\x1f\x7f]')   # (also brackets and braces: the hub cuts the list at the first «]»)


def _fraction(v: str) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if math.isfinite(f) and 0 <= f <= 1 else None


def parse(text: str) -> list[dict]:
    """The valid zones of a sections.ini, in their number's order ([] when none is)."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read_string(text)
    except configparser.Error:
        return []
    out = []
    for sec in sorted((s for s in cp.sections() if re.fullmatch(r"SECTION_\d+", s.upper())), key=lambda s: int(s.split("_")[1])):
        a, b = _fraction(cp[sec].get("IN")), _fraction(cp[sec].get("OUT"))
        name = _BAD.sub("", cp[sec].get("TEXT", "")).strip()[:40]
        if a is None or b is None or a == b or not name:
            continue
        out.append({"name": name, "from": a, "to": b})
        if len(out) == MAX_ZONES:
            break
    return out


def zones(track: str, config: str = "") -> list[dict]:
    """The zones of a layout of the server (`config` "" = the track itself); the three equal parts when it has no usable sections.ini. Never raises: the state every hub reads depends on it."""
    if not NAME.fullmatch(track or "") or (config and not NAME.fullmatch(config)) or ".." in track + config:
        return FALLBACK
    try:
        root = _tracks_dir().resolve()
        f = (root / track / config / "data" / "sections.ini") if config else (root / track / "data" / "sections.ini")
        real = f.resolve()
        if not real.is_relative_to(root) or not real.is_file():   # (a link out of the content folder is not read)
            return FALLBACK
        with real.open("rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return FALLBACK
        return parse(raw.decode("utf-8", "replace")) or FALLBACK
    except OSError:
        return FALLBACK


if __name__ == "__main__":
    z = parse("[SECTION_1]\nIN=0.2\nOUT=0.3\nTEXT=Tosa\n[SECTION_0]\nIN=0.95\nOUT=0.05\nTEXT=Rettifilo \"x\"\n[SECTION_2]\nIN=2\nOUT=0.4\nTEXT=bad\n")
    assert z == [{"name": "Rettifilo x", "from": 0.95, "to": 0.05}, {"name": "Tosa", "from": 0.2, "to": 0.3}], z
    assert parse("nonsense") == [] and zones("../etc") == FALLBACK and len(FALLBACK) == 3 and FALLBACK[2]["to"] == 1.0
    print("trackzones ok")
