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

from athena.core import activity, notifications
from athena.mentor import pages


def record_page_created(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    title: str,
    body: str = "",
) -> None:
    """A page was created — the first audit fact in its history. The creator starts
    watching it (Mentor is a shared wiki, but you follow what you start); anyone
    named by [[user:N]] in the body is mentioned (notified + auto-watched)."""
    event = activity.record(
        conn,
        actor_id=actor_id,
        verb="page_created",
        target_kind="page",
        target_id=page_id,
        detail=title,
    )
    notifications.watch(conn, actor_id, "page", page_id)
    notifications.process_mentions(
        conn, event_id=event["id"], actor_id=actor_id, text=body
    )


def record_page_edited(
    conn: sqlite3.Connection, *, actor_id: int, before: dict, after: dict
) -> None:
    """A page's content changed. No-op if neither title nor body actually moved —
    a save with no edits is not a lifecycle moment (mirrors the issue no-op rule,
    and Mentor itself cuts no new version for an unchanged save)."""
    if before["title"] == after["title"] and before["body"] == after["body"]:
        return
    event = activity.record(
        conn,
        actor_id=actor_id,
        verb="page_edited",
        target_kind="page",
        target_id=after["id"],
        detail=after["title"],
    )
    # Editing is participation — the editor starts watching the page.
    notifications.watch(conn, actor_id, "page", after["id"])
    # A newly-added [[user:N]] in the edited body mentions that person.
    notifications.process_mentions(
        conn, event_id=event["id"], actor_id=actor_id, text=after["body"]
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


def record_page_moved(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    before_parent_id: int | None,
    after_parent_id: int | None,
) -> None:
    """A page was re-parented. No-op if the parent didn't actually change (set_parent
    matches the row even when the value is unchanged, so the no-op guard lives here,
    like the issue project-move rule). The detail names the new parent so the feed
    can say where it landed; an empty detail means it was moved to the top level."""
    if before_parent_id == after_parent_id:
        return
    if after_parent_id is None:
        detail = ""
    else:
        parent = pages.get_page(conn, after_parent_id)
        detail = parent["title"] if parent else ""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_moved",
        target_kind="page",
        target_id=page_id,
        detail=detail,
    )


def record_page_restored(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    version: int,
    before: dict,
    after: dict,
) -> None:
    """A page's content was rolled back to a prior revision. Recorded as its own verb
    (not page_edited) so the trail says "restored v3", not just "edited". No-op if the
    restored content was identical to the live row — restore_version returns the page
    untouched in that case (it never files a redundant version), and so we record
    nothing either."""
    if before["title"] == after["title"] and before["body"] == after["body"]:
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_restored",
        target_kind="page",
        target_id=page_id,
        detail=f"v{version}",
    )
