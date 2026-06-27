"""The activity-log REST API.

A read-only window onto the audit trail (core/activity.py). Like search, the feed
spans every issue and actor at once, so it is a privileged cross-cutting read:
authentication is the gate (the same bar as listing users or searching). Writes
are never made here — activity rows are recorded as a side effect of the actions
that cause them, at those endpoints.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

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


class RunOut(BaseModel):
    # A reconstructed run: one actor's uninterrupted stretch of work, with the events
    # (oldest-first) so a consumer can replay the sequence it represents.
    actor_id: int
    actor_name: str
    started_at: str
    ended_at: str
    first_id: int
    last_id: int
    event_count: int
    events: list[ActivityOut]


@router.get("", response_model=list[ActivityOut])
def feed(
    target_kind: str | None = Query(
        None, description="filter by kind; with target_id, one target's history"
    ),
    target_id: int | None = Query(None),
    actor_id: int | None = Query(None, description="filter to one actor's actions"),
    actor_type: Literal["agent", "human"] | None = Query(
        None, description="filter by actor type: agents only, or humans only"
    ),
    verb: str | None = Query(None, description="filter to one event type"),
    q: str | None = Query(
        None, description="search actor, verb, target, detail, or timestamp text"
    ),
    before_id: int | None = Query(
        None, description="paging cursor: only events older than this id"
    ),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # target_id is meaningless without target_kind (an id with no idea what kind of
    # thing it is), so reject that half-query. target_kind alone is fine — it's a
    # valid feed filter ("all issue events").
    if target_id is not None and target_kind is None:
        raise HTTPException(
            status_code=422,
            detail="target_id requires target_kind",
        )
    return activity.list_activity(
        conn,
        target_kind=target_kind,
        target_id=target_id,
        actor_id=actor_id,
        actor_is_agent=None if actor_type is None else actor_type == "agent",
        verb=verb,
        search=q,
        before_id=before_id,
        limit=limit,
    )


@router.get("/runs", response_model=list[RunOut])
def runs(
    actor_id: int = Query(..., description="reconstruct this actor's runs"),
    gap_seconds: int = Query(
        1800,
        ge=1,
        le=86400,
        description="max seconds between consecutive events within one run",
    ),
    limit: int = Query(
        200, ge=1, le=500, description="how many recent events to reconstruct from"
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # A reading lens over the trail: group one actor's recent events into work
    # sessions (runs). actor_id is required — a "runs" feed mixing actors would be
    # meaningless, since a run is by definition one actor's uninterrupted stretch.
    return activity.reconstruct_runs(
        conn, actor_id=actor_id, gap_seconds=gap_seconds, limit=limit
    )
