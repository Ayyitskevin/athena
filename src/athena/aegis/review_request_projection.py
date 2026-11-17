"""Exact request facts borrowed by the desk, full context and review queue."""

from datetime import datetime
import sqlite3
from typing import Literal

from athena.aegis import leases
from athena.aegis.project_brief import _group, _rows
from athena.core import access

RequestLane = Literal["candidate", "review_issue", "desk"]
# Scope and output bounds are controlled by the owning read surface.
_LANES = {
    "candidate": ("r.candidate_id = ?", 5),
    "review_issue": ("r.review_issue_id = ?", 1),
    "desk": ("r.reviewer_id = ?", 20),
}


def _item(row: sqlite3.Row) -> dict:
    return {
        "request_id": row["request_id"],
        "candidate_id": row["candidate_id"],
        "review_issue_id": row["review_issue_id"],
        "reviewer_id": row["reviewer_id"],
        "status": row["status"],
        "reviewer_holds_active_lease": bool(row["held"]),
        "identity": {
            "kind": row["kind"],
            "source": row["source"],
            "revision": row["revision"],
        },
        "source_issue_id": row["source_issue_id"],
        "completion_event_id": row["completion_event_id"],
        "source_work_context_url": f"/issues/{row['source_issue_id']}/work-context",
    }


def visible_requests(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    lane: RequestLane,
    scope_id: int,
    now: datetime | None = None,
) -> dict | None:
    """Read under the caller's snapshot; zero visible items omits the section.

    A hidden later fulfillment must be indistinguishable from none. Resolve the
    canonical event before testing visibility; the same visible fact controls
    status, the desk predicate, and the count before the output bound.
    """
    root, limit = _LANES[lane]
    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    desk_filter = (
        "WHERE status IN ('requested', 'picked_up') OR (status = 'superseded' AND held)"
        if lane == "desk"
        else ""
    )
    base = f"""
        FROM (WITH visible_events AS NOT MATERIALIZED (
            SELECT a.id FROM activity a
            WHERE a.imported_at IS NULL AND a.visibility_restricted = 0
            {f"AND ({gate})" if gate else ""}
        ), facts AS (
            SELECT r.id AS request_id, r.candidate_id, r.review_issue_id, r.reviewer_id,
                   c.issue_id AS source_issue_id, c.completion_event_id, c.kind, c.source,
                   c.revision, latest.id AS latest_id, latest.completion_event_id AS latest_event,
                   EXISTS (
                       SELECT 1 FROM issue_leases l WHERE l.issue_id = r.review_issue_id
                         AND l.holder_id = r.reviewer_id AND l.expires_at > ?
                   ) AS held,
                   (SELECT max(v.completion_event_id) FROM issue_candidate_reviews v
                    JOIN activity completion ON completion.id = v.completion_event_id
                    WHERE v.candidate_id = r.candidate_id AND v.review_issue_id = r.review_issue_id
                      AND v.completion_event_id > r.request_event_id
                      AND completion.actor_id = r.reviewer_id) AS fulfillment_event
            FROM issue_review_requests r
            JOIN issue_review_candidates c ON c.id = r.candidate_id
            JOIN issue_review_candidates latest ON latest.id = (
                SELECT max(id) FROM issue_review_candidates WHERE issue_id = c.issue_id
            )
            WHERE {root} AND r.id = (
                SELECT max(id) FROM issue_review_requests WHERE review_issue_id = r.review_issue_id
            )
            AND EXISTS (SELECT 1 FROM visible_events WHERE id = r.request_event_id)
            AND EXISTS (SELECT 1 FROM visible_events WHERE id = c.completion_event_id)
        ), states AS (
            SELECT facts.*,
                   CASE
                       WHEN EXISTS (SELECT 1 FROM visible_events WHERE id = fulfillment_event)
                         THEN 'fulfilled'
                       WHEN candidate_id <> latest_id THEN 'superseded'
                       WHEN held THEN 'picked_up'
                       ELSE 'requested'
                   END AS status
            FROM facts
        ), visible_requests AS (
            SELECT * FROM states
            WHERE status <> 'superseded' OR EXISTS (SELECT 1 FROM visible_events WHERE id = latest_event)
        )
        SELECT * FROM visible_requests {desk_filter}) requests
        """
    total, rows = _rows(
        conn,
        columns="request_id, candidate_id, review_issue_id, reviewer_id, status, held, "
        "kind, source, revision, source_issue_id, completion_event_id",
        base=base,
        params=[*params, leases.clock_stamp(now), scope_id],
        order="request_id DESC",
        limit=limit,
    )
    if not rows:
        return None
    return _group(total, [_item(row) for row in rows], limit)
