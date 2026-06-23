"""Data access for pages — a single document inside a space.

All page SQL lives here, mirroring mentor/spaces.py and aegis/issues.py. A page
belongs to one space and may nest under a parent page (parent_id) to form a tree.
This covers create + read + edit; each edit snapshots the prior content into
page_versions (the page's history), so the live `pages` row is always the present
and page_versions is the past. The cross-space tree rule (a parent must share the
page's space) is enforced at the API boundary, not here.
"""
from __future__ import annotations

import sqlite3


def create_page(
    conn: sqlite3.Connection,
    *,
    space_id: int,
    title: str,
    created_by: int,
    body: str = "",
    parent_id: int | None = None,
) -> dict:
    """Insert a page and return it. The foreign keys refuse an orphan: space_id
    must be a real space, parent_id (when given) a real page, created_by a real
    user. A new page is its own current revision, so updated_by/updated_at start
    equal to the creator and creation time — this is what the first edit will
    snapshot as version 1."""
    cur = conn.execute(
        "INSERT INTO pages (space_id, parent_id, title, body, created_by, "
        "updated_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        (space_id, parent_id, title, body, created_by, created_by),
    )
    conn.commit()
    return get_page(conn, cur.lastrowid)


def get_page(conn: sqlite3.Connection, page_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    return dict(row) if row else None


def list_pages_in_space(conn: sqlite3.Connection, space_id: int) -> list[dict]:
    """Every page in a space, alphabetical by title. Each row carries its
    parent_id so a caller can assemble the tree; this returns the flat set."""
    rows = conn.execute(
        "SELECT * FROM pages WHERE space_id = ? ORDER BY title COLLATE NOCASE",
        (space_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def update_page(
    conn: sqlite3.Connection,
    page_id: int,
    *,
    editor_id: int,
    title: str | None = None,
    body: str | None = None,
) -> dict | None:
    """Edit a page's title and/or body, snapshotting the prior content into its
    history first. Partial: only the fields passed as non-None change. Returns the
    updated page, or None if no page has that id (so the boundary can 404). With no
    changing fields the page is returned untouched and no version is cut.

    The snapshot-then-overwrite happens in one transaction so history can never
    diverge from the live page. The version number is dense per page — one more
    than the count already stored — so versions read 1, 2, 3… The snapshot carries
    the SUPERSEDED revision's own author and time (the page's current updated_by/
    updated_at), not the editor doing the replacing; the new editor stamps the
    fresh live row. Column names in the SET clause are hardcoded literals, never
    caller input, so the f-string is safe; values stay parameterized."""
    page = get_page(conn, page_id)
    if page is None:
        return None

    fields = {
        col: val
        for col, val in (("title", title), ("body", body))
        if val is not None
    }
    if not fields:
        return page  # nothing to change — no new revision

    next_version = conn.execute(
        "SELECT COUNT(*) AS n FROM page_versions WHERE page_id = ?", (page_id,)
    ).fetchone()["n"] + 1
    conn.execute(
        "INSERT INTO page_versions "
        "(page_id, version, title, body, edited_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            page_id,
            next_version,
            page["title"],
            page["body"],
            page["updated_by"],
            page["updated_at"],
        ),
    )

    assignments = ", ".join(f"{col} = ?" for col in fields)
    conn.execute(
        f"UPDATE pages SET {assignments}, updated_by = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (*fields.values(), editor_id, page_id),
    )
    conn.commit()
    return get_page(conn, page_id)


def list_page_versions(conn: sqlite3.Connection, page_id: int) -> list[dict]:
    """A page's superseded revisions, newest first (highest version first). Empty
    for a page never edited. Does NOT include the live current revision — that is
    the `pages` row itself."""
    rows = conn.execute(
        "SELECT * FROM page_versions WHERE page_id = ? ORDER BY version DESC",
        (page_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_page_version(
    conn: sqlite3.Connection, page_id: int, version: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM page_versions WHERE page_id = ? AND version = ?",
        (page_id, version),
    ).fetchone()
    return dict(row) if row else None
