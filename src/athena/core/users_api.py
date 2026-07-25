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

from athena.core import agent_commands, approvals, budgets, user_commands, users
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


class UserPasswordReset(BaseModel):
    password: str


class BudgetUpdate(BaseModel):
    window: Literal["hour", "day"]
    action_limit: int


class BudgetOut(BaseModel):
    user_id: int
    window: str
    action_limit: int
    action_used: int
    remaining: int
    window_started_at: str


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: Role
    is_agent: bool
    created_at: str
    # Set while the operator pause lever is engaged; None for an active account.
    paused_at: str | None = None


class UserPausedUpdate(BaseModel):
    paused: bool


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
    # This actor's durable action ceiling, or None when unbudgeted (unlimited) —
    # so an agent learns its bound by asking rather than by hitting a 429.
    budget: BudgetOut | None = None
    # Action kinds this actor must get operator approval for before acting, empty
    # when ungated (the default) — same ask-don't-guess principle as `budget`.
    approval_required: list[str] = []


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
def me(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Who am I? Returns the authenticated user's identity, role, and agent flag,
    plus the acting token's effective scopes — so an agent holding only a bearer
    token can discover who it is and what it may do without first provoking a 403.

    No DB read: current_actor has already resolved the full actor (and tokens.py
    stamped the token's scopes onto it), so this just reshapes what is in hand.
    `scopes` is null for non-token auth (a browser session or the trusted
    actor-header path), which is not scope-limited. The response_model trims the
    actor dict to the declared fields, so internal keys (_token_id, _token_scopes)
    and the password hash never leak.

    Declared before /{user_id} so "me" is matched here, not as a user id.

    `budget` is this actor's durable action ceiling, or null when unbudgeted, and
    `approval_required` lists the action kinds gated behind an operator decision
    — the same discover-your-limits-by-asking principle as `scopes`, two DB
    reads."""
    scopes = actor.get("_token_scopes")
    budget = budgets.observed(conn, actor["id"])
    return {
        **actor,
        "scopes": list(scopes) if scopes is not None else None,
        "budget": None if budget is None else budget.public(),
        "approval_required": approvals.gated_kinds(conn, actor["id"]),
    }


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


@router.put("/{user_id}/password", response_model=UserOut)
def reset_password(
    user_id: int,
    payload: UserPasswordReset,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Admin password reset: replace a user's password and revoke every session that
    account holds, atomically with its 'password_reset' audit event. This lever
    previously existed only in the browser, so the one privilege operation with no
    REST surface was also the one with no trail; both gaps close together.

    The command owns authorization (admin role, plus the admin scope for a bearer
    token) — this route deliberately resolves a plain actor and lets the command
    decide, so the boundary cannot drift from the browser's. The response is the
    updated user; the password is never echoed and never recorded."""
    try:
        return user_commands.reset_user_password(
            conn, actor=actor, target_user_id=user_id, password=payload.password
        )
    except user_commands.UserCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{user_id}/budget", response_model=BudgetOut | None)
def read_budget(
    user_id: int,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | None:
    """This user's durable action budget, or `null` when unbudgeted (the default,
    meaning unlimited). An admin may read anyone's; anyone may read their OWN, so
    an agent can discover its ceiling by asking instead of by being refused.

    The returned counters are rolled forward to now, so a window that has already
    elapsed reads as fresh rather than spent."""
    if actor["id"] != user_id:
        require_admin(actor)
    if users.get_user(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="no such user")
    budget = budgets.observed(conn, user_id)
    return None if budget is None else budget.public()


@router.put("/{user_id}/budget", response_model=BudgetOut)
def set_budget(
    user_id: int,
    payload: BudgetUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Set a durable ceiling on how many metered writes this user may make per
    fixed window, atomically with its `agent_budget_set` audit event.

    Metering is opt-in: a user with no budget is unlimited, so this is the switch
    that starts bounding an agent. Changing an existing ceiling preserves the
    current consumption and window — raising a limit releases the agent at once
    without gifting a fresh window. `action_limit: 0` is legitimate and freezes
    metered writes while leaving reads working."""
    if users.get_user(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="no such user")
    try:
        budget = budgets.set_budget(
            conn,
            actor_id=actor["id"],
            target_user_id=user_id,
            window=payload.window,
            action_limit=payload.action_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return budget.public()


@router.delete("/{user_id}/budget", status_code=204)
def clear_budget(
    user_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    """Remove a user's budget, returning them to unlimited, atomically with its
    `agent_budget_cleared` audit event. Idempotent: clearing an unbudgeted user
    is a 204 that records nothing."""
    if users.get_user(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="no such user")
    budgets.clear_budget(conn, actor_id=actor["id"], target_user_id=user_id)


@router.put("/{user_id}/paused", response_model=UserOut)
def update_paused(
    user_id: int,
    payload: UserPausedUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """The operator lever between watch and revoke: pause freezes the account at
    identity resolution without destroying anything; resume restores it exactly.
    Audited, idempotent, and refuses to pause the last active admin."""
    try:
        return agent_commands.set_user_paused(
            conn, actor=actor, target_user_id=user_id, paused=payload.paused
        )
    except agent_commands.AgentCommandError as exc:
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
        return agent_commands.offboard_user(conn, actor=actor, target_user_id=user_id)
    except agent_commands.AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
