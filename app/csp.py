"""Custom Shaders Patch "server extra options": a small INI the server hands to CSP clients inside the welcome message.

CSP reads it from the hidden tail of the welcome text (documented by CSP's authors, https://cup.acstuff.club/docs/csp/misc/server-extra-options):
the INI as UTF-8, zlib-compressed, base64 without the `=` padding, behind 32 tabs and `$CSP0:`. The original game shows only the welcome text
(the tabs push the rest out of sight), CSP clients apply the options. `Server.csp_extra` holds the INI; `servers._write_instance` writes
`welcome_with_extra(welcome, csp_extra)` to `cfg/welcome.txt`. Used for `[SCRIPT_n]` (an online Lua script the clients download from a
URL), `[EXTRA_RULES]`, `[WEATHER_FX]`...
"""

from __future__ import annotations

import base64
import re
import zlib

from app.config import settings

SEPARATOR = "\t" * 32 + "$CSP0:"


def encode(extra: str) -> str:
    return base64.b64encode(zlib.compress(extra.encode("utf-8"))).decode().rstrip("=")


def decode(text: str) -> str:
    """The INI hidden at the end of a welcome message ('' when there is none); the inverse of `encode`, for tests and for reading what a server sends."""
    _, sep, tail = text.partition(SEPARATOR)
    return zlib.decompress(base64.b64decode(tail + "=" * (-len(tail) % 4))).decode("utf-8") if sep else ""


def server_listed(server_id: int, spec: str) -> bool:
    """Whether a server is named by a setting such as ACM_CSP_TRAINER_SERVERS: `*` = every server (also the ones made later), `2` or `2, 3` = those, empty = nobody, and `!2` takes one
    out (`*,!2` = every server but 2: the brake for a single server). An exclusion alone turns nothing on, and it wins over a listing. Ids, never substrings."""
    items = {x.strip() for x in spec.split(",") if x.strip()}
    sid = str(server_id)
    return f"!{sid}" not in items and ("*" in items or sid in items)


def scripts_off(csp_off: str) -> set[str]:
    """`Server.csp_off`: the scripts the admin switched off for one server, as `hub,trainer` ('' = all on, so a server made later is born with them)."""
    return {x.strip() for x in csp_off.split(",") if x.strip()}


def hub_enabled(csp_off: str = "") -> bool:
    """Whether this server sends the OPR hub. It also decides if the game keeps its own penalties (ini_generator.server_cfg): with no hub nobody else applies them."""
    return settings.csp_hub and "hub" not in scripts_off(csp_off)


def extra_for(server_id: int, extra: str, csp_off: str = "") -> str:
    """The extra options a server really sends: its own (`Server.csp_extra`) plus the OPR scripts, each one switched off per server by `csp_off` (hub: flags, penalties and track-limit reports of every
    session, so no one has to paste a section; trainer: the lap trainer that also SENDS the driver's telemetry and asks for its token with OPRHELLO, every server by default, the ones made later too).
    The probe (temporary) only goes to the servers ACM_CSP_PROBE_SERVERS names. Each takes the first free `[SCRIPT_n]` from 100 up so it never replaces a script the admin added.
    ACM_CSP_HUB=false is the master switch: nothing of OPR is sent."""
    if not settings.csp_hub:
        return extra
    off = scripts_off(csp_off)
    scripts = []   # (switch of the checkbox, ACM_CSP_*_SERVERS selector, script URL, extra lines)
    scripts.append(("hub", "*", f"{settings.csp_hub_url}?v={settings.csp_hub_version}", f"SERVER = {server_id}\n"))
    scripts.append(("probe", settings.csp_probe_servers, settings.csp_probe_url, ""))
    scripts.append(("trainer", settings.csp_trainer_servers, f"{settings.csp_trainer_url}?v={settings.csp_trainer_version}", ""))
    out, n = [], 100
    for key, servers, url, more in scripts:
        if key in off or not server_listed(server_id, servers):
            continue
        while re.search(rf"^\s*\[SCRIPT_{n}\]", extra, re.M | re.I):
            n += 1
        out.append(f'[SCRIPT_{n}]\nSCRIPT = "{url}"\n{more}')
        n += 1
    return (extra.strip() + "\n\n" if extra.strip() and out else extra) + "\n".join(out)


def welcome_with_extra(welcome: str, extra: str) -> str:
    return welcome + SEPARATOR + encode(extra.strip() + "\n") if extra.strip() else welcome
