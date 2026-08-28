"""REST projection for notification priority composed with Aegis issues."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from athena.aegis import notification_priority
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(tags=["aegis"])

Priority = Literal["low", "normal", "medium", "high", "urgent"]
DeliveryState = Literal["immediate", "digest", "muted"]
PrioritySource = Literal[
    "preference", "target", "default", "invalid_preference", "invalid_target"
]


class PriorityNotificationSourceOut(BaseModel):
    target_kind: Literal["issue", "page", "space"]
    target_id: int
    issue_priority: str | None = None
    preference_set: bool
    preference_target_kind: Literal["issue", "page", "space"] | None = None
    preference_target_id: int | None = None
    preference_valid: bool
    priority_source: PrioritySource


class PriorityNotificationOut(BaseModel):
    id: int
    event_id: int
    read_at: str | None = None
    created_at: str
    actor_id: int
    actor_name: str
    verb: str
    target_kind: Literal["issue", "page", "space"]
    target_id: int
    detail: str
    event_at: str
    priority: Priority
    muted: bool
    delivery_state: DeliveryState
    digest_bucket: str | None = None
    source: PriorityNotificationSourceOut


class PriorityInboxOut(BaseModel):
    observed_at: str
    items: list[PriorityNotificationOut]


class PrioritySummaryOut(BaseModel):
    observed_at: str
    by_priority: dict[Priority, dict[str, int]]


@router.get("/notifications/priority", response_model=PriorityInboxOut)
def priority_inbox(
    unread: bool = Query(False, description="only unread notifications"),
    min_priority: Priority | None = Query(
        None, description="filter to this priority or higher"
    ),
    include_muted: bool = Query(
        False, description="include notifications muted by preference"
    ),
    digest: bool = Query(False, description="include digest bucket per item"),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Read the actor's inbox with priority, mute, and digest annotations."""
    return notification_priority.list_priority_notifications(
        conn,
        actor["id"],
        unread_only=unread,
        min_priority=min_priority,
        include_muted=include_muted,
        digest=digest,
        limit=limit,
        actor=actor,
    )


@router.get("/notifications/priority/summary", response_model=PrioritySummaryOut)
def priority_summary(
    unread: bool = Query(False, description="only unread notifications"),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Count visible notifications per resolved priority and mute state."""
    return notification_priority.priority_summary(
        conn, actor["id"], unread_only=unread, actor=actor
    )
