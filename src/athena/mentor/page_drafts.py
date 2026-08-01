"""Page drafts — one author's unsaved work in progress.

The owner-scoped single-writer shape the repo already uses for personal state
(saved filters, watches, notification reads): REST and browser adapters both
call these functions and translate the result, and **no audit event is written
by design**. See migration 0071 for the ownership boundary this module enforces
and why a draft is deliberately not content.

Two rules hold everything together. A draft is readable only by its owner — not
by admins, not by space members — because it is unfinished thinking rather than
a document. And a draft never becomes a page by itself: turning one into content
is an explicit save through ``page_commands.edit_page``, which is where
versioning, indexing, and the audit event live. Nothing here writes to ``pages``.
"""

from __future__ import annotations

import sqlite3

#: Bounds matching the table's own CHECKs, so a refusal is a clear error from
#: this layer rather than an IntegrityError surfacing from SQLite.
MAX_TITLE_CHARS = 500
MAX_BODY_CHARS = 200_000

_COLUMNS = "page_id, owner_id, title, body, based_on, created_at, updated_at"


class DraftTooLarge(ValueError):
    """A draft exceeded the stored bounds. Transport-neutral; the adapter maps it."""


def get_draft(conn: sqlite3.Connection, *, page_id: int, owner_id: int) -> dict | None:
    """This author's draft of this page, or None.

    Keyed by BOTH ids on purpose: there is no read here that can return someone
    else's draft, so no caller can leak one by forgetting a filter.
    """
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM page_drafts WHERE page_id = ? AND owner_id = ?",
        (page_id, owner_id),
    ).fetchone()
    return None if row is None else dict(row)


# NOTE: a "list my drafts" reader was shipped here once, unused — removed
# rather than left as dead code (review finding). The 0071 owner index that
# would serve it remains, as migrations are forward-only; the reader returns
# with the surface that actually renders it.


def save_draft(
    conn: sqlite3.Connection,
    *,
    page_id: int,
    owner_id: int,
    title: str,
    body: str,
    based_on: str,
    commit: bool = True,
) -> dict:
    """Record where this author has got to on this page.

    One row per (page, author): saving again overwrites it, because a draft is a
    position rather than a history — the page's own history is what
    ``page_versions`` is for, and it stays untouched by this.

    ``based_on`` is the page's ETag as the author last saw it. It is not a lock:
    two people may draft the same page at once and neither blocks the other. It
    exists so a draft that has fallen behind can be SHOWN to be behind instead of
    silently overwriting whatever landed in the meantime.
    """
    if len(title) > MAX_TITLE_CHARS:
        raise DraftTooLarge(f"title must be at most {MAX_TITLE_CHARS} characters")
    if len(body) > MAX_BODY_CHARS:
        raise DraftTooLarge(f"body must be at most {MAX_BODY_CHARS} characters")
    conn.execute(
        "INSERT INTO page_drafts (page_id, owner_id, title, body, based_on) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(page_id, owner_id) DO UPDATE SET "
        "title = excluded.title, body = excluded.body, "
        "based_on = excluded.based_on, updated_at = datetime('now')",
        (page_id, owner_id, title, body, based_on),
    )
    if commit:
        conn.commit()
    saved = get_draft(conn, page_id=page_id, owner_id=owner_id)
    assert saved is not None  # the upsert above just made this row durable
    return saved


def discard_draft(
    conn: sqlite3.Connection, *, page_id: int, owner_id: int, commit: bool = True
) -> bool:
    """Drop this author's draft. True when one existed.

    Called on an explicit discard, and on a successful save — once the text IS
    the page, a draft of it is just a stale copy of content that now has a real
    home and a version row.
    """
    cur = conn.execute(
        "DELETE FROM page_drafts WHERE page_id = ? AND owner_id = ?",
        (page_id, owner_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def differs_from(draft: dict, page: dict) -> bool:
    """Whether this draft actually says something the page does not.

    An autosave that matches the saved page is not unsaved work, and offering to
    restore it would be noise — worse, it would make an author wonder what they
    had forgotten.
    """
    return draft["title"] != page["title"] or draft["body"] != page["body"]


def is_stale(draft: dict, current_etag: str) -> bool:
    """Whether the page moved under this draft since the author last touched it.

    A stale draft is not wrong and is never discarded — it is the author's work.
    It just cannot be restored innocently, because doing so would drop whatever
    someone else saved in between.
    """
    return bool(draft["based_on"]) and draft["based_on"] != current_etag
