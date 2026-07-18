"""The users REST API.

Users are core (every module's rows point at a user), so this lives in core/,
not in a feature module. Same shape as aegis/api.py: Pydantic validates the body,
the data-access layer in core/users.py does the SQL.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from athena.core import agent_commands, user_commands, users
from athena.core.deps import get_conn
from athena.core.identity import (
    admin_actor,
    current_actor,
    optional_actor,
    require_admin,
)
from athena.mcp.config import claude_mcp_config

router = APIRouter(prefix="/users", tags=["core"])

Role = Literal["admin", "member", "viewer"]


class UserCreate(BaseModel):
    email: str
    name: str
    # Optional: set it to enable browser login. Omit for agent/API-only users.
    password: str | None = None
    # Bootstrap ignores this and always creates the first user as admin.
    role: Role | None = None
    # Mark the account as an agent (display/audit distinction; grants nothing).
    is_agent: bool = False


class UserRoleUpdate(BaseModel):
    role: Role


class UserAgentUpdate(BaseModel):
    is_agent: bool


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: Role
    is_agent: bool
    created_at: str


class TokenRevocationOut(BaseModel):
    user_id: int
    revoked_token_count: int


class OffboardOut(BaseModel):
    user_id: int
    role: Role
    revoked_session_count: int
    revoked_token_count: int


class AgentOnboard(BaseModel):
    email: str
    name: str
    # REQUIRED: an agent's first credential must say what it may do — the same
    # least-privilege rule as POST /tokens (no omitted-scopes fail-open).
    scopes: list[Literal["read", "issue:write", "docs:write", "admin"]]
    # Defaults to the agent's name.
    token_name: str | None = None


class OnboardedTokenOut(BaseModel):
    id: int
    name: str
    scopes: list[str]
    # The raw secret — shown once, on onboarding, and never again.
    token: str


class AgentOnboardOut(BaseModel):
    user: UserOut
    token: OnboardedTokenOut
    # Ready-to-paste MCP client config wired to this deployment and the new token.
    mcp_config: dict


class UserMeOut(UserOut):
    # The acting bearer token's effective scopes, already normalized (an admin
    # token collapses to ["admin"]). None when the request is NOT token-authenticated
    # — a browser session or the trusted actor-header path is not scope-limited, the
    # same semantics token_has_scope relies on, so the caller can tell "no token cap"
    # apart from "a token that happens to allow everything".
    scopes: list[str] | None = None


@router.post("", response_model=UserOut, status_code=201)
def create(
    payload: UserCreate,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Bootstrap exception: the very first user is created without authentication,
    # because on a fresh install there is nobody to authenticate as yet. The first
    # user is always admin so a deploy cannot accidentally lock itself out. After
    # bootstrap, only admins may add users or assign roles.
    existing = users.count_users(conn)
    if existing == 0:
        role = users.BOOTSTRAP_ROLE
        # No authenticated actor exists yet — the bootstrap user is self-attributed.
        actor_id = None
    else:
        if actor is None:
            raise HTTPException(status_code=401, detail="authentication required")
        require_admin(actor)
        role = payload.role or users.DEFAULT_ROLE
        actor_id = actor["id"]
    # The command owns the insert AND the atomic 'created_user' audit event, so a new
    # actor entering the system is never a silent write.
    try:
        return user_commands.create_user(
            conn,
            actor_id=actor_id,
            email=payload.email,
            name=payload.name,
            password=payload.password,
            role=role,
            is_agent=payload.is_agent,
        )
    except sqlite3.IntegrityError:
        # email collides with an existing user — reject at the boundary.
        raise HTTPException(status_code=400, detail="email already in use")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[UserOut])
def index(
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Listing users is an authenticated operation — don't let an exposed instance
    # be enumerated anonymously.
    return users.list_users(conn)


@router.get("/me", response_model=UserMeOut)
def me(actor: dict = Depends(current_actor)) -> dict:
    """Who am I? Returns the authenticated user's identity, role, and agent flag,
    plus the acting token's effective scopes — so an agent holding only a bearer
    token can discover who it is and what it may do without first provoking a 403.

    No DB read: current_actor has already resolved the full actor (and tokens.py
    stamped the token's scopes onto it), so this just reshapes what is in hand.
    `scopes` is null for non-token auth (a browser session or the trusted
    actor-header path), which is not scope-limited. The response_model trims the
    actor dict to the declared fields, so internal keys (_token_id, _token_scopes)
    and the password hash never leak.

    Declared before /{user_id} so "me" is matched here, not as a user id."""
    scopes = actor.get("_token_scopes")
    return {**actor, "scopes": list(scopes) if scopes is not None else None}


@router.post("/onboard_agent", response_model=AgentOnboardOut, status_code=201)
def onboard_agent(
    payload: AgentOnboard,
    request: Request,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Provision a working agent in one call: create the agent user (member,
    token-only — no password) and mint its first scoped token, atomically
    audited and attributed to the acting admin. The response carries the
    one-time raw token plus a ready-to-paste MCP config block, so "add an agent
    teammate" is a single request instead of three manual steps.

    Declared before the /{user_id} routes so the literal path wins."""
    try:
        result = agent_commands.onboard_agent(
            conn,
            actor=actor,
            email=payload.email,
            name=payload.name,
            scopes=payload.scopes,
            token_name=payload.token_name,
        )
    except agent_commands.AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "user": result["user"],
        "token": result["token"],
        "mcp_config": claude_mcp_config(
            base_url=str(request.base_url).rstrip("/"),
            token=result["token"]["token"],
        ),
    }


@router.get("/{user_id}", response_model=UserOut)
def show(
    user_id: int,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    if actor["id"] != user_id:
        require_admin(actor)
    user = users.get_user(conn, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="no such user")
    return user


@router.put("/{user_id}/role", response_model=UserOut)
def update_role(
    user_id: int,
    payload: UserRoleUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The command owns the last-admin guard, the role normalization, and — new — the
    # atomic 'changed_role' audit event, so a privilege change is never a silent write.
    try:
        return user_commands.set_user_role(
            conn, actor_id=actor["id"], target_user_id=user_id, role=payload.role
        )
    except user_commands.UserCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.put("/{user_id}/agent", response_model=UserOut)
def update_agent(
    user_id: int,
    payload: UserAgentUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Marking accounts as agents is an admin concern: it shapes how the rest of the
    # team reads activity and the delegation list, so it shouldn't be self-serve.
    try:
        return user_commands.set_user_agent(
            conn,
            actor_id=actor["id"],
            target_user_id=user_id,
            is_agent=payload.is_agent,
        )
    except user_commands.UserCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.delete("/{user_id}/tokens", response_model=TokenRevocationOut)
def revoke_user_tokens(
    user_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Admin kill switch: revoke EVERY live token a user holds. Unlike the
    self-service DELETE /tokens/{id} (owner-scoped), this reaches another user's
    credentials, so it is admin-only — the lever to lock out a compromised or
    runaway agent. Idempotent and audited."""
    try:
        return agent_commands.revoke_agent_tokens(
            conn, actor=actor, target_user_id=user_id
        )
    except agent_commands.AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{user_id}/offboard", response_model=OffboardOut)
def offboard(
    user_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Admin one-click offboard: demote the user to viewer, revoke every session,
    and revoke every token — one audited action. Refuses to strip the last admin."""
    try:
        return agent_commands.offboard_user(
            conn, actor=actor, target_user_id=user_id
        )
    except agent_commands.AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
