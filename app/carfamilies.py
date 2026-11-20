"""Car families: one logical car («Stock Car 2026 Camry») that is really several car folders, one per physics variant (intermedia, corta, road, oval).

`<data_dir>/car_families.json`: {"sc26_camry": {"name": "Stock Car 2026 Camry", "variants": {"oval": "sc26_camry_oval", "road": "sc26_camry_road"}}}.
Events, league rosters and setups keep the family id; the session's `variant` picks the folder when the session is applied (server_service.apply), and
a skin uploaded to a family is copied into every variant folder (content._install_skins). An id that is not a family goes through untouched.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from fastapi import HTTPException

from app.config import settings

VARIANT_MAX = 32
ID_RE = re.compile(r"[A-Za-z0-9_][\w.\-]{0,63}")   # what a car folder name may be: the id is also the car of the skin-upload URL
_save_lock = threading.Lock()   # two admins saving at once: one after the other, never a mix


def _file() -> Path:
    return Path(settings.data_dir) / "car_families.json"


def load() -> dict[str, dict]:
    """{id: {"name", "variants": {label: folder}}}; {} when there is no file. Read on every call: it is tiny and the admin edits it live."""
    try:
        raw = json.loads(_file().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"car_families.json unreadable: {e}") from e
    return raw if isinstance(raw, dict) else {}


def validate(fams: dict, cars_dir: Path) -> None:
    """What the file must satisfy: each family has >=1 variant folder that is installed, and no family id is also a car folder (it would be ambiguous)."""
    for fid, f in fams.items():
        if not ID_RE.fullmatch(fid):
            raise HTTPException(422, f"family id {fid!r}: letters, digits, _ . - only (it is used as a car folder name)")
        v = f.get("variants") if isinstance(f, dict) else None
        if not isinstance(v, dict) or not v or not all(isinstance(k, str) and k and len(k) <= VARIANT_MAX and isinstance(d, str) and d for k, d in v.items()):
            raise HTTPException(422, f"family {fid!r}: `variants` must be {{label: car folder}}, not empty")
        if (cars_dir / fid).exists():
            raise HTTPException(422, f"family {fid!r} has the name of an installed car folder")
        for label, folder in v.items():
            if "/" in folder or "\\" in folder or folder in (".", "..") or not (cars_dir / folder).is_dir() or (cars_dir / folder).is_symlink():   # (a link could point the skins out of the cars folder)
                raise HTTPException(422, f"family {fid!r}, variant {label!r}: car {folder!r} is not installed")


def save(fams: dict, cars_dir: Path) -> None:
    validate(fams, cars_dir)
    with _save_lock:
        tmp = _file().with_suffix(".tmp")
        tmp.write_text(json.dumps(fams, indent=1, ensure_ascii=False))
        tmp.replace(_file())


def folders(fid: str) -> list[str]:
    """Every variant folder of a family ([] if `fid` is not one)."""
    return list(((load().get(fid) or {}).get("variants") or {}).values())


def resolve(body):
    """`body` (a SessionIn) with each family id of cars / car_counts / car_setups / entries replaced by the folder of `body.variant`.
    A family with no (or an unknown) variant is refused; a body with no family is returned as it came."""
    fams = load()
    if not fams:
        return body
    if not ({*body.cars, *(body.car_counts or ()), *body.car_setups, *(e.model for e in body.entries)} & fams.keys()):
        return body
    def pick(c: str) -> str:
        f = fams.get(c)
        if not f:
            return c
        if body.variant not in f["variants"]:
            raise HTTPException(400, f"{c}: variant {body.variant!r} is not one of {', '.join(f['variants'])}")
        return f["variants"][body.variant]
    counts = None
    if body.car_counts is not None:
        counts = {}
        for c, n in body.car_counts.items():
            counts[pick(c)] = counts.get(pick(c), 0) + n
    return body.model_copy(update={
        "cars": list(dict.fromkeys(pick(c) for c in body.cars)),
        "car_counts": counts,
        "car_setups": {pick(c): s for c, s in body.car_setups.items()},
        "entries": [e.model_copy(update={"model": pick(e.model)}) for e in body.entries],
    })


def real_cars(session: dict) -> list[str]:
    """The car folders a stored session dict (an event's data) will run: for the announcements, which read the dict, not a SessionIn."""
    cars = session.get("cars") or sorted({e["model"] for e in session.get("entries", [])})
    fams, v = load(), session.get("variant", "")
    return [((fams.get(c) or {}).get("variants") or {}).get(v, c) for c in cars]
