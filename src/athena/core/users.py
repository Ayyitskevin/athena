"""Data access for users.

All user SQL lives here, mirroring aegis/issues.py: HTTP handlers call these
functions instead of writing queries, so storage changes touch only this file.

Users are the actors in Athena — people now, agents later. Issues (and, later,
docs) point at a user via a foreign key, so a user must exist before it can act.
"""
from __future__ import annotations

import sqlite3


def create_user(conn: sqlite3.Connection, *, email: str, name: str) -> dict:
    """Insert a user and return it. Raises sqlite3.IntegrityError if the email
    is already taken (the UNIQUE constraint refuses the duplicate)."""
    cur = conn.execute(
        "INSERT INTO users (email, name) VALUES (?, ?)",
        (email, name),
    )
    conn.commit()
    return get_user(conn, cur.lastrowid)


def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]
