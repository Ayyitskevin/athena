"""Bounded project facts under one visibility snapshot and observation clock.

Each lane counts in SQL and limits before materialization. Reports stay advisory;
no read acknowledges a handoff, settles a lease or advances a cursor.
"""

from datetime import UTC, datetime
import sqlite3
from typing import Any

from athena.aegis import lease_commands, leases, projects, statuses
from athena.core import access, db
from athena.core.ids import MAX_SQLITE_INTEGER

DEFAULT_LIMIT = 5
MAX_LIMIT = 20


def _excerpt(value: str, limit: int = 160) -> dict[str, Any]:
    return {
        "text": value[:limit],
        "total_chars": len(value),
        "clipped": len(value) > limit,
    }


def _category_sql(alias: str) -> str:
    """Exact category_of semantics, unlike the legacy neutral SQL fallback.

    Aliases are internal constants. Defaults apply only when a project has no
    configured rows; a missing name in a configured project stays NULL.
    """
    assert alias in {"i", "b"}
    defaults = " ".join(
        f"WHEN '{name}' THEN '{category}'"
        for name, category in statuses.DEFAULT_STATUSES
    )
    return (
        "CASE WHEN EXISTS (SELECT 1 FROM project_statuses configured "
        f"WHERE configured.project_id = {alias}.project_id) "
        "THEN (SELECT configured.category FROM project_statuses configured "
        f"WHERE configured.project_id = {alias}.project_id "
        f"AND configured.name = {alias}.status) "
        f"ELSE CASE {alias}.status {defaults} END END"
    )


def _issue_columns(alias: str, prefix: str = "") -> str:
    fields = {
        "id": f"{alias}.id",
        "project_id": f"{alias}.project_id",
        "project_seq": f"{alias}.project_seq",
        "project_key": f"(SELECT key FROM projects WHERE id = {alias}.project_id)",
        "title": f"{alias}.title",
        "status": f"{alias}.status",
        "status_category": _category_sql(alias),
        "priority": f"{alias}.priority",
        "assignee_id": f"{alias}.assignee_id",
        "assignee_name": f"(SELECT name FROM users WHERE id = {alias}.assignee_id)",
        "archived_at": f"{alias}.archived_at",
    }
    return ", ".join(f"{value} AS {prefix}{name}" for name, value in fields.items())


def _person(person_id: int, name: str) -> dict[str, Any]:
    return {"id": person_id, "name": _excerpt(name)}


def _issue(row: sqlite3.Row, *, prefix: str = "") -> dict[str, Any]:
    key, seq = row[f"{prefix}project_key"], row[f"{prefix}project_seq"]
    person_id = row[f"{prefix}assignee_id"]
    issue_id = row[f"{prefix}id"]
    return {
        "id": issue_id,
        "key": f"{key}-{seq}" if key and seq is not None else None,
        "title": _excerpt(row[f"{prefix}title"]),
        "status": _excerpt(row[f"{prefix}status"]),
        "status_category": row[f"{prefix}status_category"],
        "priority": row[f"{prefix}priority"],
        "assignee": None
        if person_id is None
        else _person(person_id, row[f"{prefix}assignee_name"]),
        "work_context_url": f"/issues/{issue_id}/work-context",
    }


def _rows(
    conn: sqlite3.Connection,
    *,
    columns: str,
    base: str,
    params: list[Any],
    order: str,
    limit: int,
) -> tuple[int, list[sqlite3.Row]]:
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    rows = conn.execute(
        f"SELECT {columns} {base} ORDER BY {order} LIMIT ?", [*params, limit]
    ).fetchall()
    return total, rows


def _group(total: int, items: list[dict], limit: int) -> dict[str, Any]:
    return {
        "items": items,
        "visible_total": total,
        "returned": len(items),
        "limit": limit,
        "clipped": total > len(items),
    }


