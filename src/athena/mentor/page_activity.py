"""Recording page lifecycle events onto the activity trail.

The Mentor twin of aegis/issue_activity.py: one owner for "what counts as a page
event, and how is it phrased", so the two surfaces that mutate a page — the REST
API (mentor/api.py) and the web forms (web/mentor.py) — record the SAME facts the
same way instead of each growing its own copy.

These helpers only ever *append* (through core.activity.record); they never touch
the page. The caller does the write, then hands us the result (and, for an edit,
the before/after) so we record the fact. "Record only on real change" lives here:
an edit that changed neither title nor body writes nothing, on either surface.

Events target the page (target_kind="page"), so they land on the page's own
Activity section and the global feed links back to it. A page_deleted row outlives
the page it names — the trail is append-only and the activity table holds no FK to
the (now gone) page.
"""
from __future__ import annotations

import sqlite3

from athena.core import activity


def record_page_created(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, title: str
) -> None:
    """A page was created — the first audit fact in its history."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_created",
        target_kind="page",
        target_id=page_id,
        detail=title,
    )


def record_page_edited(
    conn: sqlite3.Connection, *, actor_id: int, before: dict, after: dict
) -> None:
    """A page's content changed. No-op if neither title nor body actually moved —
    a save with no edits is not a lifecycle moment (mirrors the issue no-op rule,
    and Mentor itself cuts no new version for an unchanged save)."""
    if before["title"] == after["title"] and before["body"] == after["body"]:
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_edited",
        target_kind="page",
        target_id=after["id"],
        detail=after["title"],
    )


def record_page_deleted(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, title: str
) -> None:
    """A page was removed — who took the document down, with its title preserved in
    the detail since the page row itself is gone."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_deleted",
        target_kind="page",
        target_id=page_id,
        detail=title,
    )
