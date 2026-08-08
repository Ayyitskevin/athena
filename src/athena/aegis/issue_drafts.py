"""Issue drafts — one author's unsaved work in progress.

The Aegis twin of ``mentor/page_drafts.py``, and deliberately its mirror down
to the docstrings: the owner-scoped single-writer shape the repo uses for
personal state (saved filters, watches, notification reads, page drafts). REST
and browser adapters call these functions and translate the result, and **no
audit event is written by design**. See migration 0074 for the ownership
boundary this module enforces and why a draft is not content.

Two rules hold everything together. A draft is readable only by its owner —
not by admins, not by project members — because it is unfinished thinking
rather than a document. And a draft never becomes the issue by itself: turning
one into content is an explicit save through ``issue_commands.update_issue``,
which is where the audit event, the If-Match precondition, and the lifecycle
facts live. Nothing here writes to ``issues``.
"""

from __future__ import annotations

import sqlite3

#: Bounds matching the table's own CHECKs (and 0071's — the drafts are twins),
#: so a refusal is a clear error from this layer rather than an IntegrityError
#: surfacing from SQLite.
MAX_TITLE_CHARS = 500
MAX_BODY_CHARS = 200_000

_COLUMNS = "issue_id, owner_id, title, body, based_on, created_at, updated_at"


class DraftTooLarge(ValueError):
    """A draft exceeded the stored bounds. Transport-neutral; the adapter maps it."""


def get_draft(conn: sqlite3.Connection, *, issue_id: int, owner_id: int) -> dict | None:
    """This author's draft of this issue, or None.

    Keyed by BOTH ids on purpose: there is no read here that can return someone
    else's draft, so no caller can leak one by forgetting a filter.
    """
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM issue_drafts WHERE issue_id = ? AND owner_id = ?",
        (issue_id, owner_id),
    ).fetchone()
    return None if row is None else dict(row)


def save_draft(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    owner_id: int,
    title: str,
    body: str,
    based_on: str,
    commit: bool = True,
) -> dict:
    """Record where this author has got to on this issue.

    One row per (issue, author): saving again overwrites it, because a draft is
    a position rather than a history — the issue's own history is the activity
    trail, and it stays untouched by this.

    ``based_on`` is the issue's ETag as the author last saw it. It is not a
    lock: two people may draft the same issue at once and neither blocks the
    other. It exists so a draft that has fallen behind can be SHOWN to be
    behind instead of silently overwriting whatever landed in the meantime.
    """
    if len(title) > MAX_TITLE_CHARS:
        raise DraftTooLarge(f"title must be at most {MAX_TITLE_CHARS} characters")
    if len(body) > MAX_BODY_CHARS:
        raise DraftTooLarge(f"body must be at most {MAX_BODY_CHARS} characters")
    conn.execute(
        "INSERT INTO issue_drafts (issue_id, owner_id, title, body, based_on) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(issue_id, owner_id) DO UPDATE SET "
        "title = excluded.title, body = excluded.body, "
        "based_on = excluded.based_on, updated_at = datetime('now')",
        (issue_id, owner_id, title, body, based_on),
    )
    if commit:
        conn.commit()
    saved = get_draft(conn, issue_id=issue_id, owner_id=owner_id)
    assert saved is not None  # the upsert above just made this row durable
    return saved


def discard_draft(
    conn: sqlite3.Connection, *, issue_id: int, owner_id: int, commit: bool = True
) -> bool:
    """Drop this author's draft. True when one existed.

    Called on an explicit discard, and on a successful save — once the text IS
    the issue, a draft of it is just a stale copy of content that now has a
    real home on the trail.
    """
    cur = conn.execute(
        "DELETE FROM issue_drafts WHERE issue_id = ? AND owner_id = ?",
        (issue_id, owner_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def differs_from(draft: dict, issue: dict) -> bool:
    """Whether this draft actually says something the issue does not.

    An autosave that matches the saved issue is not unsaved work, and offering
    to restore it would be noise — worse, it would make an author wonder what
    they had forgotten.
    """
    return draft["title"] != issue["title"] or draft["body"] != (issue["body"] or "")


def is_stale(draft: dict, current_etag: str) -> bool:
    """Whether the issue moved under this draft since the author last touched it.

    A stale draft is not wrong and is never discarded — it is the author's
    work. It just cannot be restored innocently, because doing so would drop
    whatever someone else saved in between.
    """
    return bool(draft["based_on"]) and draft["based_on"] != current_etag
