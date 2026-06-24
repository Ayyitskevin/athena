"""Recording issue lifecycle events onto the activity trail.

One owner for the "what counts as an event, and how is it phrased" rules, so the
two endpoint surfaces that can change an issue — the REST API (aegis/api.py) and
the web forms (web/router.py) — record the SAME facts the same way. Without this
the browser path and the API path would each grow their own copy of the
record-only-if-it-changed logic and inevitably drift.

These helpers only ever *append* (through core.activity.record); they never
change the issue itself. The caller does the write, then hands us the before/after
so we record the diff. "Record only on real change" lives here: a no-op edit (same
status, same assignee) writes nothing, on either surface.
"""
from __future__ import annotations

import sqlite3

from athena.core import activity, users


def record_created(conn: sqlite3.Connection, *, actor_id: int, issue_id: int) -> None:
    """An issue was created. The first audit fact in its history."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="created",
        target_kind="issue",
        target_id=issue_id,
    )


def record_status_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: str,
    after: str,
) -> None:
    """Record a status transition as "before → after". No-op if unchanged — the
    lifecycle moment only matters when the status actually moved."""
    if before == after:
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="changed_status",
        target_kind="issue",
        target_id=issue_id,
        detail=f"{before} → {after}",
    )


def record_assignee_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: int | None,
    after: int | None,
) -> None:
    """Record an assignment change. No-op if the assignee didn't change. Clearing
    records "unassigned" (no detail); setting records "assigned" with the new
    assignee's display name as the human specifics."""
    if before == after:
        return
    if after is None:
        activity.record(
            conn,
            actor_id=actor_id,
            verb="unassigned",
            target_kind="issue",
            target_id=issue_id,
        )
        return
    assignee = users.get_user(conn, after)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="assigned",
        target_kind="issue",
        target_id=issue_id,
        detail=assignee["name"] if assignee else "",
    )
