"""The ready queue: what this actor could claim right now, in the order to take it.

An issue is READY for an actor when every one of these holds:

1. **visible** to the actor (the same project-visibility rule as every list);
2. its status **category is to-do or doing** (and it is not archived);
3. **no live lease** on it, by the lease clock — a lapsed lease does not count,
   and the row is shown as the previous holder, never cleared (K1);
4. **no open blocker**: no ``blocks`` edge from an issue that is not done, by the
   open-blocker read's own rule (:func:`dependencies.open_blocker_exists_sql`).
   A blocker the actor cannot see still blocks, and is not disclosed;
5. the actor **may claim it** under the claim command's policy
   (:func:`lease_commands.claimant_is_eligible`: assignee, delegated contributor,
   or admin), so the queue never offers work a claim would refuse.

Order: delegated to me first, then priority (urgent first), then age (oldest
first), then id. One definition serves three surfaces: the ``is:ready`` query
atom, the desk's bounded ``work.ready`` lane (and ``GET /desk/ready``), and the
MCP ``claim_next_ready`` tool, which claims through the existing claim command.

Ready is a read at one instant, not a reservation: two actors can see the same
item, and the claim command's own interlock decides who gets it.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena.aegis import claim_handoffs, dependencies, issues, leases, statuses
from athena.core import access, identity

SCHEMA = "athena.ready_queue.v1"

#: The desk lane's size, and the REST read's default.
READY_LIMIT = 20
MAX_READY_LIMIT = 50

_READY_CATEGORIES = ("todo", "doing")


class ReadyQueueError(ValueError):
    """The ready rule cannot be evaluated for this caller (no actor)."""


def _now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def ready_clause(
    conn: sqlite3.Connection,
    actor: dict | None,
    *,
    now: datetime,
    category_expr: str,
) -> tuple[str, list]:
    """Rules 2 to 5 as one SQL clause over issue alias ``i``.

    ``category_expr`` is the caller's status-category expression (each caller
    owns its own ``project_statuses`` join and alias). Visibility (rule 1) and the
    archived default are the caller's, as in every list. Raises
    :class:`ReadyQueueError` for no actor: "ready for me" needs a me."""
    if actor is None:
        raise ReadyQueueError("'is:ready' needs an authenticated caller")
    categories = ", ".join(f"'{c}'" for c in _READY_CATEGORIES)
    clauses = [
        f"{category_expr} IN ({categories})",
        f"NOT {leases.live_lease_sql('i.id')}",
        f"NOT {dependencies.open_blocker_exists_sql('i.id')}",
    ]
    params: list = [leases.clock_stamp(_now(now))]
    if not identity.is_admin(actor):
        clauses.append(
            "(i.assignee_id = ? OR EXISTS (SELECT 1 FROM issue_contributors qrc "
            "WHERE qrc.issue_id = i.id AND qrc.user_id = ?))"
        )
        params.extend([int(actor["id"]), int(actor["id"])])
    return "(" + " AND ".join(clauses) + ")", params


def _delegated_to_me_sql() -> str:
    return (
        "EXISTS (SELECT 1 FROM issue_contributors qrd "
        "WHERE qrd.issue_id = i.id AND qrd.user_id = ?)"
    )


def _visibility(conn: sqlite3.Connection, actor: dict) -> tuple[str, list[int]]:
    visible = access.visible_project_filter(conn, actor)
    if visible is None:
        return "", []
    if visible:
        placeholders = ",".join("?" for _ in visible)
        return (
            f" AND (i.project_id IS NULL OR i.project_id IN ({placeholders}))",
            sorted(visible),
        )
    return " AND i.project_id IS NULL", []


def build_ready(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
    limit: int = READY_LIMIT,
) -> dict:
    """The actor's ready lane: up to ``limit`` items in claim order, with the total
    behind them, so a clipped list never reads as the whole queue. Read-only."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_READY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_READY_LIMIT}")
    current = _now(now)
    category = statuses.category_sql(
        status_column="i.status", category_column="rps.category"
    )
    rule, rule_params = ready_clause(conn, actor, now=current, category_expr=category)
    vis_sql, vis_params = _visibility(conn, actor)
    base = (
        "FROM issues i LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN project_statuses rps "
        "ON rps.project_id = i.project_id AND rps.name = i.status "
        f"WHERE i.archived_at IS NULL AND {rule}{vis_sql}"
    )
    params = [*rule_params, *vis_params]
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    me = int(actor["id"])
    rows = conn.execute(
        "SELECT i.id, i.title, i.status, i.priority, i.created_at, i.project_id, "
        "i.project_seq, i.assignee_id, p.key AS project_key, "
        f"{_delegated_to_me_sql()} AS delegated_to_me "
        f"{base} "
        f"ORDER BY {_delegated_to_me_sql()} DESC, {issues.PRIORITY_RANK_SQL} DESC, "
        "i.created_at ASC, i.id ASC LIMIT ?",
        [me, *params, me, limit],
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    lapsed = _lapsed_leases(conn, ids)
    handoffs = claim_handoffs.open_handoffs_for_issues(conn, ids)
    items = []
    for row in rows:
        issue_id = int(row["id"])
        handoff = handoffs.get(issue_id)
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        items.append(
            {
                "issue": {
                    "id": issue_id,
                    "key": key,
                    "title": row["title"],
                    "status": row["status"],
                    "priority": row["priority"],
                    "created_at": row["created_at"],
                },
                "why": {
                    "delegated_to_you": bool(row["delegated_to_me"]),
                    "assigned_to_you": row["assignee_id"] == me,
                },
                # A lapsed possession: ready again, and who held it last. Shown,
                # never cleared — the next claim is the deliberate re-acquisition.
                "previous_lease": lapsed.get(issue_id),
                "open_handoff": None
                if handoff is None
                else {
                    "handoff_token": handoff["handoff_token"],
                    "reason": handoff["reason"],
                    "yielded_by": handoff["yielded"]["actor_name"],
                    "yielded_at": handoff["yielded"]["created_at"],
                    "next": "after claiming, resume it with resume_claim_handoff",
                },
            }
        )
    return {
        "schema": SCHEMA,
        "observed_at": leases.clock_stamp(current),
        "items": items,
        "total": total,
        "limit": limit,
        "clipped": total > len(items),
        "held_by_you": leases.count_active_leases_held_by(
            conn, holder_id=me, now=current
        ),
        "meaning": (
            "issues you could claim now, in claim order (delegated to you, then "
            "priority, then oldest): visible, to-do or doing, no live lease, no "
            "blocker that is not done (a blocker you cannot see still blocks), and "
            "you are the assignee, a delegated contributor or an admin. `total` "
            "counts the same rule before `limit`. A read, not a reservation: the "
            "claim command decides between two claimants."
        ),
    }


def _lapsed_leases(conn: sqlite3.Connection, issue_ids: list[int]) -> dict[int, dict]:
    """The expired lease row on each ready issue, if any. Every lease here is
    lapsed by construction (a live one makes the issue not ready)."""
    if not issue_ids:
        return {}
    placeholders = ",".join("?" for _ in issue_ids)
    rows = conn.execute(
        "SELECT l.issue_id, l.holder_id, u.name AS holder_name, l.expires_at "
        "FROM issue_leases l JOIN users u ON u.id = l.holder_id "
        f"WHERE l.issue_id IN ({placeholders})",
        issue_ids,
    ).fetchall()
    return {
        int(row["issue_id"]): {
            "holder_id": int(row["holder_id"]),
            "holder_name": row["holder_name"],
            "expired_at": row["expires_at"],
        }
        for row in rows
    }
