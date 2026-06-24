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
    conn: sqlite3.Connection, *, actor_id: int, space_id: int, name: str
) -> None:
    """A space was created — the first audit fact in its history."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="space_created",
        target_kind="space",
        target_id=space_id,
        detail=name,
    )


def record_space_deleted(
    conn: sqlite3.Connection, *, actor_id: int, space_id: int, name: str
) -> None:
    """A space was removed — who took the container down, with its name preserved in
    the detail since the space row itself is gone."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="space_deleted",
        target_kind="space",
        target_id=space_id,
        detail=name,
    )
