"""Live, read-only project briefs assembled from authoritative Athena records."""

from __future__ import annotations

import sqlite3
from typing import Any

from athena.aegis import room_context, room_timeline, rooms
from athena.core import access, db


SCHEMA = "athena.room-brief.v1"
ISSUE_LIMIT = 10
BLOCKER_LIMIT = 10
AGENT_LIMIT = 10
DECISION_LIMIT = 10
KNOWLEDGE_LIMIT = 10
TIMELINE_LIMIT = 15


def _safe_title(value: object) -> str:
    return room_timeline.project_authoritative_text(
        value,
        redacted=room_timeline.REDACTED_TITLE,
        max_chars=rooms.MAX_TITLE_CHARS,
    )[0]


_NOT_DONE = (
    "COALESCE((SELECT ps.category FROM project_statuses ps "
    "WHERE ps.project_id = i.project_id AND ps.name = i.status LIMIT 1), "
    "CASE WHEN NOT EXISTS (SELECT 1 FROM project_statuses any_ps "
    "WHERE any_ps.project_id = i.project_id) THEN CASE i.status "
    "WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing' "
    "WHEN 'done' THEN 'done' END END) IS NOT 'done'"
)


def _group(
    items: list[dict[str, Any]],
    *,
    visible_total: int,
    unavailable_reason: str | None = None,
    source_clipped: bool = False,
) -> dict[str, Any]:
    return {
        "items": items,
        "visible_total": visible_total,
        "clipped": source_clipped or visible_total > len(items),
        "unavailable_reason": unavailable_reason,
    }


def _issue_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "key": f"{row['project_key']}-{row['project_seq']}",
        "title": _safe_title(row["title"]),
        "status": row["status"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "receipt": f"/issues/{row['id']}",
    }


def _open_priority(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    base = (
        "FROM issues i JOIN projects p ON p.id = i.project_id "
        "WHERE i.project_id = ? AND i.archived_at IS NULL "
        f"AND ({_NOT_DONE})"
    )
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n {base}", (project_id,)).fetchone()["n"]
    )
    rows = conn.execute(
        "SELECT i.id, i.title, i.status, i.priority, i.project_seq, "
        f"i.created_at, p.key AS project_key {base} "
        "ORDER BY CASE i.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, i.id LIMIT ?",
        (project_id, ISSUE_LIMIT),
    ).fetchall()
    return _group([_issue_item(row) for row in rows], visible_total=total)


def _blockers(
    conn: sqlite3.Connection,
    project_id: int,
    actor: dict[str, Any] | None,
) -> dict[str, Any]:
    visible_projects = access.visible_project_filter(conn, actor)
    if visible_projects is None:
        blocker_access = "1 = 1"
        blocker_params: list[Any] = []
    elif visible_projects:
        ordered_projects = sorted(visible_projects)
        placeholders = ",".join("?" for _ in ordered_projects)
        blocker_access = (
            f"blocker.project_id IS NULL OR blocker.project_id IN ({placeholders})"
        )
        blocker_params = ordered_projects
    else:
        blocker_access = "blocker.project_id IS NULL"
        blocker_params = []
    blocker_not_done = _NOT_DONE.replace("i.", "blocker.")
    base = (
        "FROM issues i JOIN projects p ON p.id = i.project_id "
        "WHERE i.project_id = ? AND i.archived_at IS NULL "
        f"AND ({_NOT_DONE}) AND EXISTS ("
        "SELECT 1 FROM issue_links l JOIN issues blocker ON blocker.id = l.from_id "
        "WHERE l.to_id = i.id AND l.kind = 'blocks' "
        "AND blocker.archived_at IS NULL "
        f"AND ({blocker_access}) AND ({blocker_not_done}))"
    )
    base_params = [project_id, *blocker_params]
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n {base}", base_params).fetchone()["n"]
    )
    rows = conn.execute(
        "SELECT i.id, i.title, i.status, i.priority, i.project_seq, "
        f"i.created_at, p.key AS project_key {base} "
        "ORDER BY CASE i.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, i.id LIMIT ?",
        [*base_params, BLOCKER_LIMIT],
    ).fetchall()
    return _group([_issue_item(row) for row in rows], visible_total=total)


