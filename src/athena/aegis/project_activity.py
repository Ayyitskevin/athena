"""Recording project access-control events onto the activity trail.

The Aegis twin of mentor/space_activity.py, but for the access-control moments a
project gains once privacy is switched on: it was made private/public, and a member
was granted or revoked. One owner for "what counts as a project access event, and how
is it phrased", so the REST API and (later) the web forms record the SAME facts the
same way.

These helpers only ever *append* (through core.activity.record); the caller does the
write, then hands us the result so we record the fact. Events target the project
(target_kind="project"), which core.access.event_visibility_clause gates exactly like
a 'space' target — so a project-privacy event is visible to those who can see the
project and hidden from everyone else, never leaking that a private project exists.

Projects had no activity trail before this (creating one records nothing); these are
the first project-targeted events, added alongside the feature that needs them.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity


def record_project_created(
    conn: sqlite3.Connection, *, actor_id: int, project_id: int, name: str, key: str
) -> None:
    """A project (workspace container) was created — the trail's first record that
    it exists and who made it. Detail carries the name and issue-key prefix."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="created_project",
        target_kind="project",
        target_id=project_id,
        detail=f"{name} ({key})",
    )


def record_project_edited(
    conn: sqlite3.Connection, *, actor_id: int, project_id: int, changes: str
) -> None:
    """A project's name/key/description was edited. Detail summarizes what changed
    so a rename (ATH -> OPS) is legible in the feed."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="edited_project",
        target_kind="project",
        target_id=project_id,
        detail=changes,
    )


def record_project_deleted(
    conn: sqlite3.Connection, *, actor_id: int, project_id: int, name: str, key: str
) -> None:
    """A project was deleted. The event OUTLIVES its target (activity.target_id has
    no FK), so the detail preserves the name/key the vanished container had."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="deleted_project",
        target_kind="project",
        target_id=project_id,
        detail=f"{name} ({key})",
    )


def record_project_visibility_changed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    project_id: int,
    name: str,
    visibility: str,
) -> None:
    """A project was made private or public. The verb encodes the direction so the
    feed reads at a glance; the detail names the project. The caller only records this
    on a real transition (it skips a no-op set-to-same-value), so every row here is a
    genuine change."""
    verb = "project_made_private" if visibility == "private" else "project_made_public"
    activity.record(
        conn,
        actor_id=actor_id,
        verb=verb,
        target_kind="project",
        target_id=project_id,
        detail=name,
    )


def record_project_member_added(
    conn: sqlite3.Connection, *, actor_id: int, project_id: int, member_name: str
) -> None:
    """A user was granted access to a private project. The detail is the member's name
    so the feed reads "granted <name>"; recorded only on a real change (a re-add of an
    existing member is a no-op and records nothing)."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="project_member_added",
        target_kind="project",
        target_id=project_id,
        detail=member_name,
    )


def record_project_member_removed(
    conn: sqlite3.Connection, *, actor_id: int, project_id: int, member_name: str
) -> None:
    """A user's access to a private project was revoked. The detail is the member's
    name (preserved here even though the membership row is gone), recorded only when a
    row was actually removed."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="project_member_removed",
        target_kind="project",
        target_id=project_id,
        detail=member_name,
    )
