"""Data access for spaces — a top-level container for documentation pages.

All space SQL lives here, mirroring aegis/projects.py. A space is the Mentor
equivalent of an Aegis project, but identified by a short KEY (e.g. "ENG") rather
than by its name, because pages will later be addressed by that key. Pages (a
later slice) will reference a space; this module owns only the space row itself.
"""
from __future__ import annotations

import sqlite3


def create_space(
    conn: sqlite3.Connection,
    *,
    key: str,
    name: str,
    created_by: int,
    description: str = "",
    commit: bool = True,
) -> dict:
    """Insert a space and return it. Raises sqlite3.IntegrityError if a space with
    this key already exists (key is UNIQUE, case-insensitive) or if created_by
    isn't a real user (the foreign key refuses the orphan). ``commit=False`` composes
    this inside the audited create command's transaction so the row and its
    ``space_created`` event land together."""
    cur = conn.execute(
        "INSERT INTO spaces (id, key, name, description, created_by) "
        "SELECT next_id, ?, ?, ?, ? FROM activity_target_id_sequences "
        "WHERE target_kind = 'space'",
        (key, name, description, created_by),
    )
    space_id = cur.lastrowid
    if commit:
        conn.commit()
    return get_space(conn, space_id)


def get_space(conn: sqlite3.Connection, space_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
    return dict(row) if row else None


def get_space_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    """Look a space up by its key (case-insensitive — the column is COLLATE NOCASE)."""
    row = conn.execute("SELECT * FROM spaces WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def list_spaces(
    conn: sqlite3.Connection, visible_space_ids: set[int] | None = None
) -> list[dict]:
    """Spaces the viewer may see, alphabetical by name. None means no gating — an
    admin, or an internal caller that needs every space; a set restricts to those ids;
    an empty set yields nothing. Callers get the set from access.visible_space_filter."""
    if visible_space_ids is None:
        rows = conn.execute(
            "SELECT * FROM spaces ORDER BY name COLLATE NOCASE"
        ).fetchall()
    elif not visible_space_ids:
        return []
    else:
        placeholders = ",".join("?" for _ in visible_space_ids)
        rows = conn.execute(
            f"SELECT * FROM spaces WHERE id IN ({placeholders}) "
            "ORDER BY name COLLATE NOCASE",
            list(visible_space_ids),
        ).fetchall()
    return [dict(row) for row in rows]


def update_space(
    conn: sqlite3.Connection,
    space_id: int,
    *,
    key: str | None = None,
    name: str | None = None,
    description: str | None = None,
    commit: bool = True,
) -> dict | None:
    """Partial edit: only the fields passed as non-None are written, the rest are
    left as-is (mirrors aegis.update_project). Returns the updated space, or None if
    no space has that id. Raises sqlite3.IntegrityError if the new key collides with
    another space (key is UNIQUE, case-insensitive) — the boundary checks for a
    duplicate first and returns a clean 409. The key is normalized to UPPERCASE here
    too, so "eng" and "ENG" stay one identity exactly as create does. ``commit=False``
    composes this inside the audited edit command's transaction."""
    sets, params = [], []
    if key is not None:
        sets.append("key = ?")
        params.append(key.upper())
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if not sets:
        return get_space(conn, space_id)
    params.append(space_id)
    cur = conn.execute(
        f"UPDATE spaces SET {', '.join(sets)} WHERE id = ?", params
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_space(conn, space_id)


def set_visibility(
    conn: sqlite3.Connection, space_id: int, visibility: str, *, commit: bool = True
) -> dict | None:
    """Flip a space public ↔ private. Returns the updated space, or None if no space
    has that id (so the boundary can 404). The twin of aegis.projects.set_visibility:
    the boundary validates the value ('public' | 'private'), the column CHECK is the
    backstop, and this is the SOLE writer of spaces.visibility (core/access.py only
    reads it). Narrowing to 'private' is immediate — every page/space read already
    filters through access.can_see_space. ``commit=False`` composes this inside the
    audited visibility command's transaction so the flip, the creator's membership
    row (when going private), and the event land together."""
    cur = conn.execute(
        "UPDATE spaces SET visibility = ? WHERE id = ?", (visibility, space_id)
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_space(conn, space_id)


def delete_space(
    conn: sqlite3.Connection, space_id: int, *, commit: bool = True
) -> bool:
    """Delete a space. Returns True if a space was deleted, False if no space had
    that id (so the boundary can 404).

    PRECONDITION: the space holds no pages. The caller checks
    pages.count_pages_in_space first and returns a clean 409 — we do NOT cascade,
    because deleting a whole documentation tree is exactly the kind of irreversible
    bulk loss a wiki must never do silently (the same rule project- and page-delete
    follow). Unlike a page, a space has no version history or derived link/search
    rows of its own, so there is nothing to clean up beyond the row itself. (If a
    stray page somehow remained, the pages.space_id foreign key has no ON DELETE, so
    it would refuse the delete and raise rather than orphan it.) ``commit=False``
    composes this inside the audited delete command's transaction so the row delete and
    its ``space_deleted`` event land together."""
    cur = conn.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
    if commit:
        conn.commit()
    return cur.rowcount > 0
