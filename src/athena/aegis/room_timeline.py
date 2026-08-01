"""Visibility-safe read projections for Athena room timelines and agents.

Rooms do not own the issue, approval, run, or knowledge facts they display.  This
module projects those append-only activity facts for one currently-authorized
reader and resolves linked records again at read time.  Hidden records are never
allowed to influence the page window or an enrichment.
"""

from __future__ import annotations

import base64
import binascii
import sqlite3
from typing import Any
from urllib.parse import quote

from athena.aegis import room_commands, rooms
from athena.core import access, activity, agent_run_checkins, tokens


DEFAULT_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_AGENT_LIMIT = 20
MAX_AGENT_LIMIT = 50
MAX_CAPABILITY_TOKEN_SCAN = 100
RECENT_CONTRIBUTION_LIMIT = 5
BODY_LIMIT = 4_000
REDACTED_DETAIL = "[authoritative detail redacted]"
REDACTED_TITLE = "[authoritative title redacted]"
INCOMPLETE_RUN = "incomplete_or_mixed_visibility_run"

_CURSOR_PREFIX = "athena.room-timeline.v1:"
_UNAVAILABLE = "not_visible_or_missing"


class InvalidCursor(ValueError):
    """A timeline cursor is malformed or belongs to another contract."""

    kind = "invalid_cursor"
    status_code = 422


