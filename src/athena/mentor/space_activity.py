"""Recording space lifecycle events onto the activity trail.

The container twin of mentor/page_activity.py: one owner for "what counts as a
space event, and how is it phrased", so the two surfaces that create or remove a
space — the REST API (mentor/api.py) and the web forms (web/mentor.py) — record
the SAME facts the same way.

These helpers only ever *append* (through core.activity.record); the caller does
the write, then hands us the result so we record the fact. Events target the space
(target_kind="space"), so the global feed links back to it and the space's own
detail page can show its history. A space_deleted row outlives the space it names —
the trail is append-only and holds no FK to the (now gone) space, so the name is
preserved in the detail.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity


def record_space_created(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    name: str,
    commit: bool = True,
) -> None:
    """A space was created — the first audit fact in its history. ``commit=False``
    composes this inside the audited create command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="space_created",
        target_kind="space",
        target_id=space_id,
        detail=name,
        commit=commit,
    )


def record_space_edited(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    before: dict,
    after: dict,
    commit: bool = True,
) -> None:
    """A space's key, name, or description changed. No-op if none of the three
    actually moved — the web edit form always resubmits all fields, so an unchanged
    save is not a lifecycle moment (mirrors the page no-op rule). The detail names
    the space (its current name) so the feed reads cleanly. ``commit=False`` composes
    this inside the audited edit command's transaction."""
    if (
        before["key"] == after["key"]
        and before["name"] == after["name"]
        and before["description"] == after["description"]
    ):
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="space_edited",
        target_kind="space",
        target_id=after["id"],
        detail=after["name"],
        commit=commit,
    )


def record_space_deleted(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    name: str,
    commit: bool = True,
) -> None:
    """A space was removed — who took the container down, with its name preserved in
    the detail since the space row itself is gone. ``commit=False`` composes this
    inside the audited delete command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="space_deleted",
        target_kind="space",
        target_id=space_id,
        detail=name,
        commit=commit,
    )


def record_space_visibility_changed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    name: str,
    visibility: str,
    commit: bool = True,
) -> None:
    """A space was made private or public — the Mentor twin of the project event. The
    verb encodes the direction, the detail names the space. Recorded only on a real
    transition (the caller skips a no-op set-to-same-value). ``commit=False`` composes
    this inside the audited visibility command's transaction."""
    verb = "space_made_private" if visibility == "private" else "space_made_public"
    activity.record(
        conn,
        actor_id=actor_id,
        verb=verb,
        target_kind="space",
        target_id=space_id,
        detail=name,
        commit=commit,
    )


def record_space_member_added(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    member_name: str,
    commit: bool = True,
) -> None:
    """A user was granted access to a private space. The detail is the member's name;
    recorded only on a real change (re-adding an existing member records nothing).
    ``commit=False`` composes this inside the audited space-access command's
    transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="space_member_added",
        target_kind="space",
        target_id=space_id,
        detail=member_name,
        commit=commit,
    )


def record_space_member_removed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    member_name: str,
    commit: bool = True,
) -> None:
    """A user's access to a private space was revoked. The detail is the member's name,
    preserved here even though the membership row is gone; recorded only when a row was
    actually removed. ``commit=False`` composes this inside the audited space-access
    command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="space_member_removed",
        target_kind="space",
        target_id=space_id,
        detail=member_name,
        commit=commit,
    )
