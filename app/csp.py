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


def extra_for(server_id: int, extra: str) -> str:
    """The extra options a server really sends: its own (`Server.csp_extra`) plus the OPR hub script, always (flags, penalties and track-limit reports of every session do not depend on
    anyone having pasted a section). It takes the first free `[SCRIPT_n]` from 100 up so it never replaces a script the admin added."""
    if not settings.csp_hub:
        return extra
    n = 100
    while re.search(rf"^\s*\[SCRIPT_{n}\]", extra, re.M | re.I):
        n += 1
    hub = f'[SCRIPT_{n}]\nSCRIPT = "{settings.csp_hub_url}?v={settings.csp_hub_version}"\nSERVER = {server_id}\n'
    return (extra.strip() + "\n\n" if extra.strip() else "") + hub


def welcome_with_extra(welcome: str, extra: str) -> str:
    return welcome + SEPARATOR + encode(extra.strip() + "\n") if extra.strip() else welcome