def encode_cursor(room_id: int, activity_id: int) -> str:
    """Encode a room-bound positive activity id as an opaque URL-safe cursor."""
    if not rooms.is_sqlite_id(room_id):
        raise ValueError("room id must be a SQLite positive integer")
    if not rooms.is_sqlite_id(activity_id):
        raise ValueError("activity id must be a SQLite positive integer")
    raw = f"{_CURSOR_PREFIX}{room_id}:{activity_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, room_id: int) -> int | None:
    """Decode one canonical cursor only for the room that minted it."""
    if not rooms.is_sqlite_id(room_id):
        raise ValueError("room id must be a SQLite positive integer")
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > 128:
        raise InvalidCursor("invalid room timeline cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("invalid room timeline cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise InvalidCursor("invalid room timeline cursor")
    value = raw[len(_CURSOR_PREFIX) :]
    parts = value.split(":")
    if len(parts) != 2 or any(
        not part.isascii() or not part.isdecimal() or part.startswith("0")
        for part in parts
    ):
        raise InvalidCursor("invalid room timeline cursor")
    encoded_room_id, activity_id = (int(part) for part in parts)
    if (
        encoded_room_id != room_id
        or not rooms.is_sqlite_id(encoded_room_id)
        or not rooms.is_sqlite_id(activity_id)
        or encode_cursor(encoded_room_id, activity_id) != cursor
    ):
        raise InvalidCursor("invalid room timeline cursor")
    return activity_id


def project_authoritative_text(
    value: object,
    *,
    redacted: str = REDACTED_DETAIL,
    max_chars: int = BODY_LIMIT,
    reject_structured: bool = True,
) -> tuple[str, bool]:
    """Return bounded authoritative prose or one fixed non-oracular redaction."""
    text = str(value or "")
    if text and room_commands.unsafe_room_payload_reason(
        text, reject_structured=reject_structured
    ):
        return redacted, False
    return text[:max_chars], len(text) > max_chars


def public_room(room: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the stable room fields; never expose the internal scope key."""
    projected = {
        key: room.get(key)
        for key in (
            "id",
            "project_id",
            "slug",
            "room_type",
            "title",
            "purpose",
            "visibility",
            "issue_id",
            "agent_id",
            "created_by",
            "created_at",
            "updated_at",
            "archived_at",
            "archived",
            "is_detached",
            "link_state",
            "degraded_reason",
        )
    }
    projected["title"], _ = project_authoritative_text(
        projected.get("title"),
        redacted=REDACTED_TITLE,
        max_chars=rooms.MAX_TITLE_CHARS,
    )
    projected["purpose"], _ = project_authoritative_text(
        projected.get("purpose"), max_chars=rooms.MAX_PURPOSE_CHARS
    )
    return projected


def _bounded_limit(limit: int, *, ceiling: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1 or limit > ceiling:
        raise ValueError(f"limit must be between 1 and {ceiling}")
    return limit


def _domain_scope_sql(
    room: dict[str, Any], *, alias: str = "a"
) -> tuple[str, list[Any]]:
    """Activity predicate for the authoritative records projected by a room."""
    room_type = room["room_type"]
    project_id = int(room["project_id"])
    if room_type in {"project", "brief"}:
        return (
            f"(({alias}.target_kind = 'project' AND {alias}.target_id = ?) OR "
            f"({alias}.target_kind = 'issue' AND EXISTS ("
            f"SELECT 1 FROM issues scope_issue WHERE scope_issue.id = {alias}.target_id "
            "AND scope_issue.project_id = ?)))",
            [project_id, project_id],
        )
    if room_type == "work_item":
        return (
            f"({alias}.target_kind = 'issue' AND {alias}.target_id = ?)",
            [int(room["issue_id"])],
        )
    if room_type == "agent":
        return (
            f"({alias}.actor_id = ? AND (({alias}.target_kind = 'project' "
            f"AND {alias}.target_id = ?) OR ({alias}.target_kind = 'issue' "
            f"AND EXISTS (SELECT 1 FROM issues scope_issue "
            f"WHERE scope_issue.id = {alias}.target_id "
            "AND scope_issue.project_id = ?))))",
            [int(room["agent_id"]), project_id, project_id],
        )
    # Storage CHECK constraints own the vocabulary; fail closed if a corrupted row
    # still reaches this projection.
    return "0 = 1", []


def _timeline_where(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    *,
    before_id: int | None = None,
) -> tuple[str, list[Any]]:
    """One SQL predicate that gates domain rows before ordering and limiting."""
    domain_scope, domain_params = _domain_scope_sql(room)
    visible, visible_params = access.event_visibility_clause(conn, actor, alias="a")
    # Apply the generic target visibility predicate to the whole union. It knows
    # target_kind='room' and verifies the immutable historical project envelope,
    # so current room access cannot resurrect an event after a linked-issue move.
    if room["room_type"] == "brief":
        room_event_scope = (
            "EXISTS (SELECT 1 FROM rooms projected_room "
            "WHERE projected_room.id = re.room_id "
            "AND projected_room.project_id = ? "
            "AND projected_room.project_scope_key = ?)"
        )
        room_event_params: list[Any] = [
            room["project_id"],
            room["project_scope_key"],
        ]
    else:
        room_event_scope = "re.room_id = ?"
        room_event_params = [room["id"]]
    room_or_domain = (
        f"(({room_event_scope}) OR (re.activity_id IS NULL AND ({domain_scope})))"
    )
    clauses = [room_or_domain]
    params: list[Any] = [*room_event_params, *domain_params]
    if visible:
        clauses.append(f"({visible})")
        params.extend(visible_params)
    if before_id is not None:
        clauses.append("a.id < ?")
        params.append(before_id)
    return " AND ".join(clauses), params


def _timeline_rows(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    *,
    before_id: int | None,
    limit: int,
    actor_id: int | None = None,
    native_only: bool = False,
    current_room_events_only: bool = False,
) -> list[dict[str, Any]]:
    where, params = _timeline_where(conn, room, actor, before_id=before_id)
    if native_only:
        where += " AND a.imported_at IS NULL"
    if current_room_events_only:
        where += " AND successor.activity_id IS NULL"
    if actor_id is not None:
        where += " AND a.actor_id = ?"
        params.append(actor_id)
    params.append(limit)
    rows = conn.execute(
        "SELECT a.id AS activity_id, a.actor_id, u.name AS actor_name, "
        "u.is_agent AS actor_is_agent, a.verb, a.target_kind, a.target_id, "
        "a.detail, a.created_at, a.run_id, a.parent_run_id, "
        "a.forked_from_event_id, a.imported_at, re.room_id AS authored_room_id, "
        "re.event_kind, re.reference_kind, re.reference_id, re.content_sha256, "
        "re.supersedes_event_id, successor.activity_id AS successor_event_id "
        "FROM activity a "
        "JOIN users u ON u.id = a.actor_id "
        "LEFT JOIN room_events re ON re.activity_id = a.id "
        "LEFT JOIN room_events successor "
        "ON successor.supersedes_event_id = a.id "
        f"WHERE {where} ORDER BY a.id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _classification(row: dict[str, Any]) -> str:
    if row.get("imported_at") is not None:
        return "imported"
    event_kind = row.get("event_kind")
    if event_kind == "system_notice":
        return "system"
    if event_kind == "evidence":
        return "evidence"
    verb = str(row.get("verb") or "")
    target_kind = str(row.get("target_kind") or "")
    if verb.startswith("approval_") or target_kind == "approval":
        return "approval"
    if (
        target_kind in {"attachment", "artifact"}
        or "evidence" in verb
        or verb.startswith("forge_")
    ):
        return "evidence"
    if verb.startswith(("automation_", "webhook_", "schedule_")):
        return "system"
    return "agent" if bool(row.get("actor_is_agent")) else "human"


def _projected_body(row: dict[str, Any]) -> tuple[str, bool]:
    return project_authoritative_text(
        row.get("detail"),
        redacted=REDACTED_DETAIL,
        max_chars=BODY_LIMIT,
    )


def _room_event_reference(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    row: dict[str, Any],
) -> dict[str, Any] | None:
    kind = row.get("reference_kind")
    reference_id = row.get("reference_id")
    if kind is None or reference_id is None:
        return None
    normalized = str(reference_id)
    resolved = _resolve_reference(conn, room, actor, str(kind), normalized)
    if resolved is None:
        return {
            "kind": str(kind),
            "id": None,
            "available": False,
            "unavailable_reason": _UNAVAILABLE,
            "title": None,
            "receipt": None,
        }
    title, _ = project_authoritative_text(
        resolved["title"],
        redacted=REDACTED_TITLE,
        max_chars=rooms.MAX_TITLE_CHARS,
    )
    return {
        "kind": str(kind),
        "id": normalized,
        "available": True,
        "unavailable_reason": None,
        "title": title,
        "receipt": resolved["receipt"],
    }


def _integer_reference(value: str) -> int | None:
    if not value.isascii() or not value.isdecimal() or value.startswith("0"):
        return None
    parsed = int(value)
    return parsed if rooms.is_sqlite_id(parsed) else None


def _safe_native_complete_run_ids(
    conn: sqlite3.Connection,
    actor: dict[str, Any] | None,
    run_ids: set[str],
) -> set[str]:
    """Return only native runs whose entire event set is visible to this reader."""
    safe: set[str] = set()
    for run_id in sorted(run_ids):
        summary = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN imported_at IS NOT NULL THEN 1 ELSE 0 END) AS imported "
            "FROM activity WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if (
            summary is None
            or int(summary["total"] or 0) == 0
            or int(summary["imported"] or 0) > 0
        ):
            continue
        if activity.can_see_complete_run(conn, run_id, actor):
            safe.add(run_id)
    return safe


def _resolve_reference(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    kind: str,
    reference_id: str,
) -> dict[str, str] | None:
    """Resolve controlled references without turning a room into an ACL bypass."""
    numeric = _integer_reference(reference_id)
    if kind == "issue" and numeric is not None:
        if not access.can_see_issue(conn, actor, numeric):
            return None
        record = conn.execute(
            "SELECT i.title, i.project_seq, p.key AS project_key FROM issues i "
            "LEFT JOIN projects p ON p.id = i.project_id WHERE i.id = ?",
            (numeric,),
        ).fetchone()
        if record is None:
            return None
        key = (
            f"{record['project_key']}-{record['project_seq']}"
            if record["project_key"] and record["project_seq"] is not None
            else f"#{numeric}"
        )
        return {"title": f"{key}: {record['title']}", "receipt": f"/issues/{numeric}"}
    if kind == "page" and numeric is not None:
        if not access.can_see_page(conn, actor, numeric):
            return None
        record = conn.execute(
            "SELECT title FROM pages WHERE id = ?", (numeric,)
        ).fetchone()
        if record is None:
            return None
        return {"title": record["title"], "receipt": f"/pages/{numeric}"}
    if kind == "activity" and numeric is not None:
        event = activity.get_visible_activity(conn, numeric, actor)
        if event is None:
            return None
        title = event["verb"]
        return {
            "title": f"Activity {numeric}: {title}",
            "receipt": f"/events?after={numeric - 1}",
        }
    if kind == "approval" and numeric is not None:
        if actor is None or actor.get("role") != "admin":
            return None
        request = conn.execute(
            "SELECT action_kind, target_kind, target_id FROM approval_requests "
            "WHERE id = ?",
            (numeric,),
        ).fetchone()
        if request is None or not _target_is_visible(
            conn, room, actor, request["target_kind"], request["target_id"]
        ):
            return None
        return {
            "title": f"Approval {numeric}: {request['action_kind']}",
            "receipt": f"/approvals/{numeric}",
        }
    if kind == "handoff" and numeric is not None:
        handoff = conn.execute(
            "SELECT issue_id FROM issue_claim_handoffs WHERE id = ?", (numeric,)
        ).fetchone()
        if handoff is None or not access.can_see_issue(
            conn, actor, handoff["issue_id"]
        ):
            return None
        return {
            "title": f"Claim handoff {numeric}",
            "receipt": f"/issues/{handoff['issue_id']}/work-context",
        }
    if kind == "dispatch" and numeric is not None:
        dispatch = conn.execute(
            "SELECT work_item_id FROM icarus_dispatches WHERE id = ?", (numeric,)
        ).fetchone()
        if dispatch is None or not access.can_see_issue(
            conn, actor, dispatch["work_item_id"]
        ):
            return None
        return {"title": f"Dispatch {numeric}", "receipt": f"/dispatches/{numeric}"}
    if kind == "attachment" and numeric is not None:
        attachment = conn.execute(
            "SELECT filename, target_kind, target_id FROM attachments WHERE id = ?",
            (numeric,),
        ).fetchone()
        if attachment is None or not _target_is_visible(
            conn, room, actor, attachment["target_kind"], attachment["target_id"]
        ):
            return None
        return {"title": attachment["filename"], "receipt": f"/attachments/{numeric}"}
    if kind == "run":
        where, params = _timeline_where(conn, room, actor)
        where += " AND a.run_id = ? AND a.imported_at IS NULL"
        params.append(reference_id)
        exists = conn.execute(
            "SELECT 1 FROM activity a LEFT JOIN room_events re "
            f"ON re.activity_id = a.id WHERE {where} LIMIT 1",
            params,
        ).fetchone()
        safe_runs = _safe_native_complete_run_ids(conn, actor, {reference_id})
        if exists is None or reference_id not in safe_runs:
            return None
        return {
            "title": f"Run {reference_id}",
            "receipt": f"/activity/runs/{quote(reference_id, safe='')}/lineage",
        }
    return None


def _target_is_visible(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    target_kind: str,
    target_id: int,
) -> bool:
    if target_kind == "issue":
        return access.can_see_issue(conn, actor, int(target_id))
    if target_kind == "page":
        return access.can_see_page(conn, actor, int(target_id))
    if target_kind == "project":
        return access.can_see_project(conn, actor, int(target_id))
    if target_kind == "room":
        return int(target_id) == int(room["id"])
    return False


def _timeline_item(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    row: dict[str, Any],
    *,
    safe_run_ids: set[str] | None = None,
) -> dict[str, Any]:
    body, body_truncated = _projected_body(row)
    actor_name, _ = project_authoritative_text(
        row.get("actor_name"),
        redacted=REDACTED_TITLE,
        max_chars=rooms.MAX_TITLE_CHARS,
    )
    run_id = str(row["run_id"]) if row.get("run_id") is not None else None
    if safe_run_ids is None:
        safe_run_ids = _safe_native_complete_run_ids(
            conn, actor, {run_id} if run_id is not None else set()
        )
    run_is_safe = run_id is not None and run_id in safe_run_ids
    raw_successor_event_id = row.get("successor_event_id")
    if raw_successor_event_id is None:
        raw_successor_event_id = row.get("superseded_by_event_id")
    successor_event_id = raw_successor_event_id
    if (
        raw_successor_event_id is not None
        and activity.get_visible_activity(conn, int(raw_successor_event_id), actor)
        is None
    ):
        successor_event_id = None
    return {
        "activity_id": row["activity_id"],
        "classification": _classification(row),
        "event_kind": row.get("event_kind"),
        "actor": {
            "id": row["actor_id"],
            "name": actor_name,
            "is_agent": bool(row["actor_is_agent"]),
        },
        "verb": row["verb"],
        "body": body,
        "body_truncated": body_truncated,
        "created_at": row["created_at"],
        "target": {"kind": row["target_kind"], "id": row["target_id"]},
        "run_id": run_id,
        "run_receipt": (
            f"/activity/runs/{quote(run_id, safe='')}/lineage"
            if run_is_safe and run_id is not None
            else None
        ),
        "run_receipt_unavailable_reason": (
            None if run_id is None or run_is_safe else INCOMPLETE_RUN
        ),
        "parent_run_id": row.get("parent_run_id"),
        "forked_from_event_id": row.get("forked_from_event_id"),
        "imported_at": row.get("imported_at"),
        "reference": _room_event_reference(conn, room, actor, row),
        "supersedes_event_id": row.get("supersedes_event_id"),
        "content_sha256": row.get("content_sha256"),
        "successor_event_id": successor_event_id,
        "is_current": raw_successor_event_id is None,
    }


def list_timeline(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    actor: dict[str, Any] | None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    native_only: bool = False,
    current_room_events_only: bool = False,
) -> dict[str, Any] | None:
    """Return one newest-first, activity-id-keyset room timeline page.

    ``None`` deliberately conflates a missing room with a room hidden from the
    actor.  Authorization is resolved before decoding/querying page contents.
    """
    room = rooms.get_visible_room(
        conn, actor=actor, room_id=room_id, include_archived=True
    )
    if room is None:
        return None
    bounded = _bounded_limit(limit, ceiling=MAX_LIMIT)
    before_id = decode_cursor(cursor, int(room["id"]))
    rows = _timeline_rows(
        conn,
        room,
        actor,
        before_id=before_id,
        limit=bounded + 1,
        native_only=native_only,
        current_room_events_only=current_room_events_only,
    )
    has_more = len(rows) > bounded
    selected = rows[:bounded]
    safe_run_ids = _safe_native_complete_run_ids(
        conn,
        actor,
        {str(row["run_id"]) for row in selected if row.get("run_id") is not None},
    )
    next_cursor = (
        encode_cursor(int(room["id"]), int(selected[-1]["activity_id"]))
        if has_more and selected
        else None
    )
    return {
        "room": public_room(room),
        "items": [
            _timeline_item(conn, room, actor, row, safe_run_ids=safe_run_ids)
            for row in selected
        ],
        "page": {
            "limit": bounded,
            "next_cursor": next_cursor,
            "has_more": has_more,
        },
    }


get_timeline = list_timeline


def _candidate_agents(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Select and count visible teammates without materializing the full roster."""
    project_id = int(room["project_id"])
    linked_agent_id = room.get("agent_id")
    timeline_where, timeline_params = _timeline_where(conn, room, actor)
    rows = conn.execute(
        "WITH candidate_ids(agent_id) AS ("
        "SELECT ? WHERE ? IS NOT NULL "
        "UNION SELECT p.created_by FROM projects p JOIN users creator "
        "ON creator.id = p.created_by WHERE p.id = ? AND creator.is_agent = 1 "
        "UNION SELECT pm.user_id FROM project_members pm JOIN users member "
        "ON member.id = pm.user_id WHERE pm.project_id = ? AND member.is_agent = 1 "
        "UNION SELECT i.assignee_id FROM issues i JOIN users assignee "
        "ON assignee.id = i.assignee_id WHERE i.project_id = ? "
        "AND assignee.is_agent = 1 "
        "UNION SELECT ic.user_id FROM issue_contributors ic JOIN issues i "
        "ON i.id = ic.issue_id JOIN users contributor ON contributor.id = ic.user_id "
        "WHERE i.project_id = ? AND contributor.is_agent = 1 "
        "UNION SELECT a.actor_id FROM activity a "
        "LEFT JOIN room_events re ON re.activity_id = a.id "
        f"WHERE {timeline_where} AND a.imported_at IS NULL"
        ") SELECT u.id, u.name, u.role, u.is_agent, u.paused_at, "
        "COUNT(*) OVER () AS visible_total FROM candidate_ids candidate "
        "JOIN users u ON u.id = candidate.agent_id "
        "WHERE u.is_agent = 1 OR u.id = ? "
        "ORDER BY u.name COLLATE NOCASE, u.id LIMIT ?",
        [
            linked_agent_id,
            linked_agent_id,
            project_id,
            project_id,
            project_id,
            project_id,
            *timeline_params,
            linked_agent_id,
            limit,
        ],
    ).fetchall()
    total = int(rows[0]["visible_total"]) if rows else 0
    users_out = []
    for row in rows:
        user = dict(row)
        user.pop("visible_total", None)
        users_out.append(user)
    return users_out, total


def _token_posture(
    conn: sqlite3.Connection,
    agent_id: int,
    *,
    include_details: bool,
) -> tuple[str, dict[str, Any]]:
    summary = conn.execute(
        "SELECT SUM(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END) AS live, "
        "SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END) AS revoked, "
        "MAX(last_used_at) AS last_used_at FROM api_tokens WHERE user_id = ?",
        (agent_id,),
    ).fetchone()
    live_count = int(summary["live"] or 0)
    revoked_count = int(summary["revoked"] or 0)
    state = "enabled" if live_count else ("revoked" if revoked_count else "unavailable")
    effective_scopes: set[str] = set()
    scope_scan_clipped = False
    if include_details:
        scope_rows = conn.execute(
            "SELECT scopes FROM api_tokens WHERE user_id = ? "
            "AND revoked_at IS NULL ORDER BY id DESC LIMIT ?",
            (agent_id, MAX_CAPABILITY_TOKEN_SCAN + 1),
        ).fetchall()
        scope_scan_clipped = len(scope_rows) > MAX_CAPABILITY_TOKEN_SCAN
        for row in scope_rows[:MAX_CAPABILITY_TOKEN_SCAN]:
            try:
                effective_scopes.update(tokens.parse_scopes(row["scopes"]))
            except ValueError:
                continue
    return state, {
        "live_token_count": live_count,
        "revoked_token_count": revoked_count,
        "effective_scopes": sorted(effective_scopes),
        "last_used_at": summary["last_used_at"],
        "scope_scan_clipped": scope_scan_clipped,
    }


def _claims_for_agent(
    conn: sqlite3.Connection, room: dict[str, Any], agent_id: int
) -> dict[str, Any]:
    clauses = [
        "l.holder_id = ?",
        "l.expires_at > datetime('now')",
        "i.project_id = ?",
    ]
    params: list[Any] = [agent_id, int(room["project_id"])]
    if room["room_type"] == "work_item":
        clauses.append("i.id = ?")
        params.append(int(room["issue_id"]))
    from_where = (
        "FROM issue_leases l JOIN issues i ON i.id = l.issue_id "
        "JOIN projects p ON p.id = i.project_id "
        f"WHERE {' AND '.join(clauses)}"
    )
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n {from_where}", params).fetchone()["n"]
    )
    rows = conn.execute(
        "SELECT i.id AS issue_id, i.title, i.priority, i.status, i.project_seq, "
        f"p.key AS project_key, l.claimed_at, l.expires_at, l.generation {from_where} "
        "ORDER BY l.expires_at, i.id LIMIT 5",
        params,
    ).fetchall()
    mapped = [
        {
            "issue_id": row["issue_id"],
            "key": f"{row['project_key']}-{row['project_seq']}",
            "title": project_authoritative_text(
                row["title"],
                redacted=REDACTED_TITLE,
                max_chars=rooms.MAX_TITLE_CHARS,
            )[0],
            "priority": row["priority"],
            "status": row["status"],
            "claimed_at": row["claimed_at"],
            "expires_at": row["expires_at"],
            "generation": row["generation"],
            "receipt": f"/issues/{row['issue_id']}",
            "semantics": "recorded_claim_not_process_liveness",
        }
        for row in rows[:5]
    ]
    return {
        "items": mapped,
        "visible_total": total,
        "clipped": total > len(mapped),
    }


def _latest_check_in(
    conn: sqlite3.Connection,
    agent_id: int,
    contributions: dict[str, Any],
    safe_run_ids: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Newest report whose run is already visible in this room projection."""
    visible_run_ids = {
        str(item["run_id"])
        for item in contributions["items"]
        if item.get("run_id") is not None
    }
    checkins = [
        checkin
        for run_id in visible_run_ids
        if (
            checkin := agent_run_checkins.get_checkin(
                conn, agent_id=agent_id, run_id=run_id
            )
        )
        is not None
    ]
    if not checkins:
        reason = (
            "bounded_visible_contributions"
            if contributions["clipped"]
            else "no_room_visible_report"
        )
        return None, reason
    checkin = sorted(
        checkins,
        key=lambda item: (str(item["last_seen_at"]), str(item["run_id"])),
        reverse=True,
    )[0]
    run_id = str(checkin["run_id"])
    run_is_safe = run_id in safe_run_ids
    return (
        {
            "run_id": checkin["run_id"],
            "first_seen_at": checkin["first_seen_at"],
            "last_seen_at": checkin["last_seen_at"],
            "reporting_state": checkin["reporting_state"],
            "age_seconds": checkin["age_seconds"],
            "semantics": "cooperative_report_not_process_liveness",
            "receipt": (
                f"/activity/runs/{quote(run_id, safe='')}/lineage"
                if run_is_safe
                else None
            ),
        },
        None if run_is_safe else INCOMPLETE_RUN,
    )


def _recent_contributions(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    agent_id: int,
) -> dict[str, Any]:
    where, params = _timeline_where(conn, room, actor)
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM activity a "
            "LEFT JOIN room_events re ON re.activity_id = a.id "
            f"WHERE {where} AND a.actor_id = ? AND a.imported_at IS NULL",
            [*params, agent_id],
        ).fetchone()["n"]
    )
    rows = _timeline_rows(
        conn,
        room,
        actor,
        before_id=None,
        limit=RECENT_CONTRIBUTION_LIMIT,
        actor_id=agent_id,
        native_only=True,
    )
    return {
        "items": [
            {
                "activity_id": row["activity_id"],
                "verb": row["verb"],
                "target_kind": row["target_kind"],
                "target_id": row["target_id"],
                "created_at": row["created_at"],
                "run_id": row["run_id"],
            }
            for row in rows
        ],
        "visible_total": total,
        "clipped": total > len(rows),
    }


def _visible_lineage(
    contributions: dict[str, Any], safe_run_ids: set[str]
) -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    unsafe_run_seen = False
    for event in contributions["items"]:
        run_id = event.get("run_id")
        if run_id is None or run_id in runs:
            continue
        if str(run_id) not in safe_run_ids:
            unsafe_run_seen = True
            continue
        runs[run_id] = {
            "run_id": run_id,
            "last_activity_id": event["activity_id"],
            "last_activity_at": event["created_at"],
            "receipt": f"/activity/runs/{quote(str(run_id), safe='')}/lineage",
        }
    clipped = bool(contributions["clipped"]) or unsafe_run_seen
    return {
        "items": list(runs.values()),
        "clipped": clipped,
        "unavailable_reason": (
            INCOMPLETE_RUN
            if unsafe_run_seen
            else ("bounded_to_recent_visible_contributions" if clipped else None)
        ),
    }


def _agent_item(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    user: dict[str, Any],
) -> dict[str, Any]:
    agent_id = int(user["id"])
    agent_name, _ = project_authoritative_text(
        user.get("name"),
        redacted=REDACTED_TITLE,
        max_chars=rooms.MAX_TITLE_CHARS,
    )
    is_agent = bool(user["is_agent"])
    admin = actor is not None and actor.get("role") == "admin"
    credential_state, posture = _token_posture(
        conn,
        agent_id,
        include_details=admin and is_agent,
    )
    if not is_agent:
        account_state = "unavailable"
    else:
        account_state = "paused" if user.get("paused_at") else credential_state
    contributions = _recent_contributions(conn, room, actor, agent_id)
    safe_run_ids = _safe_native_complete_run_ids(
        conn,
        actor,
        {
            str(item["run_id"])
            for item in contributions["items"]
            if item.get("run_id") is not None
        },
    )
    check_in, check_in_reason = _latest_check_in(
        conn, agent_id, contributions, safe_run_ids
    )
    claims = _claims_for_agent(conn, room, agent_id)
    capability_available = admin and is_agent
    if not is_agent:
        capability_reason = "identity_no_longer_agent"
    elif not admin:
        capability_reason = "admin_only"
    else:
        capability_reason = None
    return {
        "id": agent_id,
        "name": agent_name,
        "role": user["role"],
        "is_agent": is_agent,
        "account_state": account_state,
        "enabled": account_state == "enabled",
        "revoked": account_state == "revoked",
        "paused_at": user.get("paused_at"),
        "capability": {
            "status": "available" if capability_available else "unavailable",
            "token_scopes": (
                posture["effective_scopes"] if capability_available else None
            ),
            "credential_posture": posture if capability_available else None,
            "unavailable_reason": capability_reason,
        },
        "current_claims": claims,
        "latest_check_in": check_in,
        "latest_check_in_unavailable_reason": check_in_reason,
        "recent_contributions": contributions,
        "visible_lineage": _visible_lineage(contributions, safe_run_ids),
    }


def list_visible_agents(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    actor: dict[str, Any] | None,
    limit: int = DEFAULT_AGENT_LIMIT,
) -> dict[str, Any] | None:
    """Return a bounded teammate projection with admin-only credential detail."""
    room = rooms.get_visible_room(
        conn, actor=actor, room_id=room_id, include_archived=True
    )
    if room is None:
        return None
    bounded = _bounded_limit(limit, ceiling=MAX_AGENT_LIMIT)
    selected, total = _candidate_agents(
        conn,
        room,
        actor,
        limit=bounded,
    )
    return {
        "items": [_agent_item(conn, room, actor, user) for user in selected],
        "visible_total": total,
        "clipped": total > len(selected),
    }


visible_agents = list_visible_agents
