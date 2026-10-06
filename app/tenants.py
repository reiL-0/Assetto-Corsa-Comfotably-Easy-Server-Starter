"""Plans, tenants (customers) and per-server tokens: the management API of the hosting service.

Plans and tenants are ours alone (`global_admin`: an admin without a tenant). A tenant's users are created here with `tenant_id` set and only ever reach
their own servers (app/tenancy.py). A per-server token is minted by an admin of that server (or by us): it carries the creator's role but works on that one
server and cannot create or delete servers.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import HTTPConnection
from pydantic import BaseModel, Field
from sqlmodel import select

from app.auth import CurrentUser, Role, UserOut, _hash_pw, _issue, require
from app.db import SessionDep
from app.models import Plan, Server, Tenant, Token, User

router = APIRouter(tags=["tenants"])


def global_admin(user: CurrentUser) -> User:
    if user.role != "admin" or user.tenant_id is not None:
        raise HTTPException(403, "only our own administrators manage plans and customers")
    return user


GlobalAdmin = Annotated[User, Depends(global_admin)]


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    max_servers: int = Field(default=1, ge=1, le=1000)
    slots: int = Field(default=16, ge=1, le=50)
    cpu_percent: int | None = Field(default=None, ge=10, le=800)
    mem_mb: int | None = Field(default=None, ge=256, le=65536)
    disk_mb: int | None = Field(default=None, ge=100)
    panel_enabled: bool = True


@router.get("/plans", dependencies=[Depends(global_admin)])
def list_plans(sess: SessionDep) -> list[Plan]:
    return list(sess.exec(select(Plan).order_by(Plan.id)))


@router.post("/plans", status_code=201, dependencies=[Depends(global_admin)])
def create_plan(body: PlanIn, sess: SessionDep) -> Plan:
    if sess.exec(select(Plan).where(Plan.name == body.name)).first():
        raise HTTPException(409, "plan name taken")
    p = Plan(**body.model_dump())
    sess.add(p)
    sess.commit()
    sess.refresh(p)
    return p


@router.put("/plans/{plan_id}", dependencies=[Depends(global_admin)])
def update_plan(plan_id: int, body: PlanIn, sess: SessionDep) -> Plan:
    p = sess.get(Plan, plan_id)
    if not p:
        raise HTTPException(404, "plan not found")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    sess.add(p)
    sess.commit()
    sess.refresh(p)
    return p   # slots/CPU/RAM apply to its customers' servers the next time they start


@router.delete("/plans/{plan_id}", status_code=204, dependencies=[Depends(global_admin)])
def delete_plan(plan_id: int, sess: SessionDep) -> None:
    if sess.exec(select(Tenant).where(Tenant.plan_id == plan_id)).first():
        raise HTTPException(409, "customers are on this plan")
    p = sess.get(Plan, plan_id)
    if p:
        sess.delete(p)
        sess.commit()


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    plan_id: int


class TenantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    plan_id: int | None = None
    status: Literal["active", "suspended"] | None = None


@router.get("/tenants", dependencies=[Depends(global_admin)])
def list_tenants(sess: SessionDep) -> list[dict]:
    out = []
    for t in sess.exec(select(Tenant).order_by(Tenant.id)):
        out.append({**t.model_dump(mode="json"), "servers": len(list(sess.exec(select(Server.id).where(Server.tenant_id == t.id)))),
                    "users": len(list(sess.exec(select(User.id).where(User.tenant_id == t.id))))})
    return out


@router.post("/tenants", status_code=201, dependencies=[Depends(global_admin)])
def create_tenant(body: TenantIn, sess: SessionDep) -> Tenant:
    if not sess.get(Plan, body.plan_id):
        raise HTTPException(404, "plan not found")
    if sess.exec(select(Tenant).where(Tenant.name == body.name)).first():
        raise HTTPException(409, "customer name taken")
    t = Tenant(**body.model_dump())
    sess.add(t)
    sess.commit()
    sess.refresh(t)
    return t


@router.patch("/tenants/{tenant_id}", dependencies=[Depends(global_admin)])
def patch_tenant(tenant_id: int, body: TenantPatch, sess: SessionDep) -> Tenant:
    t = sess.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "customer not found")
    if body.plan_id is not None and not sess.get(Plan, body.plan_id):
        raise HTTPException(404, "plan not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(t, k, v)
    sess.add(t)
    sess.commit()
    sess.refresh(t)
    return t


@router.delete("/tenants/{tenant_id}", status_code=204, dependencies=[Depends(global_admin)])
def delete_tenant(tenant_id: int, sess: SessionDep) -> None:
    if sess.exec(select(Server).where(Server.tenant_id == tenant_id)).first() or sess.exec(select(User).where(User.tenant_id == tenant_id)).first():
        raise HTTPException(409, "the customer still has servers or users: remove them first")
    t = sess.get(Tenant, tenant_id)
    if t:
        sess.delete(t)
        sess.commit()


class TenantUserIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: Role = "admin"


@router.post("/tenants/{tenant_id}/users", response_model=UserOut, status_code=201, dependencies=[Depends(global_admin)])
def create_tenant_user(tenant_id: int, body: TenantUserIn, sess: SessionDep) -> User:
    if not sess.get(Tenant, tenant_id):
        raise HTTPException(404, "customer not found")
    if sess.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(409, "username taken")
    import secrets
    u = User(username=body.username, password_hash=_hash_pw(body.password, secrets.token_bytes(16)), role=body.role, tenant_id=tenant_id)
    sess.add(u)
    sess.commit()
    sess.refresh(u)
    return u


# --- per-server tokens: /servers/{id}/tokens (open to the admin of that server; the ownership check is in tenancy.check_scope) ---------------------

class ServerTokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


def _not_a_server_token(conn: HTTPConnection) -> None:
    if getattr(conn.state, "scope", {}) and conn.state.scope.get("server_id") is not None:
        raise HTTPException(403, "a per-server token cannot manage tokens")


@router.post("/servers/{server_id}/tokens", status_code=201, dependencies=[Depends(require("admin"))])
def create_server_token(server_id: int, body: ServerTokenIn, conn: HTTPConnection, user: CurrentUser, sess: SessionDep) -> dict:
    """A token for this server only (the plaintext is shown once). Same role as you, but it cannot reach other servers or create/delete servers."""
    _not_a_server_token(conn)
    t, raw = _issue(sess, user, body.name, None, server_id=server_id)
    return {"id": t.id, "name": t.name, "server_id": server_id, "token": raw}


@router.get("/servers/{server_id}/tokens", dependencies=[Depends(require("admin"))])
def list_server_tokens(server_id: int, conn: HTTPConnection, sess: SessionDep) -> list[dict]:
    _not_a_server_token(conn)
    return [{"id": t.id, "name": t.name, "user_id": t.user_id} for t in sess.exec(select(Token).where(Token.server_id == server_id))]


@router.delete("/servers/{server_id}/tokens/{token_id}", status_code=204, dependencies=[Depends(require("admin"))])
def revoke_server_token(server_id: int, token_id: int, conn: HTTPConnection, sess: SessionDep) -> None:
    _not_a_server_token(conn)
    t = sess.get(Token, token_id)
    if not t or t.server_id != server_id:
        raise HTTPException(404, "token not found")
    sess.delete(t)
    sess.commit()
