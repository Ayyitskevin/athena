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
   or admin), so the queue never offers work a claim would refuse;
6. it is **not awaiting an answer** (decision record
   ``docs/decisions/2026-11-22-ready-rule-awaiting-decision.md``). Take the
   issue's newest native settlement: a ``claim_completed`` or ``claim_yielded``
   event. If that settlement is a ``needs_input`` yield, the issue is not ready
   for ANY account; if it is a completion, it is not ready for the account that
   completed it. Either lifts once an *answer* is newer than the settlement: a
   comment, status change, assignment change or claim on the issue, native, by
   an account other than the settler and not the Automation actor. Yields for
   ``blocked`` or ``capacity`` exclude nobody. Direct ``claim_issue`` ignores
   this rule (and its claim is itself an answer). A pending review request
   also answers a completion, for the account it names and nobody else
   (decision record ``docs/decisions/2026-11-22-review-request-answers.md``):
   the issue's newest request names the completer, was made by another account
   after the completion, is native, and asks about the source's current
   candidate, with both of its events visible to the completer. It lifts no
   ``needs_input`` hold and changes no blocker.

Order: delegated to me first, then priority (urgent first), then age (oldest
first), then id. One definition serves three surfaces: the ``is:ready`` query
atom, the desk's bounded ``work.ready`` lane (and ``GET /desk/ready``), and the
MCP ``claim_next_ready`` tool, which claims through the existing claim command.
The issues rule 6 alone keeps out are listed by :func:`build_not_offered`, so
the desk can say what is waiting and why.

Ready is a read at one instant, not a reservation: two actors can see the same
item, and the claim command's own interlock decides who gets it.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena.aegis import claim_handoffs, dependencies, issues, leases, statuses
from athena.aegis.issue_commands import AUTOMATION_ACTOR_EMAIL
from athena.core import access, db, identity
from athena.core.ids import MAX_SQLITE_INTEGER

SCHEMA = "athena.ready_queue.v1"

#: The desk lane's size, and the REST read's default.
READY_LIMIT = 20
MAX_READY_LIMIT = 50

_READY_CATEGORIES = ("todo", "doing")

#: The acts that settle a possession (rule 6). Only the newest one counts.
SETTLEMENT_VERBS = ("claim_completed", "claim_yielded")
#: The acts by another account that answer a settlement (rule 6). Comment
#: edits, labels, links and lease renewals are deliberately not answers.
ANSWER_VERBS = ("commented", "changed_status", "assigned", "unassigned", "claimed")

#: Why rule 6 keeps an issue out of an actor's ready lane.
AWAITING_ANSWER = "awaiting_answer"  # a needs_input yield nobody else has answered
COMPLETED_BY_YOU = "completed_by_you"  # your completion, with nothing newer by others

_SETTLEMENT_LIST = ", ".join(f"'{verb}'" for verb in SETTLEMENT_VERBS)
_ANSWER_LIST = ", ".join(f"'{verb}'" for verb in ANSWER_VERBS)


class ReadyQueueError(ValueError):
    """The ready rule cannot be evaluated for this caller (no actor)."""


