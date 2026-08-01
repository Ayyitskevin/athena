"""Storage and visibility primitives for project-scoped Athena Rooms.

The activity log remains the ordered audit/event stream. The rooms table owns
room identity and room_events is the immutable typed extension for room-authored
activity; event prose remains activity.detail.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from athena.core import access

ROOM_TYPES: Final = frozenset({"project", "work_item", "agent", "brief"})
ROOM_VISIBILITIES: Final = frozenset({"project", "members"})
EVENT_KINDS: Final = frozenset(
    {"message", "check_in", "handoff", "decision", "evidence", "system_notice"}
)
REFERENCE_KINDS: Final = frozenset(
    {
        "issue",
        "page",
        "approval",
        "activity",
        "handoff",
        "dispatch",
        "run",
        "attachment",
    }
)

MAX_SLUG_CHARS: Final = 80
MAX_TITLE_CHARS: Final = 300
MAX_PURPOSE_CHARS: Final = 4000
MAX_EVENT_BODY_CHARS: Final = 12_000
MAX_REFERENCE_ID_CHARS: Final = 200
MAX_EVENT_PAGE: Final = 200
MAX_SQLITE_ID: Final = (1 << 63) - 1


def is_sqlite_id(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_SQLITE_ID
    )


_UNGATED = object()

_ROOM_SELECT = (
    "SELECT r.*, p.id AS live_project_id, "
    "i.id AS linked_issue_exists, i.project_id AS linked_issue_project_id, "
    "u.id AS linked_agent_exists, u.is_agent AS linked_agent_is_agent "
    "FROM rooms r "
    "LEFT JOIN projects p ON p.id = r.project_id "
    " AND p.activity_scope_key = r.project_scope_key "
    "LEFT JOIN issues i ON i.id = r.issue_id "
    "LEFT JOIN users u ON u.id = r.agent_id"
)

_EVENT_SELECT = (
    "SELECT a.id, a.id AS activity_id, re.room_id, re.event_kind, "
    "re.reference_kind, re.reference_id, re.content_sha256, "
    "re.supersedes_event_id, successor.activity_id AS superseded_by_event_id, "
    "a.actor_id, u.name AS actor_name, u.is_agent AS actor_is_agent, "
    "a.verb, a.target_kind, a.target_id, a.detail, a.created_at, "
    "a.run_id, a.parent_run_id, a.forked_from_event_id, a.imported_at, "
    "a.reverses_event_id, a.visibility_restricted, a.delivery_eligible "
    "FROM room_events re "
    "JOIN activity a ON a.id = re.activity_id "
    "JOIN users u ON u.id = a.actor_id "
    "LEFT JOIN room_events successor "
    " ON successor.supersedes_event_id = re.activity_id "
    "WHERE a.imported_at IS NULL"
)


def _row_to_room(row: sqlite3.Row) -> dict:
    room = dict(row)
    room["archived"] = room["archived_at"] is not None
    room["is_detached"] = False
    room["link_state"] = "active"
    if room["live_project_id"] is None:
        room["link_state"] = "owning_project_unavailable"
    elif room["room_type"] == "work_item":
        moved = (
            room["linked_issue_exists"] is None
            or room["linked_issue_project_id"] != room["project_id"]
        )
        room["is_detached"] = moved
        if moved:
            room["link_state"] = "linked_work_moved"
    elif room["room_type"] == "agent" and (
        room["linked_agent_exists"] is None or not room["linked_agent_is_agent"]
    ):
        room["link_state"] = "linked_agent_unavailable"
    room["degraded_reason"] = {
        "owning_project_unavailable": "The owning project is no longer available.",
        "linked_work_moved": (
            "The linked work item moved; this room is read-only until re-scoped."
        ),
        "linked_agent_unavailable": "The linked agent is no longer available.",
    }.get(room["link_state"])
    if room["linked_agent_is_agent"] is not None:
        room["linked_agent_is_agent"] = bool(room["linked_agent_is_agent"])
    return room


def _row_to_event(row: sqlite3.Row) -> dict:
    event = dict(row)
    event["event_id"] = event["activity_id"]
    event["body"] = event["detail"]
    event["actor_is_agent"] = bool(event["actor_is_agent"])
    event["delivery_eligible"] = bool(event["delivery_eligible"])
    event["visibility_restricted"] = bool(event["visibility_restricted"])
    return event


def get_room(conn: sqlite3.Connection, room_id: int) -> dict | None:
    """Return one room without an audience gate, including degraded link state."""
    if not is_sqlite_id(room_id):
        return None
    row = conn.execute(f"{_ROOM_SELECT} WHERE r.id = ?", (room_id,)).fetchone()
    return _row_to_room(row) if row else None


def get_room_by_slug(
    conn: sqlite3.Connection, project_id: int, slug: str
) -> dict | None:
    """Resolve a slug only within the current generation of a live project."""
    if not is_sqlite_id(project_id):
        return None
    row = conn.execute(
        f"{_ROOM_SELECT} WHERE r.project_id = ? AND r.slug = ? "
        "AND r.project_scope_key = ("
        "SELECT activity_scope_key FROM projects WHERE id = ?)",
        (project_id, slug, project_id),
    ).fetchone()
    return _row_to_room(row) if row else None


def get_work_item_room(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    """Return the focused room linked to an issue, without a read gate."""
    if not is_sqlite_id(issue_id):
        return None
    row = conn.execute(
        f"{_ROOM_SELECT} WHERE r.room_type = 'work_item' AND r.issue_id = ?",
        (issue_id,),
    ).fetchone()
    return _row_to_room(row) if row else None


def can_see_room(
    conn: sqlite3.Connection, actor: dict | None, room: dict | int
) -> bool:
    """Apply project access and the optional members-only narrowing."""
    resolved = get_room(conn, room) if isinstance(room, int) else room
    if resolved is None or resolved.get("live_project_id") is None:
        return False
    project_id = resolved["project_id"]
    if not access.can_see_project(conn, actor, project_id):
        return False
    if resolved["visibility"] == "project":
        return True
    if actor is None:
        return False
    if actor.get("role") == "admin":
        return True
    project = conn.execute(
        "SELECT created_by FROM projects WHERE id = ? AND activity_scope_key = ?",
        (project_id, resolved["project_scope_key"]),
    ).fetchone()
    if project is None:
        return False
    if actor.get("id") == project["created_by"]:
        return True
    return (
        conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, actor.get("id")),
        ).fetchone()
        is not None
    )


def get_visible_room(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    room_id: int,
    include_archived: bool = True,
) -> dict | None:
    """Return a visible room; hidden and missing deliberately collapse to None."""
    room = get_room(conn, room_id)
    if room is None or (not include_archived and room["archived"]):
        return None
    return room if can_see_room(conn, actor, room) else None


def list_rooms(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    actor: dict | None | object = _UNGATED,
    include_archived: bool = False,
) -> list[dict]:
    """List one live project's rooms in deterministic presentation order."""
    if not is_sqlite_id(project_id):
        return []
    project = conn.execute(
        "SELECT activity_scope_key FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        return []
    resolved_actor = actor if isinstance(actor, dict) else None
    if actor is not _UNGATED and not access.can_see_project(
        conn, resolved_actor, project_id
    ):
        return []
    clauses = ["r.project_id = ?", "r.project_scope_key = ?"]
    params: list[object] = [project_id, project["activity_scope_key"]]
    if not include_archived:
        clauses.append("r.archived_at IS NULL")
    rows = conn.execute(
        f"{_ROOM_SELECT} WHERE {' AND '.join(clauses)} "
        "ORDER BY CASE r.room_type "
        "WHEN 'project' THEN 0 WHEN 'brief' THEN 1 "
        "WHEN 'work_item' THEN 2 ELSE 3 END, "
        "r.title COLLATE NOCASE, r.id",
        params,
    ).fetchall()
    visible = [_row_to_room(row) for row in rows]
    if actor is _UNGATED:
        return visible
    return [room for room in visible if can_see_room(conn, resolved_actor, room)]


def timeline_scope(room: dict) -> dict:
    """Return inert coordinates used by the mixed timeline projection."""
    scope_kind = {
        "project": "project",
        "brief": "project",
        "work_item": "issue",
        "agent": "agent",
    }[room["room_type"]]
    return {
        "scope_kind": scope_kind,
        "room_id": room["id"],
        "project_id": room["project_id"],
        "project_scope_key": room["project_scope_key"],
        "issue_id": room.get("issue_id"),
        "agent_id": room.get("agent_id"),
        "detached": bool(room.get("is_detached")),
    }


def create_room(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    slug: str,
    room_type: str,
    title: str,
    purpose: str,
    visibility: str,
    created_by: int,
    issue_id: int | None = None,
    agent_id: int | None = None,
    project_scope_key: str | None = None,
    commit: bool = True,
) -> dict:
    """Insert validated storage; application policy lives in room_commands."""
    if project_scope_key is None:
        project = conn.execute(
            "SELECT activity_scope_key FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise sqlite3.IntegrityError("matching live room project required")
        project_scope_key = project["activity_scope_key"]
    cur = conn.execute(
        "INSERT INTO rooms ("
        "project_id, project_scope_key, slug, room_type, title, purpose, "
        "visibility, issue_id, agent_id, created_by"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            project_scope_key,
            slug,
            room_type,
            title,
            purpose,
            visibility,
            issue_id,
            agent_id,
            created_by,
        ),
    )
    if commit:
        conn.commit()
    room_id = cur.lastrowid
    assert room_id is not None
    room = get_room(conn, room_id)
    assert room is not None
    return room


def archive_room(
    conn: sqlite3.Connection, room_id: int, *, commit: bool = True
) -> dict | None:
    """Irreversibly archive an operational room; main/brief are DB-protected."""
    cur = conn.execute(
        "UPDATE rooms SET archived_at = datetime('now'), "
        "updated_at = datetime('now') "
        "WHERE id = ? AND archived_at IS NULL",
        (room_id,),
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return get_room(conn, room_id)
    return get_room(conn, room_id)


def _project_row(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name, description, created_by, created_at, activity_scope_key "
        "FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()


def ensure_project_rooms(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    created_by: int | None = None,
) -> dict:
    """Ensure and return the invariant main and brief rooms in the caller's tx."""
    project = _project_row(conn, project_id)
    if project is None:
        raise sqlite3.IntegrityError("matching live room project required")
    owner = created_by if created_by is not None else project["created_by"]
    result: dict[str, dict] = {}
    specs = (
        (
            "project",
            "main",
            str(project["name"])[:MAX_TITLE_CHARS],
            str(project["description"] or "")[:MAX_PURPOSE_CHARS],
        ),
        (
            "brief",
            "brief",
            f"{project['name']} live brief"[:MAX_TITLE_CHARS],
            "Live, read-only project coordination brief",
        ),
    )
    for room_type, slug, title, purpose in specs:
        row = conn.execute(
            "SELECT id FROM rooms WHERE project_scope_key = ? AND room_type = ?",
            (project["activity_scope_key"], room_type),
        ).fetchone()
        if row is None:
            room = create_room(
                conn,
                project_id=project_id,
                project_scope_key=project["activity_scope_key"],
                slug=slug,
                room_type=room_type,
                title=title,
                purpose=purpose,
                visibility="project",
                created_by=owner,
                commit=False,
            )
        else:
            conn.execute(
                "UPDATE rooms SET title = ?, purpose = ?, "
                "updated_at = CASE WHEN title IS NOT ? OR purpose IS NOT ? "
                "THEN datetime('now') ELSE updated_at END "
                "WHERE id = ?",
                (title, purpose, title, purpose, row["id"]),
            )
            existing_room = get_room(conn, row["id"])
            assert existing_room is not None
            room = existing_room
        result[room_type] = room
    return result


def ensure_work_item_room(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    created_by: int | None = None,
) -> dict | None:
    """Ensure one work-item room, or return None while the issue is backlog."""
    issue = conn.execute(
        "SELECT id, title, project_id, created_by FROM issues WHERE id = ?",
        (issue_id,),
    ).fetchone()
    if issue is None:
        return None
    existing = conn.execute(
        "SELECT id FROM rooms WHERE room_type = 'work_item' AND issue_id = ?",
        (issue_id,),
    ).fetchone()
    if issue["project_id"] is None:
        if existing is None:
            return None
        title = str(issue["title"])[:MAX_TITLE_CHARS]
        purpose = "Focused work-item coordination"
        conn.execute(
            "UPDATE rooms SET title = ?, purpose = ?, updated_at = CASE WHEN "
            "title IS NOT ? OR purpose IS NOT ? "
            "THEN datetime('now') ELSE updated_at END WHERE id = ?",
            (title, purpose, title, purpose, existing["id"]),
        )
        detached = get_room(conn, existing["id"])
        assert detached is not None
        return detached
    project = _project_row(conn, issue["project_id"])
    if project is None:
        return get_room(conn, existing["id"]) if existing is not None else None
    title = str(issue["title"])[:MAX_TITLE_CHARS]
    purpose = "Focused work-item coordination"
    if existing is not None:
        room = get_room(conn, existing["id"])
        assert room is not None
        conn.execute(
            "UPDATE rooms SET project_id = ?, project_scope_key = ?, "
            "title = ?, purpose = ?, updated_at = CASE WHEN "
            "project_id IS NOT ? OR project_scope_key IS NOT ? "
            "OR title IS NOT ? OR purpose IS NOT ? "
            "THEN datetime('now') ELSE updated_at END WHERE id = ?",
            (
                project["id"],
                project["activity_scope_key"],
                title,
                purpose,
                project["id"],
                project["activity_scope_key"],
                title,
                purpose,
                room["id"],
            ),
        )
        room = get_room(conn, room["id"])
        assert room is not None
        return room
    return create_room(
        conn,
        project_id=project["id"],
        project_scope_key=project["activity_scope_key"],
        slug=f"work-item-{issue_id}",
        room_type="work_item",
        title=title,
        purpose=purpose,
        visibility="project",
        issue_id=issue_id,
        created_by=created_by if created_by is not None else issue["created_by"],
        commit=False,
    )


def move_work_item_room(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    project_id: int | None,
) -> dict | None:
    """Synchronize the stable room after its issue project mutation.

    The caller first updates issues.project_id in the same immediate transaction.
    Moving to backlog leaves the room in its last project and detached/read-only;
    a later project assignment re-scopes that same id and slug.
    """
    issue = conn.execute(
        "SELECT project_id, created_by FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()
    if issue is None or issue["project_id"] != project_id:
        raise sqlite3.IntegrityError("issue project must be updated before room move")
    return ensure_work_item_room(
        conn, issue_id=issue_id, created_by=issue["created_by"]
    )


def ensure_agent_room(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    agent_id: int,
    created_by: int,
) -> dict:
    """Ensure one operational room for an assigned agent in a project."""
    project = _project_row(conn, project_id)
    agent = conn.execute(
        "SELECT id, name FROM users WHERE id = ? AND is_agent = 1", (agent_id,)
    ).fetchone()
    if project is None:
        raise sqlite3.IntegrityError("matching live room project required")
    if agent is None:
        raise sqlite3.IntegrityError("matching agent account required")
    existing = conn.execute(
        "SELECT id FROM rooms WHERE project_scope_key = ? "
        "AND room_type = 'agent' AND agent_id = ?",
        (project["activity_scope_key"], agent_id),
    ).fetchone()
    if existing is not None:
        generated_purpose = "Agent coordination and visible work receipts"
        conn.execute(
            "UPDATE rooms SET title = ?, updated_at = CASE WHEN title IS NOT ? "
            "THEN datetime('now') ELSE updated_at END "
            "WHERE id = ? AND purpose = ?",
            (
                str(agent["name"])[:MAX_TITLE_CHARS],
                str(agent["name"])[:MAX_TITLE_CHARS],
                existing["id"],
                generated_purpose,
            ),
        )
        room = get_room(conn, existing["id"])
        assert room is not None
        return room
    return create_room(
        conn,
        project_id=project_id,
        project_scope_key=project["activity_scope_key"],
        slug=f"agent-{agent_id}",
        room_type="agent",
        title=str(agent["name"])[:MAX_TITLE_CHARS],
        purpose="Agent coordination and visible work receipts",
        visibility="members",
        agent_id=agent_id,
        created_by=created_by,
        commit=False,
    )


create_default_project_rooms = ensure_project_rooms
create_default_issue_room = ensure_work_item_room


def create_room_event(
    conn: sqlite3.Connection,
    *,
    activity_id: int,
    room_id: int,
    event_kind: str,
    content_sha256: str,
    reference_kind: str | None = None,
    reference_id: str | None = None,
    supersedes_event_id: int | None = None,
    commit: bool = True,
) -> dict:
    """Attach typed metadata to an already-recorded room activity row."""
    conn.execute(
        "INSERT INTO room_events ("
        "activity_id, room_id, event_kind, reference_kind, reference_id, "
        "content_sha256, supersedes_event_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            activity_id,
            room_id,
            event_kind,
            reference_kind,
            reference_id,
            content_sha256,
            supersedes_event_id,
        ),
    )
    if commit:
        conn.commit()
    event = get_room_event(conn, activity_id)
    assert event is not None
    return event


def get_room_event(conn: sqlite3.Connection, event_id: int) -> dict | None:
    if not is_sqlite_id(event_id):
        return None
    row = conn.execute(
        f"{_EVENT_SELECT} AND re.activity_id = ?", (event_id,)
    ).fetchone()
    return _row_to_event(row) if row else None


def list_room_events(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    before_id: int | None = None,
    limit: int = 50,
    current_only: bool = False,
) -> list[dict]:
    """Return raw room events newest first by immutable activity id.

    This storage primitive is ungated. Projections authorize the room and retain
    the shared activity visibility predicate for every returned event.
    """
    if not is_sqlite_id(room_id):
        raise ValueError("room_id must be a SQLite positive integer")
    if before_id is not None and not is_sqlite_id(before_id):
        raise ValueError("before_id must be a SQLite positive integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_EVENT_PAGE
    ):
        raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE}")
    clauses = ["re.room_id = ?"]
    params: list[object] = [room_id]
    if before_id is not None:
        clauses.append("re.activity_id < ?")
        params.append(before_id)
    if current_only:
        clauses.append("successor.activity_id IS NULL")
    params.append(limit)
    rows = conn.execute(
        f"{_EVENT_SELECT} AND {' AND '.join(clauses)} "
        "ORDER BY re.activity_id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def list_rooms_page(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    actor: dict | None,
    include_archived: bool = False,
    after_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return an id-ordered keyset page with visibility applied before LIMIT."""
    if not is_sqlite_id(project_id):
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if after_id is not None and (
        not isinstance(after_id, int)
        or isinstance(after_id, bool)
        or not 0 <= after_id <= MAX_SQLITE_ID
    ):
        raise ValueError("after_id must be a bounded non-negative integer")
    project = conn.execute(
        "SELECT activity_scope_key FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None or not access.can_see_project(conn, actor, project_id):
        return []
    clauses = ["r.project_id = ?", "r.project_scope_key = ?"]
    params: list[object] = [project_id, project["activity_scope_key"]]
    if not include_archived:
        clauses.append("r.archived_at IS NULL")
    if after_id is not None:
        clauses.append("r.id > ?")
        params.append(after_id)
    if actor is None:
        clauses.append("r.visibility = 'project'")
    elif actor.get("role") != "admin":
        clauses.append(
            "(r.visibility = 'project' OR (r.visibility = 'members' AND ("
            "EXISTS (SELECT 1 FROM projects owner_project "
            "WHERE owner_project.id = r.project_id "
            "AND owner_project.activity_scope_key = r.project_scope_key "
            "AND owner_project.created_by = ?) OR "
            "EXISTS (SELECT 1 FROM project_members pm "
            "WHERE pm.project_id = r.project_id AND pm.user_id = ?))))"
        )
        params.extend([actor.get("id"), actor.get("id")])
    params.append(limit)
    rows = conn.execute(
        f"{_ROOM_SELECT} WHERE {' AND '.join(clauses)} ORDER BY r.id ASC LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_room(row) for row in rows]
