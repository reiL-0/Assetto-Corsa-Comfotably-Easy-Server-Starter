"""The clock a CSP client shows is the command's timestamp read in the TRACK's local time zone (seen live: a time sent as 12:37 showed 23:37 on a Melbourne
track, UTC+11). So the director must send `wanted local time - the track's UTC offset` for the clients to read the time that was asked for.

`offset_seconds` finds that offset: an explicit IANA zone of the plan wins; otherwise the track's `geotags` (ui_track.json, degrees as «37°51′04″S» or «48.0061°N»)
are looked up with Open-Meteo (`timezone=auto`, the same free service the live weather uses; answers are cached). With no geotags or no answer it is 0 (UTC):
the time is then what it always was.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

URL = "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto&forecast_days=1&current=temperature_2m"
_cache: dict[tuple, int] = {}
_GEO = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*(?:(\d+(?:\.\d+)?)\s*[′']\s*)?(?:(\d+(?:\.\d+)?)\s*[″\"]\s*)?([NSEWnsew])?\s*$")


def parse_geo(v: str) -> float | None:
    """«37°51′04″S» -> -37.851…, «48.0061°N» -> 48.0061, «-6.5» -> -6.5; None if it is not a coordinate."""
    m = _GEO.match(str(v))
    if not m:
        return None
    deg = abs(float(m[1])) + float(m[2] or 0) / 60 + float(m[3] or 0) / 3600
    return -deg if m[1].startswith("-") or (m[4] or "").upper() in ("S", "W") else deg


def track_geo(tracks_dir: Path, track: str, config: str = "") -> tuple[float, float] | None:
    """(lat, lon) from the geotags of the layout's ui_track.json, else of the track's."""
    for f in ([tracks_dir / track / "ui" / config / "ui_track.json"] if config else []) + [tracks_dir / track / "ui" / "ui_track.json"]:
        try:
            tags = json.loads(f.read_text(encoding="utf-8-sig")).get("geotags") or []
        except (OSError, ValueError):
            continue
        if len(tags) == 2 and (lat := parse_geo(tags[0])) is not None and (lon := parse_geo(tags[1])) is not None:
            return lat, lon
    return None


def offset_seconds(tracks_dir: Path, track: str, config: str = "", tz_name: str | None = None, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    if tz_name:
        try:
            return int(ZoneInfo(tz_name).utcoffset(now.replace(tzinfo=None)).total_seconds())
        except (ZoneInfoNotFoundError, ValueError, AttributeError):
            return 0
    geo = track_geo(tracks_dir, track, config) if track else None
    if not geo:
        return 0
    key = (round(geo[0], 2), round(geo[1], 2), now.date())
    if key not in _cache:
        try:
            with urllib.request.urlopen(URL.format(lat=geo[0], lon=geo[1]), timeout=6) as r:
                _cache[key] = int(json.load(r)["utc_offset_seconds"])
        except (OSError, ValueError, KeyError):
            return 0   # not cached: tried again at the next start
    return _cache[key]