def _now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def awaiting_answer_sql(conn: sqlite3.Connection, actor: dict) -> tuple[str, list]:
    """Rule 6's exclusion, true when the issue (alias ``i``) awaits an answer
    that ``actor`` may not supply by picking the work up again.

    One correlated read per candidate, both over ``idx_activity_target``
    (target_kind, target_id, id): the newest native settlement by walking the
    issue's own events newest first, then any answer after it by a range scan
    above that id. Imported rows never settle and never answer (``imported_at
    IS NULL`` on both reads); the Automation actor is recognised exactly as the
    commands recognise it (its fixed email on an agent account) and never
    answers. Ordering is by activity id, never by timestamp, so two events in
    the same second cannot tie.

    A completion is also answered by a pending review request naming the
    completer (D16), read from ``issue_review_requests`` rather than as an
    answer verb: the ``review_requested`` event does not carry whom it names,
    and an answer verb would lift a ``needs_input`` hold and answer for every
    account. The request is the issue's newest, names the completer, its event
    is native, newer than the completion and by another account, and it asks
    about the source issue's current candidate; its request and candidate
    events pass the completer's event visibility, as the desk's
    ``asks.review_requests`` lane requires, so a request the completer cannot
    see answers nothing."""
    request_gate, request_params = access.event_visibility_clause(
        conn, actor, alias="rqe"
    )
    candidate_gate, candidate_params = access.event_visibility_clause(
        conn, actor, alias="rqk"
    )
    sql = (
        "EXISTS (SELECT 1 FROM activity rqw WHERE rqw.id = ("
        "SELECT rqs.id FROM activity rqs "
        "WHERE rqs.target_kind = 'issue' AND rqs.target_id = i.id "
        f"AND rqs.verb IN ({_SETTLEMENT_LIST}) AND rqs.imported_at IS NULL "
        "ORDER BY rqs.id DESC LIMIT 1) "
        "AND CASE WHEN rqw.verb = 'claim_completed' THEN rqw.actor_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM issue_review_requests rqr "
        "JOIN activity rqe ON rqe.id = rqr.request_event_id "
        "JOIN issue_review_candidates rqc ON rqc.id = rqr.candidate_id "
        "JOIN activity rqk ON rqk.id = rqc.completion_event_id "
        "WHERE rqr.review_issue_id = i.id AND rqr.id = (SELECT MAX(rqm.id) "
        "FROM issue_review_requests rqm WHERE rqm.review_issue_id = i.id) "
        "AND rqr.reviewer_id = rqw.actor_id AND rqr.request_event_id > rqw.id "
        "AND rqe.imported_at IS NULL AND rqe.visibility_restricted = 0 "
        "AND rqe.actor_id != rqw.actor_id "
        "AND rqk.imported_at IS NULL AND rqk.visibility_restricted = 0 "
        "AND rqc.id = (SELECT MAX(rql.id) FROM issue_review_candidates rql "
        "WHERE rql.issue_id = rqc.issue_id)"
        f"{f' AND ({request_gate})' if request_gate else ''}"
        f"{f' AND ({candidate_gate})' if candidate_gate else ''}) "
        "ELSE EXISTS (SELECT 1 FROM issue_claim_handoffs rqh "
        "WHERE rqh.yield_event_id = rqw.id AND rqh.reason = 'needs_input') END "
        "AND NOT EXISTS (SELECT 1 FROM activity rqa "
        "WHERE rqa.target_kind = 'issue' AND rqa.target_id = i.id "
        "AND rqa.id > rqw.id AND rqa.imported_at IS NULL "
        f"AND rqa.verb IN ({_ANSWER_LIST}) "
        "AND rqa.actor_id != rqw.actor_id "
        "AND rqa.actor_id NOT IN (SELECT rqu.id FROM users rqu "
        "WHERE rqu.email = ? AND rqu.is_agent = 1)))"
    )
    return sql, [
        int(actor["id"]),
        *request_params,
        *candidate_params,
        AUTOMATION_ACTOR_EMAIL,
    ]


def _claimable_clauses(
    actor: dict, *, now: datetime, category_expr: str
) -> tuple[list[str], list]:
    """Rules 2 to 5, each a clause over issue alias ``i``, with their params in
    clause order."""
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
    return clauses, params


