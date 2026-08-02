"""Application commands for audited issue-comment lifecycle changes.

A comment is issue content: posting one adds to the record, editing one rewrites what
someone said, deleting one takes words down. All three touched the append-only trail
imperfectly — creating and deleting recorded an event but in a SEPARATE commit from the
row change (a crash between the two lost the event), and EDITING recorded nothing at all,
so a comment body could be silently rewritten over the API. These commands own that
write: the row change and its activity event (plus, for a create, the auto-watch and any
mentions) run in one db.transaction, so a comment and its whole activity footprint land
or roll back together — and an edit is finally on the record.

Authorization stays at the transport boundary, as it does for the other commands: the
routes enforce issue visibility and the author-ownership rule (edit is author-only even
for admins; delete lifts that for admin moderation) BEFORE calling here. The command
trusts the resolved ids and owns the write + audit emission. The event targets the ISSUE
(target_kind "issue"), so it lands on the issue's history and the global feed.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import comments, issue_activity
from athena.core import db, search


class CommentCommandError(Exception):
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND``, so the
    command layer states what went wrong and the transport decides how to say
    it — the same shape every command module written since uses."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def create_comment(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, body: str
) -> dict:
    """Append a comment and record the 'commented' event atomically (with the author's
    auto-watch and any [[user:N]] mentions). Issue visibility and a non-empty body are
    the caller's guards, applied before this runs. Raises sqlite3.IntegrityError if the
    issue or author is unreal (the FKs), unchanged from the bare call."""
    with db.transaction(conn, immediate=True):
        comment = comments.add_comment(
            conn, issue_id=issue_id, author_id=actor_id, body=body, commit=False
        )
        # Index the comment body for full-text search, in the same transaction as the row
        # and its event, so discussion is findable and the index never reflects a rolled-
        # back comment.
        search.index_document(
            conn, kind="issue_comment", source_id=comment["id"], commit=False
        )
        issue_activity.record_commented(
            conn, actor_id=actor_id, issue_id=issue_id, body=body, commit=False
        )
        return comment


def edit_comment(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    comment_id: int,
    body: str,
) -> dict:
    """Rewrite a comment's body and record a 'comment_edited' event atomically —
    previously a silent write. Author-ownership is the caller's guard. Raises
    CommentCommandError(404) if the comment vanished between the author check and the
    write (a race), so a non-edit never redirects as success."""
    with db.transaction(conn, immediate=True):
        updated = comments.update_comment(conn, comment_id, body=body, commit=False)
        if updated is None:
            raise CommentCommandError("not_found", "no such comment")
        # Re-index the rewritten body so search reflects the current text, not the old.
        search.index_document(
            conn, kind="issue_comment", source_id=comment_id, commit=False
        )
        issue_activity.record_comment_edited(
            conn, actor_id=actor_id, issue_id=issue_id, commit=False
        )
        return updated


def delete_comment(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, comment_id: int
) -> bool:
    """Delete a comment and record a 'comment_deleted' event atomically. Returns True if
    a row was removed, False if it had already vanished (so the caller can 404) — no
    event is recorded for a deletion that didn't happen. Author-ownership / admin
    moderation is the caller's guard."""
    with db.transaction(conn, immediate=True):
        removed = comments.delete_comment(conn, comment_id, commit=False)
        if removed:
            # The row is gone, so re-indexing removes its FTS entry (no dangling hit).
            search.index_document(
                conn, kind="issue_comment", source_id=comment_id, commit=False
            )
            issue_activity.record_comment_deleted(
                conn, actor_id=actor_id, issue_id=issue_id, commit=False
            )
        return removed
