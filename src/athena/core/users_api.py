"""The users REST API.

Users are core (every module's rows point at a user), so this lives in core/,
not in a feature module. Same shape as aegis/api.py: Pydantic validates the body,
the data-access layer in core/users.py does the SQL.
"""

from __future__ import annotations

import secrets
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from athena.core.request_models import RequestBody
from athena import config
from athena.core import (
    agent_commands,
    approvals,
    budget_commands,
    budgets,
    user_commands,
    users,
)
from athena.core.ids import RowIdPath
from athena.core.packet_time import Timestamp
from athena.core.deps import get_conn
from athena.core.identity import (
    admin_actor,
    current_actor,
    bootstrap_optional_actor,
    require_admin,
)
from athena.mcp.config import (
    athena_mcp_command,
    PrintableProfile,
    claude_mcp_config,
)
from athena.core.refusals import Refusal

router = APIRouter(prefix="/users", tags=["core"])

# Adapter-owned translation of command-error kinds (the transport decides).
STATUS_BY_KIND: dict[str, int] = {
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "forbidden": 403,
    "unauthorized": 401,
}
CODE_BY_KIND: dict[str, str] = {
    "not_found": "not_found",
    "invalid": "validation_error",
    "conflict": "conflict",
    "forbidden": "forbidden",
    "unauthorized": "unauthenticated",
}

Role = Literal["admin", "member", "viewer"]
BOOTSTRAP_TOKEN_HEADER = "X-Athena-Bootstrap-Token"

_USER_COMMAND_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "invalid": 422,
}
_USER_COMMAND_CODE = {
    "unauthorized": "unauthenticated",
    "forbidden": "forbidden",
    "bad_request": "bad_request",
    "not_found": "not_found",
    "conflict": "conflict",
    "invalid": "validation_error",
}


def _user_command_http(exc: user_commands.UserCommandError) -> Refusal:
    """Translate the command's transport-neutral refusal into REST semantics."""
    return Refusal(
        status_code=_USER_COMMAND_STATUS[exc.kind],
        detail=exc.detail,
        code=_USER_COMMAND_CODE[exc.kind],
    )


def _bootstrap_token_matches(presented: str | None) -> bool:
    """Authenticate the one fresh-database administrator grant.

    The configured value is validated as bounded ASCII at process startup. Treat a
    malformed request header as an ordinary failed credential rather than letting
    ``compare_digest`` raise on non-ASCII input.
    """
    expected = config.BOOTSTRAP_TOKEN
    if not expected or presented is None:
        return False
    try:
        presented_bytes = presented.encode("ascii")
    except UnicodeEncodeError:
        return False
    if not 32 <= len(presented_bytes) <= 255:
        return False
    return secrets.compare_digest(expected.encode("ascii"), presented_bytes)


class UserCreate(RequestBody):
    email: str = Field(min_length=1, max_length=users.MAX_EMAIL_CHARS)
    name: str = Field(min_length=1, max_length=users.MAX_NAME_CHARS)
    # Optional: set it to enable browser login. Omit for agent/API-only users.
    password: str | None = None
    # Bootstrap ignores this and always creates the first user as admin.
    role: Role | None = None
    # Mark the account as an agent (display/audit distinction; grants nothing).
    is_agent: bool = False


class UserRoleUpdate(RequestBody):
    role: Role


class UserAgentUpdate(RequestBody):
    is_agent: bool


class UserPasswordReset(RequestBody):
    password: str


class BudgetUpdate(RequestBody):
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
    # Set while the removal tombstone holds; None for a present account. Only
    # ever non-None in responses that opted into include_removed.
    removed_at: str | None = None


class UserPausedUpdate(RequestBody):
    paused: bool


class AccountLeaseOut(BaseModel):
    """One active lease of the account an act stopped (C8). Only a lease on an
    issue the caller may see is listed."""

    issue_id: int
    key: str | None
    title: str
    generation: str
    expires_at: Timestamp
    # True when this act released it (``release_leases``), recorded as
    # ``lease_released``; False when the lease is still held.
    released: bool


