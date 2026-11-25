"""Bounded exact-submission reports under the caller's read snapshot.

Newest canonical rows are selected BEFORE visibility and report parsing. Losing
access or a malformed new report can never resurrect an older visible PASS.
"""

import sqlite3
from typing import Any

from athena.aegis import (
    issue_activity,
    lease_commands,
    projects,
    review_request_projection,
    statuses,
)
from athena.aegis.project_brief import _excerpt, _person, _rows, _group
from athena.aegis.review_commands import latest_candidate_id
from athena.core import access, clock, db
from athena.core.ids import MAX_SQLITE_INTEGER

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
HISTORY_LIMIT = 5


def _candidate_base(conn: sqlite3.Connection, actor: dict | None) -> tuple[str, list]:
    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    return (
        "FROM issue_review_candidates c JOIN issues i ON i.id = c.issue_id "
        "JOIN activity a ON a.id = c.completion_event_id "
        "JOIN users u ON u.id = a.actor_id "
        "WHERE a.imported_at IS NULL AND a.visibility_restricted = 0 "
        + (f"AND ({gate}) " if gate else ""),
        params,
    )


def _candidate_columns() -> str:
    return (
        "c.id AS candidate_id, c.kind, c.source, c.revision, c.completion_event_id, "
        "a.created_at, a.actor_id, u.name AS actor_name, i.id AS issue_id, "
        "i.title, i.status, i.project_seq, "
        "(SELECT key FROM projects WHERE id = i.project_id) AS project_key, "
        f"{statuses.exact_category_sql('i')} AS status_category"
    )


def _key(row: sqlite3.Row) -> str | None:
    key, seq = row["project_key"], row["project_seq"]
    return f"{key}-{seq}" if key and seq is not None else None


def _latest_review(
    conn: sqlite3.Connection, candidate_id: int, actor: dict | None
) -> dict | None:
    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    row = conn.execute(
        "SELECT r.id AS review_id, r.review_issue_id, r.completion_event_id, "
        "a.created_at, a.actor_id, u.name AS actor_name, a.verb, a.imported_at, "
        "CASE WHEN length(a.detail) <= ? THEN a.detail ELSE '' END AS detail, "
        "i.title, i.project_seq, (SELECT key FROM projects WHERE id = i.project_id) AS project_key "
        "FROM issue_candidate_reviews r JOIN activity a ON a.id = r.completion_event_id "
        "JOIN issues i ON i.id = r.review_issue_id JOIN users u ON u.id = a.actor_id "
        "WHERE r.candidate_id = ? AND r.completion_event_id = "
        "(SELECT MAX(completion_event_id) FROM issue_candidate_reviews WHERE candidate_id = ?) "
        "AND a.imported_at IS NULL AND a.visibility_restricted = 0 "
        + (f"AND ({gate})" if gate else ""),
        [
            lease_commands.MAX_COMPLETION_REPORT_JSON_CHARS + 128,
            candidate_id,
            candidate_id,
            *params,
        ],
    ).fetchone()
    if row is None:
        return None
    report = lease_commands.reported_completion(dict(row))
    return {
        "review_id": row["review_id"],
        "review_issue": {
            "id": row["review_issue_id"],
            "key": _key(row),
            "title": _excerpt(row["title"]),
            "work_context_url": f"/issues/{row['review_issue_id']}/work-context",
            "authored_by": issue_activity.issue_text_authors(
                conn, [row["review_issue_id"]], actor=actor
            )[row["review_issue_id"]],
        },
        "completion_event_id": row["completion_event_id"],
        "reported_at": row["created_at"],
        "reported_by": _person(row["actor_id"], row["actor_name"]),
        "report_available": report is not None,
        "reported_verdict": None if report is None else report.get("verdict"),
        "reported_findings": None if report is None else report.get("findings"),
    }