def build_project_brief(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    actor: dict | None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a bounded current project read, or None for missing/hidden alike."""
    for name, value, maximum in (
        ("project_id", project_id, MAX_SQLITE_INTEGER),
        ("limit", limit, MAX_LIMIT),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    current = datetime.now(UTC) if now is None else now
    current = (
        current.replace(tzinfo=UTC)
        if current.tzinfo is None
        else current.astimezone(UTC)
    )
    with db.transaction(conn):
        project = projects.get_project(conn, project_id)
        if project is None or not access.can_see_project(conn, actor, project_id):
            return None
        return _build(conn, project=project, actor=actor, limit=limit, now=current)


def _build(
    conn: sqlite3.Connection,
    *,
    project: dict,
    actor: dict | None,
    limit: int,
    now: datetime,
) -> dict[str, Any]:
    project_id = project["id"]
    root = "i.project_id = ? AND i.archived_at IS NULL"
    is_open = f"({_category_sql('i')}) IS NOT 'done'"
    summary = conn.execute(
        "SELECT COUNT(*) AS open_issue_count, "
        "COUNT(CASE WHEN i.assignee_id IS NULL THEN 1 END) AS unassigned_open_issue_count "
        f"FROM issues i WHERE {root} AND {is_open}",
        [project_id],
    ).fetchone()

    total, rows = _rows(
        conn,
        columns=f"{_issue_columns('i')}, l.holder_id, u.name AS holder_name, l.expires_at",
        base=f"FROM issues i JOIN issue_leases l ON l.issue_id = i.id "
        f"JOIN users u ON u.id = l.holder_id WHERE {root} AND l.expires_at > ?",
        params=[project_id, leases.clock_stamp(now)],
        order="l.expires_at, i.id",
        limit=limit,
    )
    claims = _group(
        total,
        [
            {
                "issue": _issue(row),
                "holder": _person(row["holder_id"], row["holder_name"]),
                "expires_at": row["expires_at"],
            }
            for row in rows
        ],
        limit,
    )

    visible, visible_params = access.project_visibility_clause(actor, alias="bp")
    total, rows = _rows(
        conn,
        columns=f"{_issue_columns('i')}, {_issue_columns('b', 'blocker_')}",
        base=f"FROM issues i JOIN issue_links edge ON edge.to_id = i.id AND edge.kind = 'blocks' "
        f"JOIN issues b ON b.id = edge.from_id WHERE {root} AND {is_open} "
        f"AND ({_category_sql('b')}) IS NOT 'done' "
        "AND (b.project_id IS NULL OR EXISTS (SELECT 1 FROM projects bp "
        f"WHERE bp.id = b.project_id AND ({visible})))",
        params=[project_id, *visible_params],
        order="i.id, b.id",
        limit=limit,
    )
    edges = []
    for row in rows:
        blocker = _issue(row, prefix="blocker_")
        blocker["archived_at"] = row["blocker_archived_at"]
        if row["blocker_project_id"] != project_id:
            blocker["project_id"] = row["blocker_project_id"]
        edges.append({"issue": _issue(row), "blocked_by": blocker})
    blockers = _group(total, edges, limit)

    event_gate, event_params = access.event_visibility_clause(conn, actor, alias="a")
    event_gate = f" AND ({event_gate})" if event_gate else ""
    total, rows = _rows(
        conn,
        columns=f"{_issue_columns('i')}, h.reason, h.blocking_question, "
        "a.id AS event_id, a.created_at, a.actor_id, u.name AS actor_name",
        base="FROM issues i JOIN issue_claim_handoffs h ON h.issue_id = i.id "
        "JOIN activity a ON a.id = h.yield_event_id JOIN users u ON u.id = a.actor_id "
        f"WHERE {root} AND h.resume_event_id IS NULL{event_gate}",
        params=[project_id, *event_params],
        order="a.id, i.id",
        limit=limit,
    )
    handoffs = _group(
        total,
        [
            {
                "issue": _issue(row),
                "reason": row["reason"],
                "blocking_question": _excerpt(row["blocking_question"], 400),
                "yield_event_id": row["event_id"],
                "yielded_at": row["created_at"],
                "yielded_by": _person(row["actor_id"], row["actor_name"]),
            }
            for row in rows
        ],
        limit,
    )

    # Choose the latest visible native event BEFORE parsing or limiting. Invalid
    # newer reports cannot resurrect a previous PASS. No project-wide Python scan.
    latest = (
        "SELECT MAX(a.id) AS event_id FROM activity a JOIN issues i ON i.id = a.target_id "
        f"WHERE a.target_kind = 'issue' AND {root} AND a.verb = 'claim_completed' "
        f"AND a.imported_at IS NULL{event_gate} GROUP BY i.id"
    )
    total, rows = _rows(
        conn,
        columns=f"{_issue_columns('i')}, a.id AS event_id, a.created_at, a.actor_id, "
        "u.name AS actor_name, a.verb, a.imported_at, a.detail",
        base=f"FROM ({latest}) latest JOIN activity a ON a.id = latest.event_id "
        "JOIN issues i ON i.id = a.target_id JOIN users u ON u.id = a.actor_id",
        params=[project_id, *event_params],
        order="a.id DESC",
        limit=limit,
    )
    completions = []
    for row in rows:
        report = lease_commands.reported_completion(dict(row))
        completions.append(
            {
                "issue": _issue(row),
                "event_id": row["event_id"],
                "reported_at": row["created_at"],
                "reported_by": _person(row["actor_id"], row["actor_name"]),
                "report_available": report is not None,
                "reported_verdict": None if report is None else report.get("verdict"),
                "reported_findings": None if report is None else report.get("findings"),
            }
        )
    return {
        "schema": "athena.project_brief.v1",
        "scope": "visible_to_request_actor",
        "observed_at": now.isoformat(),
        "project": {
            "id": project_id,
            "key": project["key"],
            "name": _excerpt(project["name"]),
        },
        "summary": dict(summary),
        "active_claims": claims,
        "blockers": blockers,
        "open_handoffs": handoffs,
        "latest_completions": _group(total, completions, limit),
        "semantics": {
            "snapshot": "current_visible_snapshot",
            "group_counts": "independent scopes; not additive",
            "active_claims": "live possession, not process liveness",
            "blockers": "visible edges only; zero is not readiness",
            "open_handoffs": "awaiting resume, not unanswered comments",
            "latest_completions": "advisory historical actor reports, not verification",
            "authority": "read only; no state change or authority granted",
        },
    }
