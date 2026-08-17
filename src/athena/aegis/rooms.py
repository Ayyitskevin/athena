"""Project rooms — flavor areas on a floor, not a second tracker."""

from __future__ import annotations

import re
import sqlite3

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

STARTER_ROOMS: tuple[tuple[str, str, str], ...] = (
    ("warehouse", "The Warehouse", "Ship it. Don't sit on the forklift."),
    ("accounting", "Accounting", "The numbers are the numbers."),
    ("sales", "Sales", "Bears. Beets. Still a real chair."),
    ("annex", "The Annex", "Overflow. Not exile."),
)


def slugify(name: str) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug or not _SLUG_RE.match(slug) or len(slug) > 40:
        return None
    return slug


def _row(row: sqlite3.Row) -> dict:
    return dict(row)


def list_rooms(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM project_rooms WHERE project_id = ? ORDER BY name COLLATE NOCASE",
        (project_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def get_room(conn: sqlite3.Connection, room_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM project_rooms WHERE id = ?", (room_id,)
    ).fetchone()
    return _row(row) if row else None


def get_room_by_slug(
    conn: sqlite3.Connection, project_id: int, slug: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM project_rooms WHERE project_id = ? AND slug = ?",
        (project_id, slug),
    ).fetchone()
    return _row(row) if row else None


def create_room(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    name: str,
    slug: str,
    blurb: str = "",
    commit: bool = True,
) -> dict:
    conn.execute(
        "INSERT INTO project_rooms (project_id, slug, name, blurb) VALUES (?, ?, ?, ?)",
        (project_id, slug, name, blurb),
    )
    room = get_room_by_slug(conn, project_id, slug)
    assert room is not None
    if commit:
        conn.commit()
    return room


def set_issue_room(
    conn: sqlite3.Connection,
    issue_id: int,
    room_id: int | None,
    *,
    commit: bool = True,
) -> None:
    conn.execute("UPDATE issues SET room_id = ? WHERE id = ?", (room_id, issue_id))
    if commit:
        conn.commit()
