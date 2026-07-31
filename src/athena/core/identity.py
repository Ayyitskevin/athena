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
None. Endpoints that must stay reachable during first-run bootstrap depend on it
and decide for themselves when absence is allowed. Creating the first user has a
separate one-time bootstrap credential; absence of an Athena actor alone never
grants administrator authority.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Header, HTTPException, Request

from athena import config
from athena.core import rate_limits, security_events, tokens, users
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
            # Bound the flood BEFORE recording: without this, invalid-bearer
            # hammering was both unthrottled and (now) trail-writing.
            enforce_anon_rate_limit(request)
            revoked = tokens.get_revoked_token(conn, raw)
            if revoked is not None:
                # A revoked credential being PRESENTED is the signal the kill
                # switch exists for — attribute it to the token's owner.
                security_events.record_failure(
                    conn,
                    actor_id=revoked["user_id"],
                    verb=security_events.VERB_REVOKED_TOKEN_USED,
                    target_kind="token",
                    target_id=revoked["id"],
                    detail="revoked bearer token presented",
                )
            raise HTTPException(status_code=401, detail="invalid or revoked token")
        _enforce_token_rate_limit(request, actor)
        # Bearer requests are bounded by the per-token limiter above, so the
        # paused-refusal recording is already flood-bounded here.
        _refuse_paused(request, conn, actor, bounded=True)
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
        # The header path carries no token, so nothing bounds it — gate the
        # paused-refusal recording behind the anon limiter (non-raising) so a
        # paused claimed id can't flood the trail. The 403 is unchanged either way.
        _refuse_paused(request, conn, actor, bounded=False)
        return actor

    raise HTTPException(status_code=401, detail="authentication required")


def _refuse_paused(
    request: Request, conn: sqlite3.Connection, actor: dict, *, bounded: bool
) -> None:
    """The pause lever, enforced at identity resolution: a paused account is
    refused EVERY authenticated action — reads, writes, heartbeats — until an
    admin resumes it. 403 (not 401): the credential is valid, the account is
    deliberately frozen, and retrying with the same token is pointless. Each
    refused attempt lands on the trail — a paused agent that KEEPS trying is
    exactly what the operator paused it to find out — but only while the caller
    is under a rate limit (``bounded`` on the bearer path via the per-token
    limiter, or the anon limiter on the unbounded header path), so the recording
    can never flood the trail. The 403 is raised regardless of that gate."""
    if not actor.get("paused_at"):
        return
    if bounded or _anon_rate_allows(request):
        security_events.record_failure(
            conn,
            actor_id=actor["id"],
            verb=security_events.VERB_PAUSED_REFUSED,
            target_kind="user",
            target_id=actor["id"],
        )
    raise HTTPException(status_code=403, detail="account is paused")


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
        # No valid credential: this is an anonymous request. Throttle it by client IP so
        # a credential-free caller can't hammer these public reads — the per-token limiter
        # only guards authenticated paths, leaving anonymous reads otherwise unbounded.
        # An INVALID BEARER was already charged inside current_actor (it bounds the
        # revoked-token recording there), so charging again here would count one
        # request twice; only charge the truly credential-less case.
        bearer_present = authorization is not None and authorization.lower().startswith(
            _BEARER_PREFIX
        )
        if not bearer_present:
            enforce_anon_rate_limit(request)
        return None


def _enforce_token_rate_limit(request: Request, actor: dict) -> None:
    token_id = actor.get("_token_id")
    if token_id is None:
        return
    # Idempotency authenticates and charges a keyed request before it can inspect
    # or replay a receipt. A cache miss still reaches this dependency, so recognize
    # that exact token's pre-charge rather than counting one HTTP request twice.
    if getattr(request.state, "_athena_idempotency_rate_limited_token_id", None) == int(
        token_id
    ):
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


def _client_ip(request: Request) -> str:
    """The direct peer's IP — the anon-limit key. Deliberately NOT X-Forwarded-For:
    a directly-exposed box (Athena's common shape) must not let a caller spoof the key
    and evade the limit. Behind a trusted proxy every anonymous request shares the
    proxy's IP; account for that at the proxy or leave the limit off (see config)."""
    client = request.client
    return client.host if client is not None else "unknown"


def _anon_rate_allows(request: Request) -> bool:
    """Non-raising anon-budget check for gating a best-effort trail write on an
    otherwise-unbounded path: consumes one unit and reports whether the caller is
    under the anonymous limit. Never changes the HTTP response — only whether a
    failure event is recorded. No limiter configured means no bound to apply."""
    limiter: rate_limits.FixedWindowRateLimiter | None = getattr(
        request.app.state, "anon_rate_limiter", None
    )
    if limiter is None:
        return True
    return limiter.check(_client_ip(request)).allowed


def enforce_anon_rate_limit(request: Request) -> None:
    """Charge the anonymous per-IP limiter, raising 429 when the caller is over
    budget. No-op when no limiter is configured (the limit defaults off).

    Used for genuinely anonymous traffic: credential-free reads (optional_actor),
    invalid-bearer floods (current_actor), and the unauthenticated forge inbound
    route — the one path with no actor resolution at all, which must charge this
    limiter itself before doing any work."""
    limiter: rate_limits.FixedWindowRateLimiter | None = getattr(
        request.app.state, "anon_rate_limiter", None
    )
    if limiter is None:
        return
    decision = limiter.check(_client_ip(request))
    if decision.allowed:
        return
    raise HTTPException(
        status_code=429,
        detail="anonymous rate limit exceeded",
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


class ScopeDenied(HTTPException):
    """A 403 that also names WHO hit WHICH scope boundary, so the app-level
    handler (main.py) can put the probe on the activity trail while returning
    exactly the same response a plain HTTPException produced. An agent that
    keeps walking into its scope wall is a story the operator should see."""

    def __init__(
        self,
        actor: dict | None,
        scope: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=403,
            detail=f"token scope required: {scope}",
            headers=headers,
        )
        self.actor_id = actor.get("id") if actor else None
        self.scope = scope


def require_token_scope(actor: dict, scope: str) -> dict:
    if not token_has_scope(actor, scope):
        raise ScopeDenied(actor, scope)
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


def personal_write_actor(actor: dict = Depends(current_actor)) -> dict:
    """A write to the actor's OWN personal state — saved filters, watches, inbox.
    Unlike a module write it does NOT require the write ROLE (a viewer may manage their
    own queries/subscriptions, which touch only their own rows), but it MUST refuse a
    READ-ONLY bearer token: a token minted with only the 'read' scope must never mutate
    anything (the agent-safety read-vs-write token boundary). Non-token auth (browser
    session / trusted actor header) is not token-scoped and passes."""
    scopes = actor.get("_token_scopes")
    if scopes is not None and not set(scopes) & {
        tokens.ISSUE_WRITE_SCOPE,
        tokens.DOCS_WRITE_SCOPE,
        tokens.ADMIN_SCOPE,
    }:
        raise ScopeDenied(actor, "a write scope")
    return actor


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
