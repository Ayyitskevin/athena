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

from athena.core import activity, labels, notifications
from athena.mentor import pages


def record_page_created(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    title: str,
    body: str = "",
    commit: bool = True,
) -> None:
    """A page was created — the first audit fact in its history. The creator starts
    watching it (Mentor is a shared wiki, but you follow what you start); anyone
    named by [[user:N]] in the body is mentioned (notified + auto-watched).

    ``commit=False`` composes this inside the audited create command's transaction so
    the page row, its event, the creator's auto-watch, and any mentions land together."""
    event = activity.record(
        conn,
        actor_id=actor_id,
        verb="page_created",
        target_kind="page",
        target_id=page_id,
        detail=title,
        commit=commit,
    )
    notifications.watch(conn, actor_id, "page", page_id, commit=commit)
    notifications.process_mentions(
        conn, event_id=event["id"], actor_id=actor_id, text=body, commit=commit
    )


def record_page_edited(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    before: dict,
    after: dict,
    commit: bool = True,
) -> None:
    """A page's content changed. No-op if neither title nor body actually moved —
    a save with no edits is not a lifecycle moment (mirrors the issue no-op rule,
    and Mentor itself cuts no new version for an unchanged save).

    ``commit=False`` composes this inside the audited page-edit command's transaction:
    the event, the editor's auto-watch, and any mentions defer their commit to the
    command owner so the page row and its audit footprint land together."""
    if before["title"] == after["title"] and before["body"] == after["body"]:
        return
    event = activity.record(
        conn,
        actor_id=actor_id,
        verb="page_edited",
        target_kind="page",
        target_id=after["id"],
        detail=after["title"],
        commit=commit,
    )
    # Editing is participation — the editor starts watching the page.
    notifications.watch(conn, actor_id, "page", after["id"], commit=commit)
    # A newly-added [[user:N]] in the edited body mentions that person.
    notifications.process_mentions(
        conn, event_id=event["id"], actor_id=actor_id, text=after["body"], commit=commit
    )


def record_page_deleted(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    title: str,
    commit: bool = True,
) -> None:
    """A page was removed — who took the document down, with its title preserved in
    the detail since the page row itself is gone. ``commit=False`` composes inside the
    audited delete command's transaction so the row deletes and this event land
    together."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_deleted",
        target_kind="page",
        target_id=page_id,
        detail=title,
        commit=commit,
    )


def record_page_moved(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    before_parent_id: int | None,
    after_parent_id: int | None,
    commit: bool = True,
) -> None:
    """A page was re-parented. No-op if the parent didn't actually change (set_parent
    matches the row even when the value is unchanged, so the no-op guard lives here,
    like the issue project-move rule). The detail names the new parent so the feed
    can say where it landed; an empty detail means it was moved to the top level.
    ``commit=False`` composes inside the audited move command's transaction."""
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
        commit=commit,
    )


def record_page_commented(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    body: str = "",
    commit: bool = True,
) -> None:
    """Record that someone commented on the page — the page twin of the issue
    `commented` event. Targets the page (so it lands on the page's Activity and the
    global feed links there); the comment body lives on the page, not duplicated into
    the trail's detail. Commenting is participation, so the commenter starts watching
    the page, and anyone named by [[user:N]] in the comment is mentioned.

    ``commit=False`` composes this inside a command's transaction: the event, the watch,
    and the mentions all defer their commit to the command owner."""
    event = activity.record(
        conn,
        actor_id=actor_id,
        verb="page_commented",
        target_kind="page",
        target_id=page_id,
        commit=commit,
    )
    notifications.watch(conn, actor_id, "page", page_id, commit=commit)
    notifications.process_mentions(
        conn, event_id=event["id"], actor_id=actor_id, text=body, commit=commit
    )


def record_page_comment_edited(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, commit: bool = True
) -> None:
    """Record that a page comment's body was edited — the previously-silent half of the
    comment lifecycle (who rewrote content). No detail: the current body lives on the
    comment, and the point is that the change is now on the record at all. ``commit=False``
    composes inside a command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_comment_edited",
        target_kind="page",
        target_id=page_id,
        commit=commit,
    )


def record_page_comment_deleted(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, commit: bool = True
) -> None:
    """Record that a comment was removed from the page — the audit-worthy half of the
    comment lifecycle (who took content down). No detail: the comment is gone.
    ``commit=False`` composes inside a command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_comment_deleted",
        target_kind="page",
        target_id=page_id,
        commit=commit,
    )


def record_page_label_added(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    label_id: int,
    commit: bool = True,
) -> None:
    """Record that a label was attached to the page, stamped with the label's name.
    Caller records only when the attach created a new pairing. ``commit=False``
    lets the command owner fold the event into the attach transaction."""
    label = labels.get_label(conn, label_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_labeled",
        target_kind="page",
        target_id=page_id,
        detail=label["name"] if label else "",
        commit=commit,
    )


def record_page_label_removed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    label_id: int,
    commit: bool = True,
) -> None:
    """Record that a label was detached from the page. Caller records only when a
    pairing was actually removed. ``commit=False`` lets the command owner fold the
    event into the detach transaction."""
    label = labels.get_label(conn, label_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_unlabeled",
        target_kind="page",
        target_id=page_id,
        detail=label["name"] if label else "",
        commit=commit,
    )


def record_page_archive_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    before: str | None,
    after: str | None,
    commit: bool = True,
) -> None:
    """Record a page archive (soft-delete) or restore — the Mentor twin of the issue
    archive event. before/after are the page's archived_at values (None means active,
    a timestamp means archived), so we compare PRESENCE, not the exact time:
    re-archiving an already-archived page re-stamps it but isn't a lifecycle change and
    records nothing. ``commit=False`` lets the audited command fold the flip and this
    event into one transaction."""
    if (before is not None) == (after is not None):
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_archived" if after is not None else "page_unarchived",
        target_kind="page",
        target_id=page_id,
        commit=commit,
    )


def record_page_restored(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    version: int,
    before: dict,
    after: dict,
    commit: bool = True,
) -> None:
    """A page's content was rolled back to a prior revision. Recorded as its own verb
    (not page_edited) so the trail says "restored v3", not just "edited". No-op if the
    restored content was identical to the live row — restore_version returns the page
    untouched in that case (it never files a redundant version), and so we record
    nothing either. ``commit=False`` composes inside the audited restore command's
    transaction."""
    if before["title"] == after["title"] and before["body"] == after["body"]:
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="page_restored",
        target_kind="page",
        target_id=page_id,
        detail=f"v{version}",
        commit=commit,
    )
