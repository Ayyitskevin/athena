"""Admin read models for agent accounts.

Agents are still ordinary Athena users. This module only assembles the existing
facts an admin needs to supervise them: tokens, activity, access, and delegation.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, tokens, users

_RECENT_ACTIVITY_LIMIT = 6
_ASSIGNMENT_LIMIT = 20
_RUN_HEALTH_EVENT_LIMIT = 200
_RUN_HEALTH_VISIBLE_RUNS = 5


def agent_admin_summaries(conn: sqlite3.Connection) -> list[dict]:
    """Return one admin-facing summary for each user marked as an agent."""
    return [
        _agent_summary(conn, agent)
        for agent in users.list_users(conn)
        if agent["is_agent"]
    ]


def agent_run_health(conn: sqlite3.Connection) -> dict:
    """Return a fleet-level run-health read model for every agent account."""
    rows = [_agent_run_health(conn, agent) for agent in _agent_users(conn)]
    return {
        "agents": rows,
        "totals": {
            "agent_count": len(rows),
            "active_agent_count": sum(1 for row in rows if row["run_count"] > 0),
            "replay_ready_count": sum(
                1 for row in rows if row["tagged_run_count"] > 0
            ),
            "untagged_only_count": sum(
                1 for row in rows if row["health_state"] == "untagged_only"
            ),
            "partial_window_count": sum(
                1 for row in rows if row["partial_run_count"] > 0
            ),
            "total_recent_runs": sum(row["run_count"] for row in rows),
            "tagged_recent_runs": sum(row["tagged_run_count"] for row in rows),
        },
    }


def agent_run_exists(conn: sqlite3.Connection, run_id: str) -> bool:
    """Whether a tagged run has at least one event authored by an agent."""
    row = conn.execute(
        "SELECT 1 FROM activity a "
        "JOIN users u ON u.id = a.actor_id "
        "WHERE a.run_id = ? AND u.is_agent = 1 "
        "LIMIT 1",
        (run_id,),
    ).fetchone()
    return row is not None


def _agent_users(conn: sqlite3.Connection) -> list[dict]:
    return [user for user in users.list_users(conn) if user["is_agent"]]


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


def _agent_run_health(conn: sqlite3.Connection, agent: dict) -> dict:
    runs = activity.reconstruct_runs(
        conn, actor_id=agent["id"], limit=_RUN_HEALTH_EVENT_LIMIT
    )
    run_summaries = [_run_summary(conn, run) for run in runs]
    tagged_count = sum(1 for run in run_summaries if run["run_id"] is not None)
    heuristic_count = len(run_summaries) - tagged_count
    partial_count = sum(1 for run in run_summaries if run["partial"])
    state, label = _health_state(
        run_count=len(run_summaries),
        tagged_count=tagged_count,
        heuristic_count=heuristic_count,
        partial_count=partial_count,
    )
    return {
        "user": agent,
        "runs": run_summaries[:_RUN_HEALTH_VISIBLE_RUNS],
        "latest_run": run_summaries[0] if run_summaries else None,
        "run_count": len(run_summaries),
        "tagged_run_count": tagged_count,
        "heuristic_run_count": heuristic_count,
        "partial_run_count": partial_count,
        "recent_event_count": sum(run["event_count"] for run in run_summaries),
        "child_run_count": sum(run["child_run_count"] for run in run_summaries),
        "health_state": state,
        "health_label": label,
    }


def _run_summary(conn: sqlite3.Connection, run: dict) -> dict:
    run_id = run["run_id"]
    return {
        "run_id": run_id,
        "parent_run_id": run["parent_run_id"],
        "forked_from_event_id": run["forked_from_event_id"],
        "started_at": run["started_at"],
        "ended_at": run["ended_at"],
        "first_id": run["first_id"],
        "last_id": run["last_id"],
        "event_count": run["event_count"],
        "partial": run["partial"],
        "child_run_count": _child_run_count(conn, run_id) if run_id else 0,
    }


def _child_run_count(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM ("
        "SELECT DISTINCT run_id FROM activity "
        "WHERE parent_run_id = ? AND run_id IS NOT NULL AND run_id != ?"
        ")",
        (run_id, run_id),
    ).fetchone()
    return int(row["count"])


def _health_state(
    *,
    run_count: int,
    tagged_count: int,
    heuristic_count: int,
    partial_count: int,
) -> tuple[str, str]:
    if run_count == 0:
        return "quiet", "No activity"
    if partial_count > 0:
        return "partial_window", "Window clipped"
    if tagged_count == 0:
        return "untagged_only", "Untagged only"
    if heuristic_count > 0:
        return "mixed_runs", "Mixed runs"
    return "replay_ready", "Replay ready"


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
