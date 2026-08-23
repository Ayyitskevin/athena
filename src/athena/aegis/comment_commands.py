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

from athena.aegis import comments, issue_activity, issue_commands, issues
from athena.core import access, db, identity, search, users


class CommentCommandError(Exception):
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND``, so the
    command layer states what went wrong and the transport decides how to say
    it — the same shape every command module written since uses."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _visible_issue_or_refuse(conn: sqlite3.Connection, actor: dict, issue_id: int):
    """The issue, if this actor may see it — else ``not_found``.

    Called INSIDE the command's write transaction, which is the point of moving it
    here: a check the transport ran beforehand can go stale between the check and
    the write, and a caller reaching the command directly skipped it entirely. A
    hidden issue reads as missing so its existence never leaks through a write.
    """
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise CommentCommandError("not_found", "no such issue")
    return issue


def _own_comment_or_refuse(
    conn: sqlite3.Connection,
    actor: dict,
    issue_id: int,
    comment_id: int,
    *,
    allow_admin: bool,
) -> dict:
    """The comment, if it belongs to this issue and this actor may change it.

    ``not_found`` when it is missing or hangs off another issue; ``forbidden``
    when someone other than its author tries to change it. ``allow_admin`` lifts
    that for moderation and is passed ONLY by delete: removing someone's words is
    moderation, but rewriting them would put words in their mouth, so edit stays
    strictly author-only even for an admin. The delete is still audited to the
    admin, so the moderation stays on the record.
    """
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
        raise CommentCommandError("not_found", "no such comment")
    if existing["author_id"] != actor["id"] and not (
        allow_admin and identity.is_admin(actor)
    ):
        raise CommentCommandError("forbidden", "not the comment author")
    return existing


def create_comment(
    conn: sqlite3.Connection, *, actor: dict, issue_id: int, body: str
) -> dict:
    """Append a comment and record the 'commented' event atomically (with the author's
    auto-watch and any [[user:N]] mentions).

    Owns its authorization now: the issue must exist and be visible to ``actor``,
    checked inside this transaction. A non-empty body stays the transport's guard
    — that is request shape, not a trust boundary."""
    with db.transaction(conn, immediate=True):
        _visible_issue_or_refuse(conn, actor, issue_id)
        actor_id = actor["id"]
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
    actor: dict,
    issue_id: int,
    comment_id: int,
    body: str,
) -> dict:
    """Rewrite a comment's body and record a 'comment_edited' event atomically.

    Owns its authorization: issue visibility and author-ownership are both checked
    inside this transaction, so the ownership test and the write cannot straddle a
    boundary — the race that made the old transport-side check a real gap. Admins
    are NOT excepted here (see ``_own_comment_or_refuse``)."""
    with db.transaction(conn, immediate=True):
        _visible_issue_or_refuse(conn, actor, issue_id)
        _own_comment_or_refuse(conn, actor, issue_id, comment_id, allow_admin=False)
        actor_id = actor["id"]
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
    conn: sqlite3.Connection, *, actor: dict, issue_id: int, comment_id: int
) -> bool:
    """Delete a comment and record a 'comment_deleted' event atomically. Returns True if
    a row was removed, False if it had already vanished (so the caller can 404) — no
    event is recorded for a deletion that didn't happen.

    Owns its authorization: issue visibility, and author-ownership with the admin
    moderation override, both inside this transaction."""
    with db.transaction(conn, immediate=True):
        _visible_issue_or_refuse(conn, actor, issue_id)
        _own_comment_or_refuse(conn, actor, issue_id, comment_id, allow_admin=True)
        actor_id = actor["id"]
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


def create_comment_as_automation(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, body: str
) -> dict:
    """Post a rule's comment under explicit system policy.

    Automation acts on the target an enabled rule selected rather than as an actor
    who can see it, so it cannot go through :func:`create_comment`'s visibility
    check. Keep that deliberate bypass narrow, exactly as
    ``issue_commands.update_issue_as_automation`` does: only the passwordless
    in-process Automation agent may use it; every browser and API caller goes
    through the authorizing entry point.
    """
    actor = users.get_user(conn, actor_id)
    if (
        actor is None
        or actor.get("email") != issue_commands.AUTOMATION_ACTOR_EMAIL
        or not actor.get("is_agent")
    ):
        raise CommentCommandError("forbidden", "automation actor required")
    with db.transaction(conn, immediate=True):
        comment = comments.add_comment(
            conn, issue_id=issue_id, author_id=actor_id, body=body, commit=False
        )
        search.index_document(
            conn, kind="issue_comment", source_id=comment["id"], commit=False
        )
        issue_activity.record_commented(
            conn, actor_id=actor_id, issue_id=issue_id, body=body, commit=False
        )
        return comment
