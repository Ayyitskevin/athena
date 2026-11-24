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

from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import BaseModel, Field

from athena.core import idempotency, idempotency_commands, list_cursor
from athena.core.deps import get_conn
from athena.core.identity import current_actor, require_admin
from athena.core.ids import RowIdPath
from athena.core.packet_time import Timestamp
from athena.core.refusals import Refusal
from athena.core.request_models import RequestBody

router = APIRouter(prefix="/idempotency", tags=["core"])
# The operator's side (H14). Its own prefix: every path under /idempotency is
# somebody's key.
admin_router = APIRouter(prefix="/idempotency-keys", tags=["core"])

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
    "applied": (
        "refused 409 idempotency_applied: an operator confirmed the request "
        "was applied; do not send it again under any key"
    ),
}


class IdempotencyKeyOut(BaseModel):
    key: str
    state: Literal[
        "unused",
        "in_progress",
        "completed",
        "indeterminate",
        "authorization_changed",
        "applied",
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


class KeySenderOut(BaseModel):
    user_id: int
    user_name: str
    token_id: int | None = Field(description="null for a browser or header request")
    token_name: str | None


class IndeterminateKeyOut(BaseModel):
    id: int = Field(description="what resolve takes, with the key")
    key: str
    method: str
    path: str
    issue_id: int | None = Field(description="the issue the request named, if one")
    reason: str = Field(
        description=(
            "owner_lost: the process running it no longer exists; lease_expired: "
            "its claim ran out unfinished; otherwise why it could not be recorded"
        )
    )
    claimed_at: Timestamp
    since: Timestamp = Field(description="when its outcome became unknown")
    sent_by: KeySenderOut | None = Field(
        description="null when the token or the account no longer exists"
    )


class IndeterminateKeysOut(BaseModel):
    """One page, paged as every new list is (D2): while `has_more` is true,
    send `next_cursor` back as `cursor`."""

    items: list[IndeterminateKeyOut]
    # Opaque: pass it back unchanged. Null on the last page.
    next_cursor: str | None
    has_more: bool


class ResolveKey(RequestBody):
    key: str = Field(description="the key's text, as the list shows it")
    outcome: Literal["applied", "not_applied"] = Field(
        description=(
            "what reading the request's target showed: applied (the key then "
            "refuses with idempotency_applied) or not_applied (the key is "
            "forgotten and the same request under it runs)"
        )
    )


class ResolvedKeyOut(BaseModel):
    id: int
    key: str
    method: str
    path: str
    outcome: Literal["applied", "not_applied"]
    event_id: int = Field(description="the trail event that records the decision")


_CURSOR = "indeterminate-keys"


@admin_router.get("/indeterminate", response_model=IndeterminateKeysOut)
def list_indeterminate_keys(
    limit: int = Query(
        idempotency.INDETERMINATE_DEFAULT_LIMIT,
        ge=1,
        le=idempotency.INDETERMINATE_MAX_LIMIT,
    ),
    cursor: str | None = Query(
        None,
        max_length=list_cursor.MAX_CURSOR_CHARS,
        description="the previous page's next_cursor, unchanged; omit for the first page",
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """The idempotency keys whose outcome is unknown, oldest first, each with
    the request it belongs to and who sent it. At most `limit` a call; while
    `has_more` is true, send `next_cursor` back as `cursor`. Admin only."""
    require_admin(actor)
    after_id = 0
    if cursor is not None:
        try:
            (after_id,) = list_cursor.decode(cursor, _CURSOR, (int,))
        except list_cursor.CursorError as exc:
            raise Refusal(
                status_code=422, detail=str(exc), code="validation_error"
            ) from exc
    rows = idempotency.list_indeterminate(conn, limit=limit + 1, after_id=after_id)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": rows,
        "next_cursor": (
            list_cursor.encode(_CURSOR, [rows[-1]["id"]]) if has_more else None
        ),
        "has_more": has_more,
    }


@admin_router.post("/{key_id}/resolve", response_model=ResolvedKeyOut)
def resolve_key(
    key_id: RowIdPath,
    body: ResolveKey,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Settle one key whose outcome is unknown, after reading the target its
    method and path name. Recorded on the trail. Admin only."""
    require_admin(actor)
    try:
        return idempotency_commands.resolve(
            conn, actor=actor, row_id=key_id, key=body.key, outcome=body.outcome
        )
    except idempotency_commands.KeyNotIndeterminate:
        raise Refusal(
            status_code=404,
            detail="no idempotency key with that id and text has an unknown outcome",
            code="not_found",
        )
