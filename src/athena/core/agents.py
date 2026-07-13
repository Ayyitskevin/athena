"""Admin read models for agent accounts.

Agents are still ordinary Athena users. This module only assembles the existing
facts an admin needs to supervise them: tokens, activity, access, and delegation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
import shlex
import sqlite3

from athena.core import activity, tokens, users

_RECENT_ACTIVITY_LIMIT = 6
_ASSIGNMENT_LIMIT = 20
_RUN_HEALTH_EVENT_LIMIT = 200
_RUN_HEALTH_VISIBLE_RUNS = 5
_STALE_TOKEN_DAYS = 30
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_REPLAY_DB_PATH = "/var/lib/athena/athena.db"


def agent_admin_summaries(conn: sqlite3.Connection) -> list[dict]:
    """Return one admin-facing summary for each user marked as an agent."""
    return [
        _agent_summary(conn, agent)
        for agent in users.list_users(conn)
        if agent["is_agent"]
    ]


def agent_run_health(conn: sqlite3.Connection, *, agent_id: int | None = None) -> dict:
    """Return a fleet-level run-health read model for every agent account."""
    agent_options = _agent_users(conn)
    selected_agent = None
    if agent_id is None:
        selected_agents = agent_options
    else:
        selected_agents = [
            agent for agent in agent_options if int(agent["id"]) == int(agent_id)
        ]
        selected_agent = selected_agents[0] if selected_agents else None

    rows = [_agent_run_health(conn, agent) for agent in selected_agents]
    return {
        "agents": rows,
        "agent_options": agent_options,
        "selected_agent_id": agent_id,
        "selected_agent": selected_agent,
        "totals": {
            "agent_count": len(rows),
            # Activity rows prove that an agent acted in this bounded window. They do
            # not prove that its external process is still running, so keep the field
            # (and every transport/UI label) honest about what the log can establish.
            "agents_with_activity_count": sum(
                1 for row in rows if row["run_count"] > 0
            ),
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
    live_tokens = [token for token in token_rows if token["revoked_at"] is None]
    revoked_tokens = [token for token in token_rows if token["revoked_at"] is not None]
    recent_activity = activity.list_activity(
        conn, actor_id=agent["id"], limit=_RECENT_ACTIVITY_LIMIT
    )
    return {
        "user": agent,
        "tokens": token_rows,
        "live_token_count": len(live_tokens),
        "revoked_token_count": len(revoked_tokens),
        "token_warnings": _token_posture_warnings(live_tokens),
        "recent_activity": recent_activity,
        "last_activity_at": recent_activity[0]["created_at"]
        if recent_activity
        else None,
        "project_memberships": _project_memberships(conn, agent["id"]),
        "space_memberships": _space_memberships(conn, agent["id"]),
        "assignments": _assignments(conn, agent["id"], limit=_ASSIGNMENT_LIMIT),
    }


def _token_posture_warnings(live_tokens: list[dict]) -> list[dict]:
    warnings: list[dict] = []
    if not live_tokens:
        warnings.append(
            _token_warning(
                "no_live_token",
                "No live token",
                "critical",
                "This agent has no active API token.",
                [],
            )
        )
        return warnings

    admin_tokens = [
        token for token in live_tokens if tokens.ADMIN_SCOPE in token["scopes"]
    ]
    if admin_tokens:
        warnings.append(
            _token_warning(
                "admin_scope",
                "Admin scoped",
                "danger",
                "Live admin-scoped token: "
                + _token_names(admin_tokens),
                admin_tokens,
            )
        )

    never_used = [token for token in live_tokens if not token["last_used_at"]]
    if never_used:
        warnings.append(
            _token_warning(
                "never_used",
                "Never used",
                "warning",
                "Live token never used: " + _token_names(never_used),
                never_used,
            )
        )

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=_STALE_TOKEN_DAYS)
    stale_tokens = [
        token
        for token in live_tokens
        if (last_used := _token_timestamp(token["last_used_at"])) is not None
        and last_used < cutoff
    ]
    if stale_tokens:
        warnings.append(
            _token_warning(
                "stale_token",
                "Stale token",
                "warning",
                f"Live token unused for {_STALE_TOKEN_DAYS}+ days: "
                + _token_names(stale_tokens),
                stale_tokens,
            )
        )

    return warnings


def _token_warning(
    kind: str, label: str, severity: str, detail: str, token_rows: list[dict]
) -> dict:
    return {
        "kind": kind,
        "label": label,
        "severity": severity,
        "detail": detail,
        "token_names": [token["name"] for token in token_rows],
    }


def _token_names(token_rows: list[dict]) -> str:
    return ", ".join(token["name"] for token in token_rows)


def _token_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _TS_FORMAT)
    except (TypeError, ValueError):
        return None


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
        "replay_export_command": _replay_export_command(run_id) if run_id else None,
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


def _replay_export_command(run_id: str) -> str:
    artifact_path = f"/exports/{_artifact_filename(run_id)}"
    return " ".join(
        [
            "athena-export-run",
            shlex.quote(_REPLAY_DB_PATH),
            shlex.quote(run_id),
            shlex.quote(artifact_path),
        ]
    )


def _artifact_filename(run_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id).strip(".-")
    return f"{stem or 'agent-run'}.replay.json"


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
