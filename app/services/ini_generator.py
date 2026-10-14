"""Pure text generation of acServer's INI files from a server's stored data (no I/O, no settings, no DB).

`app.servers` keeps the old names (`_ports`, `_render_ini`, `render_server_cfg`, `render_entry_list`) as one-line wrappers that feed this the
manager's settings. Everything here takes its inputs explicitly, so it is testable without a database or a data dir.
"""
from __future__ import annotations

import configparser
import io

from app.schemas.servers import Scalar


def ports(base: int, range_start: int, range_end: int) -> dict[str, int]:
    """The ports of one 4-port block starting at `base`.

    plugin: acServer's own UDP_PLUGIN_LOCAL_PORT. plugin_local: our side of the ACSP socket (UDP_PLUGIN_ADDRESS), one pair per block.
    http: the port players and Content Manager use (the manager answers there, app/wake.py); http_internal: where acServer's own HTTP
    listens (outside the blocks, one per block, still open to the world: the game's UDP ping names it)."""
    internal = range_end + (base - range_start) // 4
    return {"tcp": base, "udp": base, "http": base + 1, "plugin": base + 2, "plugin_local": base + 3, "http_internal": internal}


def ini_value(v: Scalar) -> str:
    if isinstance(v, bool):  # bool before int: AC wants 1/0
        return "1" if v else "0"
    return str(v)


def render_ini(sections: dict[str, dict[str, Scalar]]) -> str:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # keep KEY casing
    for name, kv in sections.items():
        cp[name] = {k: ini_value(v) for k, v in kv.items()}
    buf = io.StringIO()
    cp.write(buf, space_around_delimiters=False)
    return buf.getvalue()


# acServer's own penalties, off: with the OPR hub (CSP) every penalty is ours, so the game must not add any of its own. No master switch exists: START_RULE 0 = the car is
# locked until the start (no jump-start penalty), RACE_GAS_PENALTY_DISABLED 1 = a cut only gets logged (no "lift the gas" penalty), ALLOWED_TYRES_OUT -1 = no cut limit.
GAME_PENALTIES_OFF = {"START_RULE": 0, "RACE_GAS_PENALTY_DISABLED": 1, "ALLOWED_TYRES_OUT": -1}


def server_cfg(config: dict[str, dict[str, Scalar]], p: dict[str, int], has_welcome: bool, game_penalties_off: bool = False) -> str:
    """server_cfg.ini with the allocated ports `p` merged into [SERVER] (user values win, except the game's own penalties when `game_penalties_off`: those are always off)."""
    sections = {name: dict(kv) for name, kv in config.items()}
    server = sections.setdefault("SERVER", {})
    server.setdefault("TCP_PORT", p["tcp"])
    server.setdefault("UDP_PORT", p["udp"])
    server["HTTP_PORT"] = p["http_internal"]   # the manager owns the public HTTP port
    server.setdefault("UDP_PLUGIN_LOCAL_PORT", p["plugin"])
    if has_welcome:
        server["WELCOME_MESSAGE"] = "cfg/welcome.txt"   # relative to the instance directory, acServer's working directory
    else:
        server.pop("WELCOME_MESSAGE", None)
    server.setdefault("UDP_PLUGIN_ADDRESS", f"127.0.0.1:{p['plugin_local']}")
    if game_penalties_off:
        server.update(GAME_PENALTIES_OFF)
    return render_ini(sections)


def entry_list(cars: list[dict[str, Scalar]]) -> str:
    return render_ini({f"CAR_{i}": car for i, car in enumerate(cars)})