#: The query option the four acts share. Never a default (ruling K1).
RELEASE_LEASES_QUERY = Query(
    False,
    description=(
        "Also release each of the account's active leases in this act, recorded "
        "per lease as lease_released. Without it nothing is released."
    ),
)


class UserPausedOut(UserOut):
    # The account's active leases when the pause or resume ran (C8).
    active_leases: list[AccountLeaseOut]


class TokenRevocationOut(BaseModel):
    user_id: int
    revoked_token_count: int
    active_leases: list[AccountLeaseOut]


class OffboardOut(BaseModel):
    user_id: int
    role: Role
    revoked_session_count: int
    revoked_token_count: int
    active_leases: list[AccountLeaseOut]


class RemoveOut(BaseModel):
    user_id: int
    removed_at: str
    revoked_session_count: int
    revoked_token_count: int
    active_leases: list[AccountLeaseOut]


class AgentOnboard(RequestBody):
    name: str
    # REQUIRED: an agent's first credential must say what it may do — the same
    # least-privilege rule as POST /tokens (no omitted-scopes fail-open).
    scopes: list[Literal["read", "issue:write", "docs:write", "admin"]]
    # Optional unique handle. Omitted → ``{slug}@agents.local``. Agents do not
    # sign in with a mailbox; this is only the users-table uniqueness key.
    email: str | None = None
    # Defaults to the agent's name.
    token_name: str | None = None
    # The ATHENA_MCP_PROFILE the printed mcp_config names. Configuration only:
    # it grants no scope and no role. Omitted → the seat profile the scopes
    # suggest (seat, or seat+knowledge with docs:write), as before.
    profile: PrintableProfile | None = None


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
    request: Request,
    bootstrap_token: str | None = Header(default=None, alias=BOOTSTRAP_TOKEN_HEADER),
    actor: dict | None = Depends(bootstrap_optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Bootstrap exception: there is no existing actor on a fresh database, so the
    # operator presents a separate one-time credential from process configuration.
    # Empty configuration disables the grant. The first user is always admin; after
    # bootstrap, only admins may add users or assign roles.
    if actor is None and request.headers.getlist("Idempotency-Key"):
        # The idempotency layer has no established principal to bind a bootstrap
        # receipt to. Reject the key for every unauthenticated user-create request
        # before reading bootstrap state, rather than silently ignoring its promise
        # or revealing whether the database is empty. Use the canonical anonymous
        # refusal so an invalid/revoked actor remains indistinguishable too.
        raise Refusal(
            status_code=401,
            detail="authentication required",
            code="unauthenticated",
        )
    existing = users.count_users(conn)
    is_bootstrap = existing == 0
    if is_bootstrap:
        raw_bootstrap_tokens = request.headers.getlist(BOOTSTRAP_TOKEN_HEADER)
        presented_bootstrap_token = (
            bootstrap_token
            if len(raw_bootstrap_tokens) == 1
            and raw_bootstrap_tokens[0] == bootstrap_token
            else None
        )
        if not _bootstrap_token_matches(presented_bootstrap_token):
            # Same refusal as every anonymous post-bootstrap attempt: do not reveal
            # whether the database is empty or a bootstrap credential is configured.
            # Duplicate or proxy-coalesced credential headers fail the same way.
            raise Refusal(
                status_code=401,
                detail="authentication required",
                code="unauthenticated",
            )
        if config.BOOTSTRAP_PASSWORD_REQUIRED and (
            not payload.password or not payload.password.strip()
        ):
            raise Refusal(
                status_code=422,
                detail="athena-serve bootstrap requires an administrator password",
                code="validation_error",
            )
    else:
        if actor is None:
            raise Refusal(
                status_code=401,
                detail="authentication required",
                code="unauthenticated",
            )
        require_admin(actor)
    # The command owns the insert AND the atomic 'created_user' audit event, so a new
    # actor entering the system is never a silent write. Bootstrap eligibility is
    # rechecked after BEGIN IMMEDIATE: two anonymous callers can both observe zero
    # here, but only one can still be the first user under the write lock.
    try:
        if is_bootstrap:
            return user_commands.bootstrap_user(
                conn,
                email=payload.email,
                name=payload.name,
                password=payload.password,
                is_agent=payload.is_agent,
            )
        assert actor is not None, "post-bootstrap create requires an actor"
        return user_commands.create_user(
            conn,
            actor_id=actor["id"],
            email=payload.email,
            name=payload.name,
            password=payload.password,
            role=payload.role or users.DEFAULT_ROLE,
            is_agent=payload.is_agent,
        )
    except user_commands.UserCommandError as exc:
        raise _user_command_http(exc) from exc
    except sqlite3.IntegrityError:
        # email collides with an existing user — reject at the boundary.
        raise Refusal(
            status_code=400, detail="email already in use", code="bad_request"
        )
    except ValueError as exc:
        raise Refusal(
            status_code=422, detail=str(exc), code="validation_error"
        ) from exc


@router.get("", response_model=list[UserOut])
def index(
    include_removed: bool = False,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Listing users is an authenticated operation — don't let an exposed instance
    # be enumerated anonymously. Removed users stay hidden unless the admin
    # explicitly asks for tombstones (?include_removed=true) — the only surface
    # that shows them, e.g. to find an id to /restore.
    return users.list_users(conn, include_removed=include_removed)


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

    `profile` names the tool set the printed config selects. It grants nothing:
    a profile is intersected with the token's scopes when athena-mcp starts, so
    a profile the scopes cannot fully use would start without some of its tools
    and say nothing. Only an explicitly requested profile is checked (by the
    onboarding command); such a request is refused before anything is created.
    Omitted, the widest seat profile the scopes suggest is printed, unchecked,
    as before.

    Declared before the /{user_id} routes so the literal path wins."""
    try:
        result = agent_commands.onboard_agent(
            conn,
            actor=actor,
            name=payload.name,
            email=payload.email,
            scopes=payload.scopes,
            token_name=payload.token_name,
            profile=payload.profile,
        )
    except agent_commands.AgentCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=str(exc),
            code=CODE_BY_KIND[exc.kind],
        ) from exc
    return {
        "user": result["user"],
        "token": result["token"],
        "mcp_config": claude_mcp_config(
            base_url=str(request.base_url).rstrip("/"),
            token=result["token"]["token"],
            profile=result["profile"],
            command=athena_mcp_command(),
        ),
    }


@router.get("/{user_id}", response_model=UserOut)
def show(
    user_id: RowIdPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    if actor["id"] != user_id:
        require_admin(actor)
    user = users.get_user(conn, user_id)
    if user is None:
        raise Refusal(status_code=404, detail="no such user", code="not_found")
    return user


@router.put("/{user_id}/role", response_model=UserOut)
def update_role(
    user_id: RowIdPath,
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
        raise _user_command_http(exc) from exc


@router.put("/{user_id}/agent", response_model=UserOut)
def update_agent(
    user_id: RowIdPath,
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
        raise _user_command_http(exc) from exc


@router.put("/{user_id}/password", response_model=UserOut)
def reset_password(
    user_id: RowIdPath,
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
        raise _user_command_http(exc) from exc


@router.get("/{user_id}/budget", response_model=BudgetOut | None)
def read_budget(
    user_id: RowIdPath,
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
        raise Refusal(status_code=404, detail="no such user", code="not_found")
    budget = budgets.observed(conn, user_id)
    return None if budget is None else budget.public()


@router.put("/{user_id}/budget", response_model=BudgetOut)
def set_budget(
    user_id: RowIdPath,
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
        raise Refusal(status_code=404, detail="no such user", code="not_found")
    try:
        budget = budget_commands.set_budget(
            conn,
            actor_id=actor["id"],
            target_user_id=user_id,
            window=payload.window,
            action_limit=payload.action_limit,
        )
    except budget_commands.BudgetCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=exc.detail,
            code=CODE_BY_KIND[exc.kind],
        ) from exc
    except ValueError as exc:
        raise Refusal(
            status_code=422, detail=str(exc), code="validation_error"
        ) from exc
    return budget.public()


@router.delete("/{user_id}/budget", status_code=204)
def clear_budget(
    user_id: RowIdPath,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    """Remove a user's budget, returning them to unlimited, atomically with its
    `agent_budget_cleared` audit event. Idempotent: clearing an unbudgeted user
    is a 204 that records nothing. No account clears its own budget: a 403."""
    if users.get_user(conn, user_id) is None:
        raise Refusal(status_code=404, detail="no such user", code="not_found")
    try:
        budget_commands.clear_budget(conn, actor_id=actor["id"], target_user_id=user_id)
    except budget_commands.BudgetCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=exc.detail,
            code=CODE_BY_KIND[exc.kind],
        ) from exc


@router.put("/{user_id}/paused", response_model=UserPausedOut)
def update_paused(
    user_id: RowIdPath,
    payload: UserPausedUpdate,
    release_leases: bool = RELEASE_LEASES_QUERY,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """The operator lever between watch and revoke: pause freezes the account at
    identity resolution without destroying anything; resume restores it exactly.
    Audited, idempotent, and refuses to pause the last active admin. Answers
    with the account's active leases; ``release_leases=true`` on a pause also
    releases them (422 on a resume or on your own account)."""
    try:
        return agent_commands.set_user_paused(
            conn,
            actor=actor,
            target_user_id=user_id,
            paused=payload.paused,
            release_leases=release_leases,
        )
    except agent_commands.AgentCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=str(exc),
            code=CODE_BY_KIND[exc.kind],
        ) from exc


@router.delete("/{user_id}/tokens", response_model=TokenRevocationOut)
def revoke_user_tokens(
    user_id: RowIdPath,
    release_leases: bool = RELEASE_LEASES_QUERY,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Admin kill switch: revoke EVERY live token a user holds. Unlike the
    self-service DELETE /tokens/{id} (owner-scoped), this reaches another user's
    credentials, so it is admin-only — the lever to lock out a compromised or
    runaway agent. Idempotent and audited. Answers with the account's active
    leases; ``release_leases=true`` also releases them."""
    try:
        return agent_commands.revoke_agent_tokens(
            conn, actor=actor, target_user_id=user_id, release_leases=release_leases
        )
    except agent_commands.AgentCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=str(exc),
            code=CODE_BY_KIND[exc.kind],
        ) from exc


@router.post("/{user_id}/offboard", response_model=OffboardOut)
def offboard(
    user_id: RowIdPath,
    release_leases: bool = RELEASE_LEASES_QUERY,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Admin one-click offboard: demote the user to viewer, revoke every session,
    and revoke every token — one audited action. Refuses to strip the last admin.
    Answers with the account's active leases; ``release_leases=true`` also
    releases them."""
    try:
        return agent_commands.offboard_user(
            conn, actor=actor, target_user_id=user_id, release_leases=release_leases
        )
    except agent_commands.AgentCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=str(exc),
            code=CODE_BY_KIND[exc.kind],
        ) from exc


@router.post("/{user_id}/remove", response_model=RemoveOut)
def remove(
    user_id: RowIdPath,
    release_leases: bool = RELEASE_LEASES_QUERY,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Admin removal: offboard AND stamp the tombstone in one audited action.
    The user vanishes from every list, picker, and email lookup; attributed
    history stays intact (nothing is deleted). Restore with /restore. Answers
    with the account's active leases; ``release_leases=true`` also releases
    them."""
    try:
        return agent_commands.remove_user(
            conn, actor=actor, target_user_id=user_id, release_leases=release_leases
        )
    except agent_commands.AgentCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=str(exc),
            code=CODE_BY_KIND[exc.kind],
        ) from exc


@router.post("/{user_id}/restore", response_model=UserOut)
def restore(
    user_id: RowIdPath,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Clear the removal tombstone: the account returns as an offboarded viewer
    with no credentials — every further step is its own audited action."""
    try:
        return agent_commands.restore_user(conn, actor=actor, target_user_id=user_id)
    except agent_commands.AgentCommandError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=str(exc),
            code=CODE_BY_KIND[exc.kind],
        ) from exc
