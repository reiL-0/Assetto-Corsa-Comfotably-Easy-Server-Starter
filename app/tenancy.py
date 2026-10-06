"""Customers (tenants), their plans and what an account of theirs may touch.

One choke point: `check_scope`, called by `auth.current_user` for every authenticated request. Our own staff (no `tenant_id`, no per-server token) are not
restricted. A **customer's** account (`User.tenant_id`) or a **per-server token** (`Token.server_id`) gets a whitelist of routes; anything not listed is a 403 —
a new route is closed to customers until it is added here on purpose. Server routes also check ownership (404, never 403, so ids do not leak).

- Customer account: its own servers (list, create up to the plan, config, start/stop, results…), `POST /binaries/verify`, its own tokens. Not: users, plans,
  championships, leagues, the shared content folder (until content is kept per tenant), `limits` and `binary` of a server (those come from the plan).
- Per-server token: the same on ONE server, no creating or deleting servers.
- Slots, CPU and RAM come from the plan (`limits_for`, `clamp_config`), ports are forced to the server's own block, config values cannot carry newlines.
"""

from __future__ import annotations

import re

from fastapi import HTTPException
from fastapi.requests import HTTPConnection
from sqlmodel import Session, select

from app.models import Plan, Server, Tenant, Token, User

API = ""   # the route templates below are as FastAPI reports them, without the /api/v1 prefix (stripped in `check_scope` when present)
# (methods or None for any, regex of the route TEMPLATE, e.g. /servers/{server_id}/start)
ALLOW: list[tuple[set[str] | None, re.Pattern]] = [
    ({"GET", "POST"}, re.compile(rf"^{API}/servers$")),
    (None, re.compile(rf"^{API}/servers/\{{server_id\}}$")),
    (None, re.compile(rf"^{API}/servers/\{{server_id\}}/.+$")),
    ({"POST"}, re.compile(rf"^{API}/binaries/verify$")),
    (None, re.compile(rf"^{API}/tenant/content(/.*)?$")),   # its own cars and tracks (app/tenantcontent.py)
    (None, re.compile(rf"^{API}/auth/(me|logout|tokens|tokens/\{{token_id\}})$")),
]
SERVER_ONLY_DENY = re.compile(rf"^{API}/servers/\{{server_id\}}/(limits|binary)$")   # set by the plan / the operator


def _forbidden(msg: str = "not available to this account") -> HTTPException:
    return HTTPException(403, msg)


def check_scope(conn: HTTPConnection, sess: Session, user: User, token: Token) -> None:
    if user.tenant_id is None and token.server_id is None:
        return
    tenant = sess.get(Tenant, user.tenant_id) if user.tenant_id is not None else None
    if user.tenant_id is not None and (not tenant or tenant.status != "active"):
        raise _forbidden("this account is suspended")
    route = getattr(conn.scope.get("route"), "path", "").removeprefix("/api/v1")
    method = conn.scope.get("method", "GET")
    if not any((m is None or method in m) and rx.match(route) for m, rx in ALLOW) or SERVER_ONLY_DENY.match(route):
        raise _forbidden()
    sid = conn.path_params.get("server_id")
    if sid is not None:
        s = sess.get(Server, int(sid))
        if not s or (token.server_id is not None and s.id != token.server_id) or (user.tenant_id is not None and s.tenant_id != user.tenant_id):
            raise HTTPException(404, "server not found")
    elif route == f"{API}/servers" and method == "POST" and token.server_id is not None:
        raise _forbidden("a per-server token cannot create servers")
    if route == f"{API}/servers/{{server_id}}" and method == "DELETE" and token.server_id is not None:
        raise _forbidden("a per-server token cannot delete its server")
    conn.state.scope = {"tenant_id": user.tenant_id, "server_id": token.server_id}


def visible(conn: HTTPConnection, servers: list[Server]) -> list[Server]:
    """The servers a list route may show to the caller."""
    sc = getattr(conn.state, "scope", None)
    if not sc:
        return servers
    return [s for s in servers if (sc["tenant_id"] is None or s.tenant_id == sc["tenant_id"]) and (sc["server_id"] is None or s.id == sc["server_id"])]


def plan_of(sess: Session, s: Server) -> Plan | None:
    t = sess.get(Tenant, s.tenant_id) if s.tenant_id is not None else None
    return sess.get(Plan, t.plan_id) if t else None


def limits_for(sess: Session, s: Server) -> tuple[int | None, int | None]:
    """(cpu_percent, mem_mb) a server starts with: the plan's for a customer's, its own for ours."""
    p = plan_of(sess, s)
    return (p.cpu_percent, p.mem_mb) if p else (s.cpu_limit, s.mem_limit_mb)


def enforce_new_server(sess: Session, user: User) -> Plan | None:
    """Called when a customer creates a server: its tenant must be active and under `max_servers`. Returns the plan (None for our staff)."""
    if user.tenant_id is None:
        return None
    t = sess.get(Tenant, user.tenant_id)
    p = sess.get(Plan, t.plan_id) if t else None
    if not t or not p or t.status != "active":
        raise _forbidden("this account is suspended")
    if len(list(sess.exec(select(Server.id).where(Server.tenant_id == t.id)))) >= p.max_servers:
        raise _forbidden(f"your plan allows {p.max_servers} server(s)")
    return p


_SEGMENT = re.compile(r"^[A-Za-z0-9_.\- ]{1,80}$")


def clean_text(v: str) -> str:
    """No line breaks or control characters in an INI value or name (they could add keys to the file acServer reads)."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v))


def clamp_config(sections: dict[str, dict], entries: list[dict], plan: Plan, ports: dict[str, int]) -> tuple[dict, list[dict]]:
    """A customer's config as acServer may read it: our ports, slots within the plan, content names that are plain folder names."""
    server = sections.setdefault("SERVER", {})
    server["TCP_PORT"], server["UDP_PORT"], server["HTTP_PORT"] = ports["tcp"], ports["udp"], ports["http_internal"]
    server["UDP_PLUGIN_LOCAL_PORT"], server["UDP_PLUGIN_ADDRESS"] = ports["plugin"], f"127.0.0.1:{ports['plugin_local']}"
    for k in ("REGISTER_TO_LOBBY",):
        server[k] = 0   # a customer server is not published with our IP
    try:
        server["MAX_CLIENTS"] = max(1, min(int(server.get("MAX_CLIENTS", plan.slots)), plan.slots))
    except (TypeError, ValueError):
        server["MAX_CLIENTS"] = plan.slots
    names = [server.get("TRACK", ""), server.get("CONFIG_TRACK", ""), *str(server.get("CARS", "")).split(";")]
    names += [e.get(k, "") for e in entries for k in ("MODEL", "SKIN")]
    for n in names:
        if str(n) and not _SEGMENT.match(str(n)) or str(n) in (".", ".."):
            raise HTTPException(400, f"invalid content name: {str(n)[:40]!r}")
    return sections, entries[: plan.slots]