def _agents(
    conn: sqlite3.Connection,
    room_id: int,
    actor: dict[str, Any] | None,
) -> dict[str, Any]:
    projection = room_timeline.list_visible_agents(
        conn, room_id, actor=actor, limit=AGENT_LIMIT
    )
    assert projection is not None
    items = [
        {
            "id": item["id"],
            "name": _safe_title(item["name"]),
            "status": item["account_state"],
            "current_claims": item["current_claims"],
            "latest_check_in": item["latest_check_in"],
            "detail": ("Recorded claim/check-in observations; not proof of execution."),
            "receipt": (
                item["latest_check_in"]["receipt"]
                if item["latest_check_in"] is not None
                else None
            ),
        }
        for item in projection["items"]
    ]
    return _group(items, visible_total=int(projection["visible_total"]))


def _decisions(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
) -> dict[str, Any]:
    where, params = room_timeline._timeline_where(conn, room, actor)
    decision = (
        "a.imported_at IS NULL AND "
        "(re.event_kind = 'decision' OR a.verb LIKE 'approval\\_%' ESCAPE '\\')"
        " AND (re.event_kind IS NOT 'decision' OR successor.activity_id IS NULL)"
    )
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM activity a "
            "LEFT JOIN room_events re ON re.activity_id = a.id "
            "LEFT JOIN room_events successor "
            "ON successor.supersedes_event_id = a.id "
            f"WHERE {where} AND {decision}",
            params,
        ).fetchone()["n"]
    )
    rows = conn.execute(
        "SELECT a.id AS activity_id, a.actor_id, u.name AS actor_name, "
        "u.is_agent AS actor_is_agent, a.verb, a.target_kind, a.target_id, "
        "a.detail, a.created_at, a.run_id, a.parent_run_id, "
        "a.forked_from_event_id, a.imported_at, re.room_id AS authored_room_id, "
        "re.event_kind, re.reference_kind, re.reference_id, re.content_sha256, "
        "re.supersedes_event_id, successor.activity_id AS successor_event_id "
        "FROM activity a JOIN users u ON u.id = a.actor_id "
        "LEFT JOIN room_events re ON re.activity_id = a.id "
        "LEFT JOIN room_events successor "
        "ON successor.supersedes_event_id = a.id "
        f"WHERE {where} AND {decision} ORDER BY a.id DESC LIMIT ?",
        [*params, DECISION_LIMIT],
    ).fetchall()
    items = []
    for raw in rows:
        item = room_timeline._timeline_item(conn, room, actor, dict(raw))
        items.append(
            {
                "activity_id": item["activity_id"],
                "title": item["event_kind"] or item["verb"].replace("_", " "),
                "body": item["body"],
                "actor": item["actor"],
                "created_at": item["created_at"],
                "receipt": f"/events?after={int(item['activity_id']) - 1}",
            }
        )
    return _group(items, visible_total=total)


