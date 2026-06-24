"""The activity-log REST API.

A read-only window onto the audit trail (core/activity.py). Like search, the feed
spans every issue and actor at once, so it is a privileged cross-cutting read:
authentication is the gate (the same bar as listing users or searching). Writes
are never made here — activity rows are recorded as a side effect of the actions
that cause them, at those endpoints.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from athena.core import activity
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/activity", tags=["core"])


class ActivityOut(BaseModel):
    id: int
    actor_id: int
    actor_name: str
    verb: str
    target_kind: str
    target_id: int
    detail: str
    created_at: str


@router.get("", response_model=list[ActivityOut])
def feed(
    target_kind: str | None = Query(
        None, description="with target_id, scope to one target's history"
    ),
    target_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Both filters together or neither: a kind without an id (or an id without a
    # kind) is an ambiguous half-query, so reject it rather than silently ignore.
    if (target_kind is None) != (target_id is None):
        raise HTTPException(
            status_code=422,
            detail="pass both target_kind and target_id, or neither",
        )
    return activity.list_activity(
        conn, target_kind=target_kind, target_id=target_id, limit=limit
    )
