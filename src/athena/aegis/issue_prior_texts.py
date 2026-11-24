"""Reading the title and body an issue edit replaced (F10, D20).

``issue_activity.record_edited`` keeps, in the edit's own transaction, the text
each ``issue_edited`` event replaced (``issue_prior_texts``). This module is the
one read of that text for a caller: ``GET /issues/{id}/prior-texts/{event_id}``
and MCP ``get_issue_prior_text``. The issue history (``issue_narrative``) names
each edit and points here; no other answer (the feed, notifications, the desk,
the work-context packet) carries the stored text.

Visibility is the issue's and the event's: the caller must see the issue and
the edit event in its history. A hidden issue, an event the caller cannot see,
an event of another issue, an imported edit and an edit recorded before the
table existed all read the same: missing.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issue_activity
from athena.core import activity, authorship, db, packet_time

EDIT_VERB = "issue_edited"


def _prior_author(
    conn: sqlite3.Connection, *, issue_id: int, event_id: int, actor: dict | None
) -> authorship.Author | None:
    """Who wrote the text this edit replaced: the actor of the newest text event
    (the creation or an edit) before it, as ``authorship.latest_text_authors``
    answers for the current text. ``None`` when that event is imported, missing
    or hidden from the reader."""
    candidates = [
        row
        for verb in issue_activity.ISSUE_TEXT_VERBS
        for row in activity.list_activity(
            conn,
            target_kind="issue",
            target_id=issue_id,
            verb=verb,
            before_id=event_id,
            limit=1,
        )
    ]
    if not candidates:
        return None
    previous = max(candidates, key=lambda row: int(row["id"]))
    if previous["imported_at"] is not None:
        return None
    if activity.get_visible_activity(conn, int(previous["id"]), actor) is None:
        return None
    return authorship.author(conn, previous["actor_id"])


def prior_text_pointer(issue_id: int, event_id: int) -> dict:
    """Where the history sends a reader for one edit's prior text."""
    return {
        "event_id": event_id,
        "via": f"GET /issues/{issue_id}/prior-texts/{event_id}",
        "next": {
            "tool": "get_issue_prior_text",
            "arguments": {"issue_id": issue_id, "event_id": event_id},
        },
    }


def read_prior_text(
    conn: sqlite3.Connection, *, issue_id: int, event_id: int, actor: dict | None
) -> dict | None:
    """The text one edit of this issue replaced, or None when the caller may not
    read it or none was stored. The caller has already checked that the issue
    is visible (a hidden issue is the route's 404 before this runs).

    ``changed`` lists the fields the edit changed; ``prior_title`` and
    ``prior_body`` hold their replaced text, exactly as stored, and null for a
    field the edit left alone. ``edited_by`` and ``edited_at`` say who made the
    edit and when; ``authored_by`` who wrote the replaced text."""
    with db.transaction(conn):
        event = activity.get_visible_activity(conn, event_id, actor)
        if (
            event is None
            or event["verb"] != EDIT_VERB
            or event["target_kind"] != "issue"
            or event["target_id"] != issue_id
            or event["imported_at"] is not None
        ):
            return None
        fact = issue_activity.prior_text_for_event(conn, event_id)
        if fact is None or fact["issue_id"] != issue_id:
            return None
        return {
            "issue_id": issue_id,
            "event_id": event_id,
            "edited_at": packet_time.rfc3339(event["created_at"]),
            "edited_by": {"id": event["actor_id"], "name": event["actor_name"]},
            "changed": fact["changed"],
            "prior_title": fact["prior_title"],
            "prior_body": fact["prior_body"],
            "authored_by": _prior_author(
                conn, issue_id=issue_id, event_id=event_id, actor=actor
            ),
        }
