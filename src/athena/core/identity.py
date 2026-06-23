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

from fastapi import Depends, Header, HTTPException

from athena import config
from athena.core import tokens, users
from athena.core.deps import get_conn

ACTOR_HEADER = "X-Athena-Actor"
_BEARER_PREFIX = "bearer "


def current_actor(
    authorization: str | None = Header(default=None),
    x_athena_actor: str | None = Header(default=None, alias=ACTOR_HEADER),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Resolve the acting user from a bearer token or the actor-header fallback,
    or raise 401."""
    # 1. Bearer token — the authenticated path.
    if authorization is not None and authorization.lower().startswith(_BEARER_PREFIX):
        raw = authorization[len(_BEARER_PREFIX):].strip()
        actor = tokens.resolve_token(conn, raw)
        if actor is None:
            raise HTTPException(status_code=401, detail="invalid or revoked token")
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
    authorization: str | None = Header(default=None),
    x_athena_actor: str | None = Header(default=None, alias=ACTOR_HEADER),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | None:
    """Resolve the acting user the same way as `current_actor`, but return None
    instead of raising when authentication is absent or invalid. The caller
    decides whether a missing actor is acceptable (used by the first-user
    bootstrap path)."""
    try:
        return current_actor(authorization, x_athena_actor, conn)
    except HTTPException:
        return None
