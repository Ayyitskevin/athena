"""The notifications + watches REST API — a user's personal inbox and subscriptions.

These are PERSONAL actions (your inbox, your watches), so they require only an
authenticated actor — not a write role. A read-only viewer may still watch issues
and read their inbox; they just can't change shared state elsewhere. Every read and
write is scoped to the acting user, so no one can see or touch another's inbox.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from athena.core import notifications
from athena.core.ids import RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import current_actor, personal_write_actor

router = APIRouter(tags=["core"])


class NotificationOut(BaseModel):
    id: int
    event_id: int
    read_at: str | None = None
    created_at: str
    actor_id: int
    actor_name: str
    verb: str
    target_kind: str
    target_id: int
    detail: str
    event_at: str


class UnreadCount(BaseModel):
    count: int


class WatchCreate(BaseModel):
    target_kind: str
    target_id: int


@router.get("/notifications", response_model=list[NotificationOut])
def inbox(
    unread: bool = Query(False, description="only unread notifications"),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    return notifications.list_notifications(
        conn, actor["id"], unread_only=unread, limit=limit, actor=actor
    )


@router.get("/notifications/unread_count", response_model=UnreadCount)
def unread_count(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    return {"count": notifications.unread_count(conn, actor["id"], actor=actor)}


@router.post("/notifications/{notification_id}/read", status_code=204)
def mark_read(
    notification_id: RowIdPath,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    # 404 if it isn't an unread notification belonging to this actor — a user can
    # only touch their own inbox.
    if not notifications.mark_read(conn, actor["id"], notification_id, actor=actor):
        raise HTTPException(status_code=404, detail="no such unread notification")


@router.post("/notifications/read_all", response_model=UnreadCount)
def mark_all_read(
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Returns how many were just cleared.
    return {"count": notifications.mark_all_read(conn, actor["id"], actor=actor)}


def _validate_kind(kind: str) -> str:
    if kind not in notifications.WATCHABLE_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"target_kind must be one of: {', '.join(notifications.WATCHABLE_KINDS)}",
        )
    return kind


@router.post("/watches", status_code=204)
def add_watch(
    payload: WatchCreate,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    notifications.watch(
        conn, actor["id"], _validate_kind(payload.target_kind), payload.target_id
    )


@router.delete("/watches/{target_kind}/{target_id}", status_code=204)
def remove_watch(
    target_kind: str,
    target_id: RowIdPath,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    if not notifications.unwatch(
        conn, actor["id"], _validate_kind(target_kind), target_id
    ):
        raise HTTPException(status_code=404, detail="not watching that target")


# --- priority / mute / digest projections -----------------------------------


class PriorityNotificationOut(BaseModel):
    id: int
    event_id: int
    read_at: str | None = None
    created_at: str
    actor_id: int
    actor_name: str
    verb: str
    target_kind: str
    target_id: int
    detail: str
    event_at: str
    priority: str
    muted: bool
    digest_bucket: str | None = None
    source: dict


class PriorityInboxOut(BaseModel):
    observed_at: str
    items: list[PriorityNotificationOut]


class PrioritySummaryOut(BaseModel):
    observed_at: str
    by_priority: dict[str, dict[str, int]]


class WatchPreferenceOut(BaseModel):
    user_id: int
    target_kind: str
    target_id: int
    priority: str | None = None
    mute_until: str | None = None
    digest_window_minutes: int | None = None
    created_at: str
    updated_at: str


class WatchPreferenceUpdate(BaseModel):
    priority: str | None = None
    mute_until: str | None = None
    digest_window_minutes: int | None = None


@router.get("/notifications/priority", response_model=PriorityInboxOut)
def priority_inbox(
    unread: bool = Query(False, description="only unread notifications"),
    min_priority: str | None = Query(None, description="filter to this priority or higher"),
    include_muted: bool = Query(False, description="include notifications muted by preference"),
    digest: bool = Query(False, description="include digest bucket per item"),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Inbox annotated with resolved priority, mute state, and optional digest
    buckets. This is a read-only projection over the existing notifications and
    watch_preferences tables; no new event authority is introduced."""
    if min_priority is not None and min_priority not in notifications.PRIORITY_ORDER:
        raise HTTPException(
            status_code=422,
            detail=f"min_priority must be one of: {', '.join(notifications.PRIORITY_ORDER)}",
        )
    return notifications.list_priority_notifications(
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
    """Count notifications per resolved priority. Muted items are counted
    separately so a cockpit can show '3 urgent, 1 muted'."""
    return notifications.priority_summary(
        conn, actor["id"], unread_only=unread, actor=actor
    )


@router.get("/watches/{target_kind}/{target_id}/preference",
            response_model=WatchPreferenceOut)
def get_watch_preference(
    target_kind: str,
    target_id: RowIdPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Read your notification preferences for a watched target."""
    pref = notifications.get_preference(
        conn, actor["id"], _validate_kind(target_kind), target_id
    )
    if pref is None:
        raise HTTPException(status_code=404, detail="no preference for this watch")
    return pref


@router.put("/watches/{target_kind}/{target_id}/preference",
            response_model=WatchPreferenceOut)
def update_watch_preference(
    target_kind: str,
    target_id: RowIdPath,
    payload: WatchPreferenceUpdate,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Set or update your notification preferences for a watched target.

    The preferences layer is a personal projection override: it does not create
    or delete watches, and it never writes to the activity log. A missing watch
    is fine — the preference applies if and when you watch the target.
    """
    try:
        return notifications.set_preference(
            conn,
            actor["id"],
            _validate_kind(target_kind),
            target_id,
            priority=payload.priority,
            mute_until=payload.mute_until,
            digest_window_minutes=payload.digest_window_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/watches/{target_kind}/{target_id}/preference", status_code=204)
def remove_watch_preference(
    target_kind: str,
    target_id: RowIdPath,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    """Remove your notification preferences for a target, reverting to the
    default issue-priority fallback."""
    notifications.delete_preference(
        conn, actor["id"], _validate_kind(target_kind), target_id
    )
