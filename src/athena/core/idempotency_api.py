"""The read of an idempotency key's state, for the credential that used it (A11).

A keyed write that times out leaves its caller holding a key and no answer.
Retrying finds out, but a retry can itself be refused: 409
``idempotency_in_progress`` while the first request still runs, 409
``idempotency_indeterminate`` when its outcome is unknown. Until now nothing
read the state of a key, so the caller could only retry and interpret the
refusal. This read says what a retry would meet and changes nothing.

Keys are stored under the credential that sent them, so the read needs no
authorization beyond authentication: a caller can only ever name its own.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, Path
from pydantic import BaseModel, Field

from athena.core import idempotency
from athena.core.deps import get_conn
from athena.core.identity import current_actor
from athena.core.refusals import Refusal

router = APIRouter(prefix="/idempotency", tags=["core"])

_BEARER = "bearer "

#: What an identical retry would meet in each state: the words an agent acts on.
RETRY = {
    "unused": "executes: no request under this key is on record, or its receipt expired",
    "in_progress": (
        "refused 409 idempotency_in_progress: the first request is still "
        "running; read this again after its Retry-After"
    ),
    "completed": "replays the stored answer; nothing runs again",
    "indeterminate": (
        "refused 409 idempotency_indeterminate: the outcome is unknown; read "
        "the target named by method and path, then use a new key if the write "
        "is still needed"
    ),
    "authorization_changed": (
        "refused 409 idempotency_authorization_changed: what you may do "
        "changed since the key was used; read the target, then use a new key"
    ),
}


class IdempotencyKeyOut(BaseModel):
    key: str
    state: Literal[
        "unused", "in_progress", "completed", "indeterminate", "authorization_changed"
    ]
    method: str | None = Field(description="the keyed request's method, if on record")
    path: str | None = Field(description="the keyed request's path, if on record")
    status_code: int | None = Field(
        description="the stored answer's status, when state is completed"
    )
    retry: str = Field(
        description=(
            "what a retry of the same request under this key would meet; a "
            "different request under it is refused idempotency_mismatch"
        )
    )


@router.get("/{key:path}", response_model=IdempotencyKeyOut)
def read_key(
    key: str = Path(description="the Idempotency-Key you sent"),
    authorization: str | None = Header(default=None),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """What a retry under this key would meet, for a key this credential used.
    Reads only: it claims nothing, replays nothing and expires nothing."""
    if not idempotency.is_valid_key(key):
        raise Refusal(
            status_code=400,
            detail="Idempotency-Key must be 1-255 visible ASCII characters",
            code="idempotency_key_invalid",
        )
    if authorization is not None and authorization.lower().startswith(_BEARER):
        identity = idempotency.token_identity(authorization[len(_BEARER) :].strip())
    else:
        identity = idempotency.actor_identity(int(actor["id"]))
    state = idempotency.key_state(conn, key=key, identity=identity)
    return {"key": key, **state, "retry": RETRY[state["state"]]}
