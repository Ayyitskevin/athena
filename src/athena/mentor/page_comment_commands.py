"""Application commands for audited page-comment lifecycle changes.

The Mentor twin of aegis/comment_commands.py. A page comment is page content: posting
one adds to the discussion, editing one rewrites what someone said, deleting one takes
words down. Creating and deleting recorded an event but in a SEPARATE commit from the
row change (a crash between the two lost the event), and EDITING recorded nothing at
all, so a page-comment body could be silently rewritten over the API. These commands
own that write: the row change and its activity event (plus, for a create, the
auto-watch and any mentions) run in one db.transaction.

They also own their authorization, exactly as the aegis twin does: each entry point
takes a resolved ``actor`` and checks page visibility (a page in a private space
reads as missing, never forbidden) and the author-ownership rule — edit is
author-only even for admins, delete lifts that for admin moderation — INSIDE its
own write transaction, so the check and the write it guards cannot straddle a
boundary and a caller reaching the command directly is refused rather than trusted.
Role and token scope stay at the transport. The event targets the PAGE
(target_kind "page").
"""

from __future__ import annotations

import sqlite3

from athena.core import access, db, identity, search
from athena.mentor import page_activity, page_comments, pages


class PageCommentCommandError(Exception):
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND``, so the
    command layer states what went wrong and the transport decides how to say
    it — the same shape every command module written since uses."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _visible_page_or_refuse(
    conn: sqlite3.Connection, actor: dict, page_id: int
) -> dict:
    """The page, if this actor may see it — else ``not_found``.

    Called INSIDE the command's write transaction (the aegis twin's
    ``_visible_issue_or_refuse``, verbatim in spirit): a hidden page reads as
    missing so its existence never leaks through a write path."""
    page = pages.get_page(conn, page_id)
    if page is None or not access.can_see_space(conn, actor, page["space_id"]):
        raise PageCommentCommandError("not_found", "no such page")
    return page


def _own_comment_or_refuse(
    conn: sqlite3.Connection,
    actor: dict,
    page_id: int,
    comment_id: int,
    *,
    allow_admin: bool,
) -> dict:
    """The comment, if it belongs to this page and this actor may change it.

    ``not_found`` when it is missing or hangs off another page; ``forbidden``
    when someone other than its author tries to change it. ``allow_admin`` lifts
    that for moderation and is passed ONLY by delete: removing someone's words is
    moderation, but rewriting them would put words in their mouth, so edit stays
    strictly author-only even for an admin. The delete is still audited to the
    admin, so the moderation stays on the record."""
    existing = page_comments.get_comment(conn, comment_id)
    if existing is None or existing["page_id"] != page_id:
        raise PageCommentCommandError("not_found", "no such comment")
    if existing["author_id"] != actor["id"] and not (
        allow_admin and identity.is_admin(actor)
    ):
        raise PageCommentCommandError("forbidden", "not the comment author")
    return existing


def create_page_comment(
    conn: sqlite3.Connection, *, actor: dict, page_id: int, body: str
) -> dict:
    """Append a page comment and record the 'page_commented' event atomically (with the
    author's auto-watch and any [[user:N]] mentions).

    Owns its authorization: the page must exist and be visible to ``actor``,
    checked inside this transaction. A non-empty body stays the transport's guard —
    that is request shape, not a trust boundary."""
    with db.transaction(conn, immediate=True):
        _visible_page_or_refuse(conn, actor, page_id)
        actor_id = actor["id"]
        comment = page_comments.add_comment(
            conn, page_id=page_id, author_id=actor_id, body=body, commit=False
        )
        # Index the comment body for full-text search in the same transaction as the row
        # and its event, so page discussion is findable and the index can't reflect a
        # rolled-back comment.
        search.index_document(
            conn, kind="page_comment", source_id=comment["id"], commit=False
        )
        page_activity.record_page_commented(
            conn, actor_id=actor_id, page_id=page_id, body=body, commit=False
        )
        return comment


def edit_page_comment(
    conn: sqlite3.Connection, *, actor: dict, page_id: int, comment_id: int, body: str
) -> dict:
    """Rewrite a page comment's body and record a 'page_comment_edited' event atomically
    — previously a silent write.

    Owns its authorization: page visibility and author-ownership are both checked
    inside this transaction, so the ownership test and the write cannot straddle a
    boundary. Admins are NOT excepted here (see ``_own_comment_or_refuse``)."""
    with db.transaction(conn, immediate=True):
        _visible_page_or_refuse(conn, actor, page_id)
        _own_comment_or_refuse(conn, actor, page_id, comment_id, allow_admin=False)
        actor_id = actor["id"]
        updated = page_comments.update_comment(
            conn, comment_id, body=body, commit=False
        )
        if updated is None:
            raise PageCommentCommandError("not_found", "no such comment")
        # Re-index the rewritten body so search reflects the current text, not the old.
        search.index_document(
            conn, kind="page_comment", source_id=comment_id, commit=False
        )
        page_activity.record_page_comment_edited(
            conn, actor_id=actor_id, page_id=page_id, commit=False
        )
        return updated


def delete_page_comment(
    conn: sqlite3.Connection, *, actor: dict, page_id: int, comment_id: int
) -> bool:
    """Delete a page comment and record a 'page_comment_deleted' event atomically.
    Returns True if a row was removed, False if it had already vanished (so the caller
    can 404) — no event for a deletion that didn't happen.

    Owns its authorization: page visibility, and author-ownership with the admin
    moderation override, both inside this transaction."""
    with db.transaction(conn, immediate=True):
        _visible_page_or_refuse(conn, actor, page_id)
        _own_comment_or_refuse(conn, actor, page_id, comment_id, allow_admin=True)
        actor_id = actor["id"]
        removed = page_comments.delete_comment(conn, comment_id, commit=False)
        if removed:
            # The row is gone, so re-indexing removes its FTS entry (no dangling hit).
            search.index_document(
                conn, kind="page_comment", source_id=comment_id, commit=False
            )
            page_activity.record_page_comment_deleted(
                conn, actor_id=actor_id, page_id=page_id, commit=False
            )
        return removed
