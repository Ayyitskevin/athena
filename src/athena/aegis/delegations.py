"""Read-only pickup projection for work delegated through issue contributors.

Delegation already has one durable owner: ``issue_contributors``.  This module
does not create a second queue or lifecycle.  It assembles the facts an actor needs
to discover their own contributor assignments: the issue instructions, accountable
assignee, delegation attribution, and blockers that actor is allowed to see.

The projection deliberately avoids "ready", "running", or "unblocked" claims.  A
missing visible blocker does not prove that no hidden blocker exists, and a row in
this inbox does not claim work or say anything about an external process.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import claim_handoffs
from athena.core import access, db, labels


DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_OFFSET = 2**63 - 1
_BLOCKER_PREVIEW_LIMIT = 10


# Project statuses own their category.  Backlog issues have no project-status row,
# so fall back to the canonical built-in meanings.  The same fallback protects a
# damaged/legacy project missing its seeded rows without hard-coding "done" as the
# only possible closed status.
def _status_category_sql(issue_alias: str, status_alias: str) -> str:
    return (
        f"COALESCE({status_alias}.category, CASE {issue_alias}.status "
        "WHEN 'open' THEN 'todo' "
        "WHEN 'done' THEN 'done' "
        "ELSE 'doing' END)"
    )


_STATUS_CATEGORY_SQL = _status_category_sql("i", "ps")

_PRIORITY_ORDER_SQL = (
    "CASE i.priority "
    "WHEN 'urgent' THEN 0 "
    "WHEN 'high' THEN 1 "
    "WHEN 'medium' THEN 2 "
    "WHEN 'low' THEN 3 "
    "ELSE 4 END"
)


def list_delegations(
    conn: sqlite3.Connection,
    subject: dict,
    *,
    viewer: dict | None = None,
    include_closed: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """Return one subject's bounded contributor assignments.

    Results are urgent-first, then oldest-delegated-first so an old task cannot be
    hidden indefinitely by newer work of the same priority.  limit + 1 rows are
    fetched to derive ``has_more`` without an unbounded count.  Archived issues are
    never pickup work; closed-category issues appear only when explicitly requested.

    By default the subject is also the viewer, which is the self-service REST/MCP
    contract.  An authenticated admin page may pass itself as viewer to supervise
    all rows; each item then says whether the subject could see it.  Blocker previews
    always use the subject's visibility, so supervision shows the context the agent
    actually receives rather than the admin's wider view.
    """
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0 or offset > MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")

    viewer = subject if viewer is None else viewer
    # All access resolution, body reads, and enrichment share one WAL snapshot. Without
    # it, a concurrent public->private flip or membership revoke could land between the
    # visibility query and serialization and expose content from stale authorization.
    with db.transaction(conn):
        return _list_delegations(
            conn,
            subject,
            viewer=viewer,
            include_closed=include_closed,
            limit=limit,
            offset=offset,
        )


def _list_delegations(
    conn: sqlite3.Connection,
    subject: dict,
    *,
    viewer: dict,
    include_closed: bool,
    limit: int,
    offset: int,
) -> dict:
    clauses = ["ic.user_id = ?", "i.archived_at IS NULL"]
    params: list[object] = [subject["id"]]
    if not include_closed:
        clauses.append(f"{_STATUS_CATEGORY_SQL} != 'done'")

    subject_visible_project_ids = access.visible_project_filter(conn, subject)
    viewer_visible_project_ids = (
        subject_visible_project_ids
        if viewer["id"] == subject["id"]
        else access.visible_project_filter(conn, viewer)
    )
    visibility_sql, visibility_params = _visibility_sql(
        "i.project_id", viewer_visible_project_ids
    )
    if visibility_sql is not None:
        clauses.append(visibility_sql)
        params.extend(visibility_params)

    query = (
        "SELECT i.id, i.title, i.body, i.status, i.priority, i.project_id, "
        "i.project_seq, i.assignee_id, assignee.name AS assignee_name, "
        "p.name AS project_name, p.key AS project_key, "
        "ic.added_at AS delegated_at, ic.added_by AS delegated_by_id, "
        "delegator.name AS delegated_by_name, "
        f"{_STATUS_CATEGORY_SQL} AS status_category "
        "FROM issue_contributors ic "
        "JOIN issues i ON i.id = ic.issue_id "
        "LEFT JOIN users assignee ON assignee.id = i.assignee_id "
        "LEFT JOIN users delegator ON delegator.id = ic.added_by "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN project_statuses ps "
        "ON ps.project_id = i.project_id AND ps.name = i.status "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY {_PRIORITY_ORDER_SQL}, ic.added_at, i.id "
        "LIMIT ? OFFSET ?"
    )
    rows = conn.execute(query, [*params, limit + 1, offset]).fetchall()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    issue_ids = [int(row["id"]) for row in page_rows]
    labels_by_issue = labels.labels_for_issues(conn, issue_ids)
    blockers_by_issue = _blocker_previews(conn, issue_ids, subject_visible_project_ids)
    handoffs_by_issue = claim_handoffs.open_handoffs_for_issues(conn, issue_ids)
    items = []
    self_view = viewer["id"] == subject["id"]
    for row in page_rows:
        issue_id = int(row["id"])
        item = _to_item(
            row,
            subject_visible_project_ids,
            labels_by_issue.get(issue_id, []),
            blockers_by_issue.get(issue_id, {"items": [], "count": 0}),
            handoffs_by_issue.get(issue_id),
        )
        # Defense in depth: a self-service response must never serialize content after
        # a visibility mismatch. The shared transaction should make this unreachable
        # under normal access functions; fail closed if those predicates ever drift.
        if self_view and not item["visible_to_subject"]:
            continue
        items.append(item)
    return {
        "subject": {
            "id": subject["id"],
            "name": subject["name"],
            "is_agent": bool(subject.get("is_agent")),
        },
        "items": items,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page_rows) if has_more else None,
        # Make the negative semantics machine-readable instead of relying only on
        # prose: blockers and warnings cover what this actor may see, not hidden work.
        "blocker_scope": "visible_to_subject",
    }


def _visibility_sql(
    project_column: str, visible_project_ids: set[int] | None
) -> tuple[str | None, list[int]]:
    if visible_project_ids is None:
        return None, []
    if not visible_project_ids:
        return f"{project_column} IS NULL", []
    ordered_ids = sorted(visible_project_ids)
    placeholders = ",".join("?" for _ in ordered_ids)
    return (
        f"({project_column} IS NULL OR {project_column} IN ({placeholders}))",
        ordered_ids,
    )


def _visible_to(project_id: int | None, visible_project_ids: set[int] | None) -> bool:
    return (
        project_id is None
        or visible_project_ids is None
        or project_id in visible_project_ids
    )


def _blocker_previews(
    conn: sqlite3.Connection,
    issue_ids: list[int],
    subject_visible_project_ids: set[int] | None,
) -> dict[int, dict]:
    """Return exact visible-open counts and at most ten summaries per target.

    One window query replaces per-inbox-item and per-blocker reads. The visibility
    predicate is the subject's even when an admin is supervising.
    """
    if not issue_ids:
        return {}

    target_placeholders = ",".join("?" for _ in issue_ids)
    visibility_sql, visibility_params = _visibility_sql(
        "bi.project_id", subject_visible_project_ids
    )
    visibility_clause = f"AND {visibility_sql} " if visibility_sql else ""
    blocker_category_sql = _status_category_sql("bi", "bps")
    rows = conn.execute(
        "WITH visible_open_blockers AS ("
        "SELECT l.to_id AS blocked_id, bi.id, bi.title, bi.status, "
        "bi.project_seq, bp.key AS project_key, "
        "ROW_NUMBER() OVER (PARTITION BY l.to_id ORDER BY bi.id) AS preview_rank, "
        "COUNT(*) OVER (PARTITION BY l.to_id) AS blocker_count "
        "FROM issue_links l "
        "JOIN issues bi ON bi.id = l.from_id "
        "LEFT JOIN projects bp ON bp.id = bi.project_id "
        "LEFT JOIN project_statuses bps "
        "ON bps.project_id = bi.project_id AND bps.name = bi.status "
        f"WHERE l.kind = 'blocks' AND l.to_id IN ({target_placeholders}) "
        f"AND {blocker_category_sql} != 'done' "
        f"{visibility_clause}"
        ") "
        "SELECT blocked_id, id, title, status, project_seq, project_key, "
        "preview_rank, blocker_count "
        "FROM visible_open_blockers WHERE preview_rank <= ? "
        "ORDER BY blocked_id, preview_rank",
        [*issue_ids, *visibility_params, _BLOCKER_PREVIEW_LIMIT],
    ).fetchall()

    grouped: dict[int, dict] = {}
    for row in rows:
        blocked_id = int(row["blocked_id"])
        bucket = grouped.setdefault(
            blocked_id, {"items": [], "count": int(row["blocker_count"])}
        )
        key = None
        if row["project_key"] and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        bucket["items"].append(
            {
                "id": row["id"],
                "key": key,
                "title": row["title"],
                "status": row["status"],
            }
        )
    return grouped


def _to_item(
    row: sqlite3.Row,
    subject_visible_project_ids: set[int] | None,
    issue_labels: list[dict],
    blocker_info: dict,
    open_claim_handoff: dict | None,
) -> dict:
    issue_id = int(row["id"])
    visible_to_subject = _visible_to(row["project_id"], subject_visible_project_ids)
    warnings: list[str] = []
    if not visible_to_subject:
        warnings.append("subject_cannot_see_issue")
    if not row["body"].strip():
        warnings.append("empty_description")
    if row["assignee_id"] is None:
        warnings.append("no_accountable_assignee")
    if blocker_info["count"]:
        warnings.append("visible_open_blockers")
    if open_claim_handoff is not None:
        warnings.append("open_claim_handoff")

    key = None
    if row["project_key"] and row["project_seq"] is not None:
        key = f"{row['project_key']}-{row['project_seq']}"

    return {
        "issue": {
            "id": issue_id,
            "key": key,
            "title": row["title"],
            "body": row["body"],
            "status": row["status"],
            "status_category": row["status_category"],
            "priority": row["priority"],
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "assignee_id": row["assignee_id"],
            "assignee_name": row["assignee_name"],
            "labels": issue_labels,
        },
        "delegated_at": row["delegated_at"],
        "delegated_by": {
            "id": row["delegated_by_id"],
            "name": row["delegated_by_name"],
        },
        "visible_to_subject": visible_to_subject,
        "visible_open_blockers": blocker_info["items"],
        "visible_open_blocker_count": blocker_info["count"],
        "visible_open_blockers_truncated": blocker_info["count"]
        > len(blocker_info["items"]),
        "open_claim_handoff": open_claim_handoff,
        "warnings": warnings,
    }
