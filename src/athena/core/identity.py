"""Who is acting on this request.

Every write needs to record WHO did it. `current_actor` reads the
`X-Athena-Actor: <user_id>` header, confirms it names a real user, and hands
the handler that user. Endpoints depend on it instead of trusting a caller to
put their own id in the request body.

It IDENTIFIES the caller; it does NOT authenticate them — anyone who can reach
the API could send any id. That is acceptable only on a trusted local/tailnet
deployment. Real token auth must land here before Athena is network-exposed.
"""
from __future__ import annotations

import sqlite3

from fastapi import Depends, Header, HTTPException

from athena.core import users
from athena.core.deps import get_conn

ACTOR_HEADER = "X-Athena-Actor"


def current_actor(
    x_athena_actor: str | None = Header(default=None, alias=ACTOR_HEADER),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Resolve the acting user from the actor header, or raise 401."""
    if x_athena_actor is None:
        raise HTTPException(status_code=401, detail=f"{ACTOR_HEADER} header required")
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
