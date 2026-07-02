"""Who is acting on this request.

Every write records WHO did it. `current_actor` resolves the acting user two
ways, in order of trust:

1. `Authorization: Bearer <token>` — a real credential. The token is hashed and
   looked up; a match AUTHENTICATES the caller as that user. This is the path
   agents and remote clients use, and the only one safe on an exposed network.

2. `X-Athena-Actor: <user_id>` — only CLAIMS an id, proves nothing. Accepted as
   a fallback solely when `config.TRUST_ACTOR_HEADER` is on (it defaults OFF), a
   mode meant for a trusted local/tailnet box. It also bootstraps the first
   token: on a fresh deploy you enable the header, mint a token through this
   path, then turn the header back off.

With the header trust off and no valid bearer token, the request is 401.

`optional_actor` is the same resolution without the 401: it returns the actor or
None. Endpoints that must stay reachable during first-run bootstrap (e.g.
creating the very first user, when nobody can possibly be authenticated yet)
depend on it and decide for themselves when absence is allowed.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Header, HTTPException, Request

from athena import config
from athena.core import rate_limits, tokens, users
from athena.core.deps import get_conn

ACTOR_HEADER = "X-Athena-Actor"
_BEARER_PREFIX = "bearer "


def current_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_athena_actor: str | None = Header(default=None, alias=ACTOR_HEADER),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Resolve the acting user from a bearer token or the actor-header fallback,
    or raise 401."""
    # 1. Bearer token — the authenticated path.
    if authorization is not None and authorization.lower().startswith(_BEARER_PREFIX):
        raw = authorization[len(_BEARER_PREFIX) :].strip()
        actor = tokens.resolve_token(conn, raw)
        if actor is None:
            raise HTTPException(status_code=401, detail="invalid or revoked token")
        _enforce_token_rate_limit(request, actor)
        return actor

    # 2. Actor header — local-trust fallback, only if explicitly enabled.
    if config.TRUST_ACTOR_HEADER and x_athena_actor is not None:
        try:
            actor_id = int(x_athena_actor)
        except ValueError:
            raise HTTPException(
                status_code=401, detail=f"{ACTOR_HEADER} must be an integer user id"
            )
        actor = users.get_user(conn, actor_id)
        if actor is None:
            raise HTTPException(status_code=401, detail="unknown actor")
        return actor

    raise HTTPException(status_code=401, detail="authentication required")


def optional_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_athena_actor: str | None = Header(default=None, alias=ACTOR_HEADER),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | None:
    """Resolve the acting user the same way as `current_actor`, but return None
    instead of raising when authentication is absent or invalid. The caller
    decides whether a missing actor is acceptable (used by the first-user
    bootstrap path)."""
    try:
        return current_actor(request, authorization, x_athena_actor, conn)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return None


def _enforce_token_rate_limit(request: Request, actor: dict) -> None:
    token_id = actor.get("_token_id")
    if token_id is None:
        return
    limiter: rate_limits.TokenRateLimiter | None = getattr(
        request.app.state, "token_rate_limiter", None
    )
    if limiter is None:
        return
    decision = limiter.check(int(token_id))
    if decision.allowed:
        return
    raise HTTPException(
        status_code=429,
        detail="token rate limit exceeded",
        headers={
            "Retry-After": str(decision.retry_after_seconds),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
        },
    )


def is_admin(actor: dict | None) -> bool:
    """Whether the actor may perform administration-only operations."""
    return actor is not None and actor.get("role") == users.ADMIN_ROLE


def can_write(actor: dict | None) -> bool:
    """Whether the actor may perform ordinary state-changing work."""
    return actor is not None and actor.get("role") != users.VIEWER_ROLE


def token_has_scope(actor: dict | None, scope: str) -> bool:
    """Whether this actor's bearer token allows a scope. Non-token auth is not
    token-scoped, so browser sessions and the trusted actor-header path pass."""
    if actor is None:
        return False
    scopes = actor.get("_token_scopes")
    if scopes is None:
        return True
    return tokens.ADMIN_SCOPE in scopes or scope in scopes


def require_token_scope(actor: dict, scope: str) -> dict:
    if not token_has_scope(actor, scope):
        raise HTTPException(status_code=403, detail=f"token scope required: {scope}")
    return actor


def require_admin(actor: dict) -> dict:
    if not is_admin(actor):
        raise HTTPException(status_code=403, detail="admin role required")
    return require_token_scope(actor, tokens.ADMIN_SCOPE)


def require_write_role(actor: dict) -> dict:
    if not can_write(actor):
        raise HTTPException(status_code=403, detail="viewer role is read-only")
    return actor


def admin_actor(actor: dict = Depends(current_actor)) -> dict:
    """FastAPI dependency for administration-only endpoints."""
    return require_admin(actor)


def write_actor(actor: dict = Depends(current_actor)) -> dict:
    """FastAPI dependency for authenticated non-viewer write endpoints."""
    return require_write_role(actor)


def issue_write_actor(actor: dict = Depends(current_actor)) -> dict:
    """Authenticated actor allowed to write Aegis issue/project/label state."""
    require_write_role(actor)
    return require_token_scope(actor, tokens.ISSUE_WRITE_SCOPE)


def docs_write_actor(actor: dict = Depends(current_actor)) -> dict:
    """Authenticated actor allowed to write Mentor docs state."""
    require_write_role(actor)
    return require_token_scope(actor, tokens.DOCS_WRITE_SCOPE)


def token_management_actor(actor: dict = Depends(current_actor)) -> dict:
    """Authenticated actor allowed to mint or revoke API tokens."""
    require_write_role(actor)
    return require_token_scope(actor, tokens.ADMIN_SCOPE)
