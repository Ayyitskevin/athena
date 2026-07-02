"""Admin read models for agent accounts.

Agents are still ordinary Athena users. This module only assembles the existing
facts an admin needs to supervise them: tokens, activity, access, and delegation.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, tokens, users

_RECENT_ACTIVITY_LIMIT = 6
_ASSIGNMENT_LIMIT = 20


def agent_admin_summaries(conn: sqlite3.Connection) -> list[dict]:
    """Return one admin-facing summary for each user marked as an agent."""
    return [
        _agent_summary(conn, agent)
        for agent in users.list_users(conn)
        if agent["is_agent"]
    ]


def _agent_summary(conn: sqlite3.Connection, agent: dict) -> dict:
    token_rows = tokens.list_tokens(conn, agent["id"])
    recent_activity = activity.list_activity(
        conn, actor_id=agent["id"], limit=_RECENT_ACTIVITY_LIMIT
    )
    return {
        "user": agent,
        "tokens": token_rows,
        "live_token_count": sum(1 for token in token_rows if token["revoked_at"] is None),
        "revoked_token_count": sum(
            1 for token in token_rows if token["revoked_at"] is not None
        ),
        "recent_activity": recent_activity,
        "last_activity_at": recent_activity[0]["created_at"]
        if recent_activity
        else None,
        "project_memberships": _project_memberships(conn, agent["id"]),
        "space_memberships": _space_memberships(conn, agent["id"]),
        "assignments": _assignments(conn, agent["id"], limit=_ASSIGNMENT_LIMIT),
    }


def _project_memberships(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, p.name, p.key, p.visibility, pm.added_at, "
        "u.name AS added_by_name "
        "FROM project_members pm "
        "JOIN projects p ON p.id = pm.project_id "
        "LEFT JOIN users u ON u.id = pm.added_by "
        "WHERE pm.user_id = ? "
        "ORDER BY p.name COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _space_memberships(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.name, s.key, s.visibility, sm.added_at, "
        "u.name AS added_by_name "
        "FROM space_members sm "
        "JOIN spaces s ON s.id = sm.space_id "
        "LEFT JOIN users u ON u.id = sm.added_by "
        "WHERE sm.user_id = ? "
        "ORDER BY s.name COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _assignments(
    conn: sqlite3.Connection, user_id: int, *, limit: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT i.id, i.title, i.status, i.priority, i.project_id, i.project_seq, "
        "p.name AS project_name, p.key AS project_key, ic.added_at, "
        "u.name AS added_by_name "
        "FROM issue_contributors ic "
        "JOIN issues i ON i.id = ic.issue_id "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN users u ON u.id = ic.added_by "
        "WHERE ic.user_id = ? AND i.archived_at IS NULL "
        "ORDER BY ic.added_at DESC, i.id DESC "
        "LIMIT ?",
        (user_id, limit),
    ).fetchall()
    assignments: list[dict] = []
    for row in rows:
        item = dict(row)
        if item.get("project_key") and item.get("project_seq") is not None:
            item["key"] = f"{item['project_key']}-{item['project_seq']}"
        else:
            item["key"] = None
        assignments.append(item)
    return assignments
