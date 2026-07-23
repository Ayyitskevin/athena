"""Bounded, visibility-safe issue context for an operator or delegated agent.

The projection intentionally describes only the request actor's current visible
snapshot.  It is useful context for deciding what to do, but it is not a claim,
lease, readiness check, liveness signal, or replay contract.
"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
from typing import Any

from athena.aegis import claim_handoffs, issue_etags, issues, statuses
from athena.core import access, activity, db, etag


SCHEMA = "athena.issue_work_context.v1"
SCOPE = "visible_to_request_actor"

GENERAL_LIMIT = 20
REFERENCE_LIMIT = 10
ACTIVITY_LIMIT = 30
ISSUE_DESCRIPTION_LIMIT = 50_000
PAGE_BODY_LIMIT = 8_000
COMMENT_BODY_LIMIT = 4_000
ACTIVITY_DETAIL_LIMIT = 1_000

_RELATED_COLUMNS = (
    "i.id, i.title, i.status, i.priority, i.project_id, i.project_seq, "
    "i.archived_at, p.name AS project_name, p.key AS project_key"
)
_RELATED_FROM = "issues i LEFT JOIN projects p ON p.id = i.project_id"


def context_etag(payload: dict[str, Any]) -> str:
    """Return the strong validator for exactly one work-context payload."""
    return etag.strong_etag("issue-work-context-v1", payload)


def _excerpt(value: str | None, limit: int) -> dict[str, Any]:
    text = value or ""
    total_chars = len(text)
    return {
        "text": text[:limit],
        "total_chars": total_chars,
        "truncated": total_chars > limit,
    }


def _visibility_clause(
    alias: str, visible_ids: set[int] | None, container_column: str
) -> tuple[str, list[int]]:
    if visible_ids is None:
        return "1 = 1", []
    if not visible_ids:
        if container_column == "project_id":
            return f"{alias}.{container_column} IS NULL", []
        return "0 = 1", []
    placeholders = ",".join("?" for _ in visible_ids)
    values = sorted(visible_ids)
    if container_column == "project_id":
        return (
            f"({alias}.{container_column} IS NULL OR "
            f"{alias}.{container_column} IN ({placeholders}))",
            values,
        )
    return f"{alias}.{container_column} IN ({placeholders})", values


def _bounded_group(
    conn: sqlite3.Connection,
    *,
    select_sql: str,
    params: list[Any],
    order_by: str,
    limit: int,
    mapper: Callable[[sqlite3.Row], dict[str, Any]],
) -> dict[str, Any]:
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM ({select_sql}) AS visible_rows", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"{select_sql} ORDER BY {order_by} LIMIT ?", [*params, limit]
    ).fetchall()
    return {
        "items": [mapper(row) for row in rows],
        "visible_total": total,
        "clipped": total > len(rows),
    }


def _issue_key(row: sqlite3.Row | dict[str, Any]) -> str | None:
    key = row["project_key"]
    seq = row["project_seq"]
    return f"{key}-{seq}" if key and seq is not None else None


def _related_issue(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "key": _issue_key(row),
        "title": row["title"],
        "status": row["status"],
        "status_category": None,
        "priority": row["priority"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "archived_at": row["archived_at"],
    }


def _with_status_category(
    conn: sqlite3.Connection, item: dict[str, Any]
) -> dict[str, Any]:
    item["status_category"] = statuses.category_of(
        conn, item["project_id"], item["status"]
    )
    return item


def _related_from_issue(conn: sqlite3.Connection, issue: dict) -> dict[str, Any]:
    item = {
        "id": issue["id"],
        "key": issue["key"],
        "title": issue["title"],
        "status": issue["status"],
        "status_category": None,
        "priority": issue["priority"],
        "project_id": issue["project_id"],
        "project_name": issue["project_name"],
        "archived_at": issue["archived_at"],
    }
    return _with_status_category(conn, item)


def _related_group(
    conn: sqlite3.Connection,
    *,
    from_sql: str,
    where_sql: str,
    params: list[Any],
    visible_project_ids: set[int] | None,
    limit: int = GENERAL_LIMIT,
) -> dict[str, Any]:
    gate, gate_params = _visibility_clause(
        "i", visible_project_ids, "project_id"
    )
    select_sql = (
        f"SELECT {_RELATED_COLUMNS} FROM {from_sql} "
        f"WHERE ({where_sql}) AND ({gate})"
    )
    return _bounded_group(
        conn,
        select_sql=select_sql,
        params=[*params, *gate_params],
        order_by="i.id",
        limit=limit,
        mapper=lambda row: _with_status_category(conn, _related_issue(row)),
    )


def _hierarchy(
    conn: sqlite3.Connection,
    issue: dict,
    actor: dict | None,
    visible_project_ids: set[int] | None,
) -> dict[str, Any]:
    parent = None
    if issue.get("parent_id") and access.can_see_issue(
        conn, actor, issue["parent_id"]
    ):
        parent_issue = issues.get_issue(conn, issue["parent_id"])
        if parent_issue is not None:
            parent = _related_from_issue(conn, parent_issue)
    children = _related_group(
        conn,
        from_sql=_RELATED_FROM,
        where_sql="i.parent_id = ?",
        params=[issue["id"]],
        visible_project_ids=visible_project_ids,
    )
    return {"parent": parent, "children": children}


def _dependencies(
    conn: sqlite3.Connection,
    issue_id: int,
    visible_project_ids: set[int] | None,
) -> dict[str, Any]:
    blocks = _related_group(
        conn,
        from_sql=(
            "issue_links l JOIN issues i ON i.id = l.to_id "
            "LEFT JOIN projects p ON p.id = i.project_id"
        ),
        where_sql="l.from_id = ? AND l.kind = 'blocks'",
        params=[issue_id],
        visible_project_ids=visible_project_ids,
    )
    blocked_by = _related_group(
        conn,
        from_sql=(
            "issue_links l JOIN issues i ON i.id = l.from_id "
            "LEFT JOIN projects p ON p.id = i.project_id"
        ),
        where_sql="l.to_id = ? AND l.kind = 'blocks'",
        params=[issue_id],
        visible_project_ids=visible_project_ids,
    )
    relates = _related_group(
        conn,
        from_sql=(
            "issue_links l JOIN issues i ON i.id = "
            "CASE WHEN l.from_id = ? THEN l.to_id ELSE l.from_id END "
            "LEFT JOIN projects p ON p.id = i.project_id"
        ),
        where_sql="l.kind = 'relates' AND (l.from_id = ? OR l.to_id = ?)",
        params=[issue_id, issue_id, issue_id],
        visible_project_ids=visible_project_ids,
    )
    # Match statuses.category_of: backlog (and the defensive no-status-row project
    # fallback) uses the built-in lifecycle; a known custom status uses its category;
    # an unknown status is conservatively treated as open, never as resolved.
    not_done = (
        "COALESCE((SELECT ps.category FROM project_statuses ps "
        "WHERE ps.project_id = i.project_id AND ps.name = i.status LIMIT 1), "
        "CASE WHEN i.project_id IS NULL OR NOT EXISTS ("
        "SELECT 1 FROM project_statuses any_ps "
        "WHERE any_ps.project_id = i.project_id) THEN CASE i.status "
        "WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing' "
        "WHEN 'done' THEN 'done' END END) IS NOT 'done'"
    )
    open_blockers = _related_group(
        conn,
        from_sql=(
            "issue_links l JOIN issues i ON i.id = l.from_id "
            "LEFT JOIN projects p ON p.id = i.project_id"
        ),
        where_sql=f"l.to_id = ? AND l.kind = 'blocks' AND ({not_done})",
        params=[issue_id],
        visible_project_ids=visible_project_ids,
    )
    return {
        "open_blockers": open_blockers,
        "blocked_by": blocked_by,
        "blocks": blocks,
        "relates": relates,
    }


def _contributors(conn: sqlite3.Connection, issue_id: int) -> dict[str, Any]:
    select_sql = (
        "SELECT ic.user_id, u.name, u.is_agent, ic.added_by, ic.added_at "
        "FROM issue_contributors ic JOIN users u ON u.id = ic.user_id "
        "WHERE ic.issue_id = ?"
    )
    return _bounded_group(
        conn,
        select_sql=select_sql,
        params=[issue_id],
        order_by="u.name COLLATE NOCASE, ic.user_id",
        limit=GENERAL_LIMIT,
        mapper=lambda row: {
            "user_id": row["user_id"],
            "name": row["name"],
            "is_agent": bool(row["is_agent"]),
            "added_by": row["added_by"],
            "added_at": row["added_at"],
        },
    )


def _attachments(conn: sqlite3.Connection, issue_id: int) -> dict[str, Any]:
    select_sql = (
        "SELECT id, filename, content_type, byte_size, sha256, uploaded_by, "
        "created_at FROM attachments "
        "WHERE target_kind = 'issue' AND target_id = ?"
    )
    return _bounded_group(
        conn,
        select_sql=select_sql,
        params=[issue_id],
        order_by="id",
        limit=GENERAL_LIMIT,
        mapper=lambda row: dict(row),
    )


def _comments(conn: sqlite3.Connection, issue_id: int) -> dict[str, Any]:
    select_sql = (
        "SELECT c.id, c.author_id, u.name AS author_name, c.body, c.created_at "
        "FROM comments c JOIN users u ON u.id = c.author_id "
        "WHERE c.issue_id = ?"
    )
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM ({select_sql}) AS visible_rows", [issue_id]
    ).fetchone()["n"]
    # Keep the newest bounded window, then present that window chronologically.
    rows = conn.execute(
        f"SELECT * FROM ({select_sql} ORDER BY c.id DESC LIMIT ?) recent "
        "ORDER BY id",
        (issue_id, GENERAL_LIMIT),
    ).fetchall()
    return {
        "items": [
            {
                "id": row["id"],
                "author_id": row["author_id"],
                "author_name": row["author_name"],
                "body": _excerpt(row["body"], COMMENT_BODY_LIMIT),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        "visible_total": total,
        "clipped": total > len(rows),
    }


def _outgoing_references(
    conn: sqlite3.Connection,
    issue_id: int,
    visible_project_ids: set[int] | None,
    visible_space_ids: set[int] | None,
) -> dict[str, Any]:
    issue_gate, issue_gate_params = _visibility_clause(
        "i", visible_project_ids, "project_id"
    )
    page_gate, page_gate_params = _visibility_clause(
        "pg", visible_space_ids, "space_id"
    )
    select_sql = (
        "SELECT 'issue' AS kind, i.id AS id, i.title, p.key AS project_key, "
        "i.project_seq, i.status, i.priority, i.project_id, "
        "p.name AS project_name, i.archived_at, NULL AS space_id, "
        "NULL AS space_key, NULL AS space_name, NULL AS page_body "
        "FROM links l JOIN issues i ON i.id = l.target_id "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "WHERE l.source_kind = 'issue' AND l.source_id = ? "
        "AND l.target_kind = 'issue' AND (" + issue_gate + ") "
        "UNION ALL "
        "SELECT 'page' AS kind, pg.id AS id, pg.title, NULL AS project_key, "
        "NULL AS project_seq, NULL AS status, NULL AS priority, "
        "NULL AS project_id, NULL AS project_name, NULL AS archived_at, "
        "pg.space_id, s.key AS space_key, s.name AS space_name, "
        "pg.body AS page_body "
        "FROM links l JOIN pages pg ON pg.id = l.target_id "
        "JOIN spaces s ON s.id = pg.space_id "
        "WHERE l.source_kind = 'issue' AND l.source_id = ? "
        "AND l.target_kind = 'page' AND (" + page_gate + ")"
    )

    def map_reference(row: sqlite3.Row) -> dict[str, Any]:
        status_category = None
        if row["kind"] == "issue":
            status_category = statuses.category_of(
                conn, row["project_id"], row["status"]
            )
        return {
            "kind": row["kind"],
            "id": row["id"],
            "title": row["title"],
            "issue_key": _issue_key(row) if row["kind"] == "issue" else None,
            "status": row["status"],
            "status_category": status_category,
            "priority": row["priority"],
            "space_id": row["space_id"],
            "space_key": row["space_key"],
            "space_name": row["space_name"],
            "body": (
                _excerpt(row["page_body"], PAGE_BODY_LIMIT)
                if row["kind"] == "page"
                else None
            ),
        }

    return _bounded_group(
        conn,
        select_sql=select_sql,
        params=[
            issue_id,
            *issue_gate_params,
            issue_id,
            *page_gate_params,
        ],
        order_by="kind, id",
        limit=REFERENCE_LIMIT,
        mapper=map_reference,
    )


def _backlinks(
    conn: sqlite3.Connection,
    issue_id: int,
    visible_project_ids: set[int] | None,
    visible_space_ids: set[int] | None,
) -> dict[str, Any]:
    issue_gate, issue_gate_params = _visibility_clause(
        "i", visible_project_ids, "project_id"
    )
    page_gate, page_gate_params = _visibility_clause(
        "pg", visible_space_ids, "space_id"
    )
    select_sql = (
        "SELECT 'issue' AS kind, i.id AS id, i.title, p.key AS project_key, "
        "i.project_seq, NULL AS space_key "
        "FROM links l JOIN issues i ON i.id = l.source_id "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "WHERE l.target_kind = 'issue' AND l.target_id = ? "
        "AND l.source_kind = 'issue' AND (" + issue_gate + ") "
        "UNION ALL "
        "SELECT 'page' AS kind, pg.id AS id, pg.title, NULL AS project_key, "
        "NULL AS project_seq, s.key AS space_key "
        "FROM links l JOIN pages pg ON pg.id = l.source_id "
        "JOIN spaces s ON s.id = pg.space_id "
        "WHERE l.target_kind = 'issue' AND l.target_id = ? "
        "AND l.source_kind = 'page' AND (" + page_gate + ")"
    )
    return _bounded_group(
        conn,
        select_sql=select_sql,
        params=[
            issue_id,
            *issue_gate_params,
            issue_id,
            *page_gate_params,
        ],
        order_by="kind, id",
        limit=REFERENCE_LIMIT,
        mapper=lambda row: {
            "kind": row["kind"],
            "id": row["id"],
            "title": row["title"],
            "issue_key": _issue_key(row) if row["kind"] == "issue" else None,
            "space_key": row["space_key"],
        },
    )


def _recent_activity(
    conn: sqlite3.Connection, issue_id: int, actor: dict | None
) -> dict[str, Any]:
    gate, gate_params = access.event_visibility_clause(conn, actor, alias="a")
    clauses = ["a.target_kind = 'issue'", "a.target_id = ?"]
    params: list[Any] = [issue_id]
    if gate:
        clauses.insert(0, gate)
        params = [*gate_params, *params]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM activity a WHERE " + " AND ".join(clauses),
        params,
    ).fetchone()["n"]
    rows = activity.list_activity(
        conn,
        target_kind="issue",
        target_id=issue_id,
        limit=ACTIVITY_LIMIT,
        actor=actor,
    )
    return {
        "items": [
            {
                "id": row["id"],
                "actor_id": row["actor_id"],
                "actor_name": row["actor_name"],
                "verb": row["verb"],
                "target_kind": row["target_kind"],
                "target_id": row["target_id"],
                "detail": _excerpt(row["detail"], ACTIVITY_DETAIL_LIMIT),
                "created_at": row["created_at"],
                "run_id": row["run_id"],
                "parent_run_id": row["parent_run_id"],
                "forked_from_event_id": row["forked_from_event_id"],
                "imported_at": row["imported_at"],
            }
            for row in rows
        ],
        "visible_total": total,
        "clipped": total > len(rows),
    }


def _has_clipping(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("clipped") is True or value.get("truncated") is True:
            return True
        return any(_has_clipping(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_clipping(child) for child in value)
    return False


def build_work_context(
    conn: sqlite3.Connection, ref: str, *, actor: dict | None
) -> dict[str, Any] | None:
    """Build one deterministic snapshot, or None for missing and hidden roots alike."""
    with db.transaction(conn):
        issue = issues.get_by_ref(conn, ref)
        if issue is None or not access.can_see_project_or_backlog(
            conn, actor, issue["project_id"]
        ):
            return None

        visible_project_ids = access.visible_project_filter(conn, actor)
        visible_space_ids = access.visible_space_filter(conn, actor)
        public_issue, issue_etag = issue_etags.resource_and_etag(conn, issue)
        status_category = statuses.category_of(
            conn, issue["project_id"], issue["status"]
        )

        dependencies = _dependencies(
            conn, issue["id"], visible_project_ids
        )
        handoff_history = claim_handoffs.list_handoffs(conn, issue["id"])
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "scope": SCOPE,
            "semantics": {
                "snapshot": "current_visible_state",
                "does_not_assert": [
                    "claimed",
                    "ready",
                    "unblocked",
                    "running",
                    "live",
                    "replay_safe",
                ],
            },
            "issue": {
                "id": public_issue["id"],
                "key": public_issue["key"],
                "title": public_issue["title"],
                "description": _excerpt(
                    public_issue["body"], ISSUE_DESCRIPTION_LIMIT
                ),
                "status": public_issue["status"],
                "status_category": status_category,
                "priority": public_issue["priority"],
                "project_id": public_issue["project_id"],
                "project_name": public_issue["project_name"],
                "assignee_id": public_issue["assignee_id"],
                "assignee_name": public_issue["assignee_name"],
                "created_by": public_issue["created_by"],
                "created_at": public_issue["created_at"],
                "archived_at": public_issue["archived_at"],
                "labels": public_issue["labels"],
            },
            "issue_etag": issue_etag,
            "warnings": [],
            "hierarchy": _hierarchy(
                conn, issue, actor, visible_project_ids
            ),
            "dependencies": dependencies,
            "contributors": _contributors(conn, issue["id"]),
            "attachments": _attachments(conn, issue["id"]),
            "comments": _comments(conn, issue["id"]),
            "references": {
                "outgoing": _outgoing_references(
                    conn,
                    issue["id"],
                    visible_project_ids,
                    visible_space_ids,
                ),
                "backlinks": _backlinks(
                    conn,
                    issue["id"],
                    visible_project_ids,
                    visible_space_ids,
                ),
            },
            "recent_activity": _recent_activity(conn, issue["id"], actor),
            "claim_handoffs": handoff_history,
        }

        warnings: list[str] = []
        if issue["archived_at"] is not None:
            warnings.append("archived")
        if not issue["body"].strip():
            warnings.append("empty_description")
        if issue["assignee_id"] is None:
            warnings.append("no_accountable_assignee")
        if dependencies["open_blockers"]["visible_total"]:
            warnings.append("visible_open_blockers")
        if handoff_history["open"] is not None:
            warnings.append("open_claim_handoff")
        if status_category is None:
            warnings.append("unknown_status_category")
        if _has_clipping(payload):
            warnings.append("context_clipped")
        payload["warnings"] = warnings
        return payload