def _item(
    conn: sqlite3.Connection, row: sqlite3.Row, actor: dict | None
) -> dict[str, Any]:
    latest = _latest_review(conn, row["candidate_id"], actor)
    state = "awaiting_review"
    if latest is not None:
        verdict = latest["reported_verdict"]
        state = (
            "reported_pass"
            if verdict in {"PASS", "PASS-WITH-NOTES"}
            else "reported_hold"
            if verdict in {"HOLD", "BLOCKED"}
            else "unknown"
        )
    return {
        "issue": {
            "id": row["issue_id"],
            "key": _key(row),
            "title": _excerpt(row["title"]),
            "status": _excerpt(row["status"]),
            "status_category": row["status_category"],
            "work_context_url": f"/issues/{row['issue_id']}/work-context",
            "authored_by": issue_activity.issue_text_authors(
                conn, [row["issue_id"]], actor=actor
            )[row["issue_id"]],
        },
        "candidate": {
            "candidate_id": row["candidate_id"],
            "identity": {
                "kind": row["kind"],
                "source": row["source"],
                "revision": row["revision"],
            },
            "completion_event_id": row["completion_event_id"],
            "reported_at": row["created_at"],
            "submitted_by": _person(row["actor_id"], row["actor_name"]),
        },
        "review_state": state,
        "latest_review": latest,
    }


def _candidate_page(
    conn: sqlite3.Connection,
    project_id: int,
    actor: dict | None,
    limit: int,
    cursor: int | None,
) -> tuple[int, list[sqlite3.Row]]:
    base, params = _candidate_base(conn, actor)
    base += (
        "AND i.project_id = ? AND i.archived_at IS NULL "
        "AND c.id = (SELECT MAX(latest.id) FROM issue_review_candidates latest "
        "WHERE latest.issue_id = i.id)"
    )
    params = [*params, project_id]
    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    # A replacement above the cursor must still supersede its predecessor.
    if cursor is not None:
        base += " AND c.id < ?"
        params.append(cursor)
    rows = conn.execute(
        f"SELECT {_candidate_columns()} {base} ORDER BY c.id DESC, i.id LIMIT ?",
        [*params, limit + 1],
    ).fetchall()
    return total, rows


def build_project_review_queue(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    actor: dict | None,
    limit: int = DEFAULT_LIMIT,
    cursor: int | None = None,
) -> dict | None:
    for name, value, maximum in (
        ("project_id", project_id, MAX_SQLITE_INTEGER),
        ("limit", limit, MAX_LIMIT),
        ("cursor", cursor, MAX_SQLITE_INTEGER),
    ):
        if name == "cursor" and value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    with db.transaction(conn):
        project = projects.get_project(conn, project_id)
        if project is None or not access.can_see_project(conn, actor, project_id):
            return None
        now = clock.now()
        total, rows = _candidate_page(conn, project_id, actor, limit, cursor)
        items = []
        for row in rows[:limit]:
            item = _item(conn, row, actor)
            requests = review_request_projection.visible_requests(
                conn,
                actor=actor,
                lane="candidate",
                scope_id=row["candidate_id"],
                now=now,
            )
            if requests is not None:
                item["review_requests"] = requests
            items.append(item)
        return {
            "schema": "athena.project_review_queue.v1",
            "scope": "visible_to_request_actor",
            "observed_at": now.isoformat(),
            "project": {
                "id": project_id,
                "key": project["key"],
                "name": _excerpt(project["name"]),
            },
            **_group(total, items, limit),
            "next_cursor": rows[limit - 1]["candidate_id"]
            if len(rows) > limit
            else None,
            "semantics": {
                "snapshot": "current_visible_snapshot",
                "review_states": "advisory actor reports, not verification or acceptance",
                "authority": "read only; no state change or authority granted",
            },
        }


def issue_review_tracking(
    conn: sqlite3.Connection, *, issue_id: int, actor: dict | None
) -> dict | None:
    """Borrow the full work-context snapshot; no new clock in its deterministic ETag."""
    current = latest_candidate_id(conn, issue_id)
    if current is None:
        return None
    base, params = _candidate_base(conn, actor)
    row = conn.execute(
        f"SELECT {_candidate_columns()} {base} AND c.id = ?", [*params, current]
    ).fetchone()
    if row is None:
        return None
    total, rows = _rows(
        conn,
        columns=_candidate_columns(),
        base=base + "AND c.issue_id = ?",
        params=[*params, issue_id],
        order="c.id DESC",
        limit=HISTORY_LIMIT,
    )
    current_item = _item(conn, row, actor)
    history = [
        {
            **(
                current_item
                if item["candidate_id"] == current
                else _item(conn, item, actor)
            ),
            "is_current": item["candidate_id"] == current,
        }
        for item in rows
    ]
    return {
        "current_candidate": current_item,
        "recent_candidates": _group(total, history, HISTORY_LIMIT),
    }
