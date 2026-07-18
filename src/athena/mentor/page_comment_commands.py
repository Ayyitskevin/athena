"""Application commands for audited page-comment lifecycle changes.

The Mentor twin of aegis/comment_commands.py. A page comment is page content: posting
one adds to the discussion, editing one rewrites what someone said, deleting one takes
words down. Creating and deleting recorded an event but in a SEPARATE commit from the
row change (a crash between the two lost the event), and EDITING recorded nothing at
all, so a page-comment body could be silently rewritten over the API. These commands
own that write: the row change and its activity event (plus, for a create, the
auto-watch and any mentions) run in one db.transaction.

Authorization (page visibility and the author-ownership rule — edit is author-only even
for admins; delete lifts that for admin moderation) stays at the transport boundary, as
it does for the aegis comment commands. The event targets the PAGE (target_kind "page").
"""

from __future__ import annotations

import sqlite3

from athena.core import db
from athena.mentor import page_activity, page_comments


class PageCommentCommandError(Exception):
    """A transport-neutral rejection. ``status_code`` lets each adapter map it (404 for
    a comment that vanished between the boundary's author check and the write)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def create_page_comment(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, body: str
) -> dict:
    """Append a page comment and record the 'page_commented' event atomically (with the
    author's auto-watch and any [[user:N]] mentions). Page visibility and a non-empty
    body are the caller's guards. Raises sqlite3.IntegrityError if the page or author is
    unreal (the FKs), unchanged from the bare call."""
    with db.transaction(conn, immediate=True):
        comment = page_comments.add_comment(
            conn, page_id=page_id, author_id=actor_id, body=body, commit=False
        )
        page_activity.record_page_commented(
            conn, actor_id=actor_id, page_id=page_id, body=body, commit=False
        )
        return comment


def edit_page_comment(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, comment_id: int, body: str
) -> dict:
    """Rewrite a page comment's body and record a 'page_comment_edited' event atomically
    — previously a silent write. Author-ownership is the caller's guard. Raises
    PageCommentCommandError(404) if the comment vanished between the author check and the
    write (a race)."""
    with db.transaction(conn, immediate=True):
        updated = page_comments.update_comment(conn, comment_id, body=body, commit=False)
        if updated is None:
            raise PageCommentCommandError("no such comment", status_code=404)
        page_activity.record_page_comment_edited(
            conn, actor_id=actor_id, page_id=page_id, commit=False
        )
        return updated


def delete_page_comment(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, comment_id: int
) -> bool:
    """Delete a page comment and record a 'page_comment_deleted' event atomically.
    Returns True if a row was removed, False if it had already vanished (so the caller
    can 404) — no event for a deletion that didn't happen. Author-ownership / admin
    moderation is the caller's guard."""
    with db.transaction(conn, immediate=True):
        removed = page_comments.delete_comment(conn, comment_id, commit=False)
        if removed:
            page_activity.record_page_comment_deleted(
                conn, actor_id=actor_id, page_id=page_id, commit=False
            )
        return removed