def _knowledge(
    conn: sqlite3.Connection,
    project_id: int,
    actor: dict[str, Any] | None,
) -> dict[str, Any]:
    issue_rows = conn.execute(
        "SELECT id FROM issues WHERE project_id = ? AND archived_at IS NULL ORDER BY id LIMIT ?",
        (project_id, room_context.MAX_SCOPE_ISSUES + 1),
    ).fetchall()
    issue_ids = [int(row["id"]) for row in issue_rows]
    issues_clipped = len(issue_ids) > room_context.MAX_SCOPE_ISSUES
    issue_ids = issue_ids[: room_context.MAX_SCOPE_ISSUES]
    page_ids, pages_clipped = room_context._related_visible_page_scope(
        conn, issue_ids, actor
    )
    rows_by_id: dict[int, sqlite3.Row] = {}
    for chunk in room_context._chunks(page_ids):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            "SELECT id, title, updated_at FROM pages "
            f"WHERE id IN ({placeholders}) AND archived_at IS NULL",
            chunk,
        ).fetchall():
            rows_by_id[int(row["id"])] = row
    ordered = sorted(
        rows_by_id.values(),
        key=lambda row: (str(row["title"]).casefold(), int(row["id"])),
    )
    selected = ordered[:KNOWLEDGE_LIMIT]
    return _group(
        [
            {
                "id": row["id"],
                "title": _safe_title(row["title"]),
                "updated_at": row["updated_at"],
                "receipt": f"/pages/{row['id']}",
            }
            for row in selected
        ],
        visible_total=len(ordered),
        source_clipped=issues_clipped or pages_clipped,
        unavailable_reason=(
            "Related knowledge is a lower-bound view because its source scope "
            "exceeded the brief cap."
            if issues_clipped or pages_clipped
            else None
        ),
    )


def _recent_timeline(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    cursor: str | None,
) -> dict[str, Any]:
    before_id = room_timeline.decode_cursor(cursor, int(room["id"]))
    where, params = room_timeline._timeline_where(
        conn, room, actor, before_id=before_id
    )
    where += " AND a.imported_at IS NULL AND successor.activity_id IS NULL"
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM activity a "
            "LEFT JOIN room_events re ON re.activity_id = a.id "
            "LEFT JOIN room_events successor "
            "ON successor.supersedes_event_id = a.id "
            f"WHERE {where}",
            params,
        ).fetchone()["n"]
    )
    page = room_timeline.list_timeline(
        conn,
        int(room["id"]),
        actor=actor,
        cursor=cursor,
        limit=TIMELINE_LIMIT,
        native_only=True,
        current_room_events_only=True,
    )
    assert page is not None
    items = [
        {
            "activity_id": item["activity_id"],
            "title": item["event_kind"] or item["verb"].replace("_", " "),
            "verb": item["verb"],
            "body": item["body"],
            "actor": item["actor"],
            "created_at": item["created_at"],
            "receipt": f"/events?after={int(item['activity_id']) - 1}",
        }
        for item in page["items"]
    ]
    return _group(items, visible_total=total)


def _build(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    cursor: str | None,
) -> dict[str, Any]:
    project = conn.execute(
        "SELECT name, description FROM projects WHERE id = ? "
        "AND activity_scope_key = ?",
        (room["project_id"], room["project_scope_key"]),
    ).fetchone()
    assert project is not None
    snapshot_at = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS now"
    ).fetchone()["now"]
    uncertainty = [
        "Claims and check-ins are recorded observations, not proof of execution.",
        "Empty groups mean no matching record was visible in this bounded snapshot.",
    ]
    if room.get("degraded_reason"):
        uncertainty.append(str(room["degraded_reason"]))
    purpose, _ = room_timeline.project_authoritative_text(
        project["description"] or room["purpose"], max_chars=rooms.MAX_PURPOSE_CHARS
    )
    return {
        "schema": SCHEMA,
        "room": room_timeline.public_room(room),
        "snapshot_at": snapshot_at,
        "purpose": purpose,
        "open_priority": _open_priority(conn, int(room["project_id"])),
        "blockers": _blockers(conn, int(room["project_id"]), actor),
        "agents": _agents(conn, int(room["id"]), actor),
        "decisions": _decisions(conn, room, actor),
        "knowledge": _knowledge(conn, int(room["project_id"]), actor),
        "recent_timeline": _recent_timeline(conn, room, actor, cursor),
        "uncertainty": uncertainty,
    }


def build_live_brief(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    actor: dict[str, Any] | None,
    cursor: str | None = None,
) -> dict[str, Any] | None:
    """Return one single-snapshot project brief or None for hidden/missing."""
    with db.transaction(conn):
        room = rooms.get_visible_room(
            conn, actor=actor, room_id=room_id, include_archived=True
        )
        if room is None:
            return None
        return _build(conn, room, actor, cursor)


build_brief = build_live_brief
