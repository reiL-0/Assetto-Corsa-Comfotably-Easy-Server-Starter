"""Auth + RBAC. One `tokens` table backs both cookie sessions (login) and Bearer API tokens."""

import hashlib
import hmac
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.requests import HTTPConnection
from pydantic import BaseModel, Field
from sqlmodel import select

from app import tenancy
from app.db import SessionDep
from app.models import Token, User

Role = Literal["driver", "steward", "admin"]
ROLES = get_args(Role)  # ascending privilege
COOKIE = "acm_session"
SESSION_DAYS = 30

router = APIRouter(tags=["auth"])


def _hash_pw(pw: str, salt: bytes) -> str:
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def _check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(_hash_pw(pw, bytes.fromhex(stored.split("$")[0])), stored)


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _raw_token(conn: HTTPConnection) -> str | None:
    auth = conn.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else conn.cookies.get(COOKIE)


def _find_token(sess: SessionDep, raw: str | None) -> Token | None:
    if not raw:
        return None
    t = sess.exec(select(Token).where(Token.token_hash == _sha(raw))).first()
    if t and t.expires_at and t.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        return None
    return t


def current_user(conn: HTTPConnection, sess: SessionDep) -> User:
    t = _find_token(sess, _raw_token(conn))
    if not t:
        raise HTTPException(401, "not authenticated", headers={"WWW-Authenticate": "Bearer"})
    user = sess.get(User, t.user_id)
    tenancy.check_scope(conn, sess, user, t)   # a customer's account or a per-server token only reaches its own servers (403/404 otherwise)
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def _need(user: User, role: Role) -> User:
    if ROLES.index(user.role) < ROLES.index(role):
        raise HTTPException(403, f"{role} role required")
    return user


def require(role: Role):
    """Dependency: caller must have at least `role`."""

    def dep(user: CurrentUser) -> User:
        return _need(user, role)

    return dep


def guard(read: Role = "driver"):
    """Router-level default: GET/HEAD/websocket need `read`, every other method needs admin.
    Fail-safe: a new write route is admin-only until it is deliberately moved.
    """

    def dep(conn: HTTPConnection, user: CurrentUser) -> User:
        reading = conn.scope.get("method", "GET") in ("GET", "HEAD")
        return _need(user, read if reading else "admin")

    return dep


def _issue(sess: SessionDep, user: User, name: str, ttl: timedelta | None, server_id: int | None = None) -> tuple[Token, str]:
    raw = secrets.token_urlsafe(32)
    t = Token(
        user_id=user.id,
        token_hash=_sha(raw),
        name=name,
        expires_at=datetime.now(UTC) + ttl if ttl else None,
        server_id=server_id,
    )
    sess.add(t)
    sess.commit()
    sess.refresh(t)
    return t, raw


class UserOut(BaseModel):
    id: int
    username: str
    role: Role
    discord_id: str | None = None
    tenant_id: int | None = None
    timezone: str


class UserIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: Role = "driver"


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserIn, conn: HTTPConnection, sess: SessionDep) -> User:
    # Bootstrap: while no users exist this is open and the caller becomes admin.
    first = sess.exec(select(User)).first() is None
    if not first:
        _need(current_user(conn, sess), "admin")
    if sess.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(409, "username taken")
    u = User(
        username=body.username,
        password_hash=_hash_pw(body.password, secrets.token_bytes(16)),
        role="admin" if first else body.role,
    )
    sess.add(u)
    sess.commit()
    sess.refresh(u)
    return u


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require("admin"))])
def list_users(sess: SessionDep) -> list[User]:
    return list(sess.exec(select(User)))


class RoleIn(BaseModel):
    role: Role


@router.patch("/users/{user_id}", response_model=UserOut, dependencies=[Depends(require("admin"))])
def patch_user(user_id: int, body: RoleIn, sess: SessionDep) -> User:
    u = sess.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.role = body.role
    sess.add(u)
    sess.commit()
    return u


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login", response_model=UserOut)
def login(body: LoginIn, resp: Response, sess: SessionDep) -> User:
    u = sess.exec(select(User).where(User.username == body.username)).first()
    if not u or not u.password_hash or not _check_pw(body.password, u.password_hash):
        raise HTTPException(401, "bad credentials")
    _, raw = _issue(sess, u, "session", timedelta(days=SESSION_DAYS))
    # SameSite=Lax is the CSRF guard; a cross-origin frontend should use a Bearer token instead.
    resp.set_cookie(COOKIE, raw, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")
    return u


@router.post("/auth/logout", status_code=204)
def logout(conn: HTTPConnection, resp: Response, sess: SessionDep) -> None:
    if t := _find_token(sess, _raw_token(conn)):
        sess.delete(t)
        sess.commit()
    resp.delete_cookie(COOKIE)


@router.get("/auth/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user


class TimezoneIn(BaseModel):
    timezone: str  # IANA name, e.g. what the browser reports with Intl.DateTimeFormat().resolvedOptions().timeZone


@router.patch("/auth/me", response_model=UserOut)
def set_timezone(body: TimezoneIn, user: CurrentUser, sess: SessionDep) -> User:
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "unknown time zone")
    user.timezone = body.timezone
    sess.add(user)
    sess.commit()
    return user


class TokenIn(BaseModel):
    name: str = Field(min_length=1)


@router.post("/auth/tokens", status_code=201)
def create_token(body: TokenIn, user: CurrentUser, sess: SessionDep) -> dict:
    """Bearer API token with the caller's role. The plaintext is shown once."""
    t, raw = _issue(sess, user, body.name, None)
    return {"id": t.id, "name": t.name, "token": raw}


@router.get("/auth/tokens")
def list_tokens(user: CurrentUser, sess: SessionDep) -> list[dict]:
    rows = sess.exec(select(Token).where(Token.user_id == user.id))
    return [{"id": t.id, "name": t.name, "expires_at": t.expires_at and t.expires_at.replace(tzinfo=UTC)} for t in rows]   # SQLite hands expires_at back naive: say it is UTC


@router.delete("/auth/tokens/{token_id}", status_code=204)
def revoke_token(token_id: int, user: CurrentUser, sess: SessionDep) -> None:
    t = sess.get(Token, token_id)
    if not t or t.user_id != user.id:
        raise HTTPException(404, "token not found")
    sess.delete(t)
    sess.commit()
