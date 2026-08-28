"""Compose notification preferences with Aegis-owned issue priority.

``core.notifications`` owns subscriptions, personal preferences, and inbox
rows. Aegis owns issue rows. This module is the legal seam between them: core
accepts a read resolver, and this upper layer supplies the Aegis data without
teaching core how the issue table is stored.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from athena.aegis import issues
from athena.core import notifications

_UNGATED = object()


def _resolve_target_priorities(
    conn: sqlite3.Connection, targets: set[tuple[str, int]]
) -> dict[tuple[str, int], str]:
    issue_ids = sorted(target_id for kind, target_id in targets if kind == "issue")
    resolved: dict[tuple[str, int], str] = {}
    for offset in range(0, len(issue_ids), 500):
        chunk = set(issue_ids[offset : offset + 500])
        resolved.update(
            {
                ("issue", issue_id): priority
                for issue_id, priority in issues.priorities_for_ids(conn, chunk).items()
            }
        )
    return resolved


def list_priority_notifications(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    unread_only: bool = False,
    min_priority: str | None = None,
    include_muted: bool = False,
    digest: bool = False,
    limit: int = 50,
    actor: dict | None | object = _UNGATED,
) -> dict:
    """The actor's priority inbox with Aegis issue priority composed in."""
    kwargs: dict[str, Any] = {}
    if actor is not _UNGATED:
        kwargs["actor"] = actor
    return notifications.list_priority_notifications(
        conn,
        user_id,
        unread_only=unread_only,
        min_priority=min_priority,
        include_muted=include_muted,
        digest=digest,
        limit=limit,
        resolve_priorities=_resolve_target_priorities,
        **kwargs,
    )


def priority_summary(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    unread_only: bool = False,
    actor: dict | None | object = _UNGATED,
) -> dict:
    """Priority counts from the same composed projection as the detailed read."""
    kwargs: dict[str, Any] = {}
    if actor is not _UNGATED:
        kwargs["actor"] = actor
    return notifications.priority_summary(
        conn,
        user_id,
        unread_only=unread_only,
        resolve_priorities=_resolve_target_priorities,
        **kwargs,
    )
