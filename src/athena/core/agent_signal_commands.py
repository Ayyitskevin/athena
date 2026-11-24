"""The writes behind ``agent_signals``: an agent's newest refusal and the
newest time a pickup told it nothing was ready (F15).

Each replaces one row per agent account in place. Neither is an activity
event: nothing happened to anything, and a looping agent must not be able to
grow the trail by being refused. Only agent accounts are kept; a person's
refusal is in front of the person.
"""

from __future__ import annotations

import sqlite3

from athena.core import packet_time
from athena.core.agent_signals import MAX_REQUEST_CHARS


def _is_agent(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT is_agent FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and bool(row["is_agent"])


def _now() -> str:
    return packet_time.rfc3339(packet_time.clock())


def record_refusal(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    code: str,
    status_code: int,
    method: str,
    path: str,
) -> bool:
    """Keep ``code`` as the newest refusal this agent account received.

    Runs on a connection of its own, after the refused request's work has
    rolled back, and commits. Returns whether a row was written: an account
    that is not an agent, or a status outside 400 to 499, writes nothing."""
    if not 400 <= status_code <= 499 or not code or not _is_agent(conn, user_id):
        return False
    request = f"{method} {path}"[:MAX_REQUEST_CHARS]
    conn.execute(
        "INSERT INTO agent_last_signals (user_id, last_refusal_code, "
        "last_refusal_status, last_refusal_request, last_refusal_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
        "last_refusal_code = excluded.last_refusal_code, "
        "last_refusal_status = excluded.last_refusal_status, "
        "last_refusal_request = excluded.last_refusal_request, "
        "last_refusal_at = excluded.last_refusal_at",
        (user_id, code[:100], status_code, request, _now()),
    )
    conn.commit()
    return True


def record_nothing_ready(
    conn: sqlite3.Connection, *, user_id: int, commit: bool
) -> bool:
    """Keep now as the newest time a pickup told this agent account that
    nothing was ready. ``commit=False`` folds it into the pickup's own
    transaction."""
    if not _is_agent(conn, user_id):
        return False
    conn.execute(
        "INSERT INTO agent_last_signals (user_id, last_nothing_ready_at) "
        "VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET "
        "last_nothing_ready_at = excluded.last_nothing_ready_at",
        (user_id, _now()),
    )
    if commit:
        conn.commit()
    return True