def ready_clause(
    conn: sqlite3.Connection,
    actor: dict | None,
    *,
    now: datetime,
    category_expr: str,
) -> tuple[str, list]:
    """Rules 2 to 6 as one SQL clause over issue alias ``i``.

    ``category_expr`` is the caller's status-category expression (each caller
    owns its own ``project_statuses`` join and alias). Visibility (rule 1) and the
    archived default are the caller's, as in every list. Raises
    :class:`ReadyQueueError` for no actor: "ready for me" needs a me."""
    if actor is None:
        raise ReadyQueueError("'is:ready' needs an authenticated caller")
    clauses, params = _claimable_clauses(actor, now=now, category_expr=category_expr)
    # Last, so the cheap column and lease tests discard most rows before the
    # correlated trail reads run.
    awaiting, awaiting_params = awaiting_answer_sql(conn, actor)
    clauses.append(f"NOT {awaiting}")
    params.extend(awaiting_params)
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
    project_id: int | None = None,
) -> dict:
    """The actor's ready lane: up to ``limit`` items in claim order, with the total
    behind them, so a clipped list never reads as the whole queue. Read-only."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_READY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_READY_LIMIT}")
    if project_id is not None and (
        isinstance(project_id, bool)
        or not isinstance(project_id, int)
        or not 1 <= project_id <= MAX_SQLITE_INTEGER
    ):
        raise ValueError("project_id must be a positive signed 64-bit integer")
    with db.transaction(conn):
        return _build_ready(
            conn, actor=actor, now=_now(now), limit=limit, project_id=project_id
        )


def _build_ready(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime,
    limit: int,
    project_id: int | None,
) -> dict:
    current = now
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
    if project_id is not None:
        base += " AND i.project_id = ?"
        params.append(project_id)
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
            "blocker that is not done (a blocker you cannot see still blocks), "
            "you are the assignee, a delegated contributor or an admin, and not "
            "awaiting an answer (the desk's not_offered says why). `total` counts "
            "the same rule before `limit` and is what claim_next_ready can serve. "
            "A read, not a reservation: the claim command decides between two "
            "claimants."
        ),
    }


def build_not_offered(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
    limit: int = READY_LIMIT,
) -> dict:
    """The issues rule 6 alone keeps out of this actor's ready lane: every other
    ready rule holds, visibility included, but the work awaits an answer. Each
    row says why. Read-only; the caller owns the snapshot (the desk's)."""
    current = _now(now)
    category = statuses.category_sql(
        status_column="i.status", category_column="rps.category"
    )
    clauses, params = _claimable_clauses(actor, now=current, category_expr=category)
    awaiting, awaiting_params = awaiting_answer_sql(conn, actor)
    clauses.append(awaiting)
    params.extend(awaiting_params)
    vis_sql, vis_params = _visibility(conn, actor)
    base = (
        "FROM issues i LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN project_statuses rps "
        "ON rps.project_id = i.project_id AND rps.name = i.status "
        f"WHERE i.archived_at IS NULL AND {' AND '.join(clauses)}{vis_sql}"
    )
    params.extend(vis_params)
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    rows = conn.execute(
        "SELECT i.id, i.title, i.status, i.priority, i.project_seq, "
        f"p.key AS project_key {base} ORDER BY i.id ASC LIMIT ?",
        [*params, limit],
    ).fetchall()
    settlements = _latest_settlements(conn, [int(row["id"]) for row in rows])
    me = int(actor["id"])
    items = []
    for row in rows:
        settled = settlements[int(row["id"])]
        mine = settled["actor_id"] == me
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        items.append(
            {
                "kind": "not_offered",
                "issue": {
                    "id": int(row["id"]),
                    "key": key,
                    "title": row["title"],
                    "status": row["status"],
                    "priority": row["priority"],
                },
                "reason": (
                    COMPLETED_BY_YOU
                    if settled["verb"] == "claim_completed"
                    else AWAITING_ANSWER
                ),
                "settled_by_you": mine,
                # Your own act's time only: another account's is not yours to read
                # here.
                "settled_at": settled["created_at"] if mine else None,
            }
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "clipped": total > len(items),
        "meaning": (
            "issues claim_next_ready will not offer you yet, though every other "
            "ready rule holds. awaiting_answer: the newest settlement is a "
            "needs_input yield, offered to no account; completed_by_you: you "
            "completed it, not offered back to you. Either lifts when another "
            "account (not Automation) comments, changes its status or assignment, "
            "or claims it; completed_by_you also when another account requests "
            "your review on it. claim_issue still works directly."
        ),
    }


def _latest_settlements(
    conn: sqlite3.Connection, issue_ids: list[int]
) -> dict[int, dict]:
    """The newest native settlement on each issue: verb, settler and time. The
    same choice rule 6 makes, read back for the rows it excluded."""
    if not issue_ids:
        return {}
    placeholders = ",".join("?" for _ in issue_ids)
    rows = conn.execute(
        "SELECT a.target_id, a.verb, a.actor_id, a.created_at FROM activity a "
        "WHERE a.id IN (SELECT MAX(s.id) FROM activity s "
        "WHERE s.target_kind = 'issue' "
        f"AND s.target_id IN ({placeholders}) "
        f"AND s.verb IN ({_SETTLEMENT_LIST}) AND s.imported_at IS NULL "
        "GROUP BY s.target_id) AND a.imported_at IS NULL",
        issue_ids,
    ).fetchall()
    return {
        int(row["target_id"]): {
            "verb": row["verb"],
            "actor_id": int(row["actor_id"]),
            "created_at": row["created_at"],
        }
        for row in rows
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
