"""Completions that reported HOLD or BLOCKED and that nothing has answered.

A completion carries a verdict. ``PASS`` ends the matter; ``HOLD`` and
``BLOCKED`` say the work is not done and someone has to decide what happens to
it. That decision had no signal: the report was an event like any other, and a
conductor or operator who was not watching the feed at that moment saw an
issue with no lease and nothing asking for them (F15).

A report is open while the issue is neither done nor archived and nothing has
taken the work up since:

* another account has not answered it, by the ready lane's answer rule
  (``ready_queue.answered_sql``: a comment, a status change, an assignment or
  a claim, by an account other than the reporter and not the Automation
  actor);
* nobody, the reporter included, has claimed the issue again.

A completion ends its lease and only a claim starts one, so a later report on
the same issue always follows a claim: the second rule is also what makes the
newest report the only one listed.

Read-only and derived on every read from the trail and the verdict each
completion stores as a field (0086). A completion recorded before that
migration has no stored verdict and is never listed. Imported history never
reports and never answers.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import ready_queue, statuses
from athena.core import access

#: The verdicts that leave a decision open. The literal list in the SQL below
#: must stay exactly this text: the partial index of 0091 matches it.
HELD_VERDICTS = ("HOLD", "BLOCKED")
MAX_LIMIT = 200


def open_held_reports(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    accountable_only: bool = False,
    limit: int = MAX_LIMIT,
) -> dict:
    """Open HOLD and BLOCKED reports ``actor`` may read, oldest first, with
    their ``total``.

    ``actor=None`` is the admin rollup's caller contract and reads every
    project. ``accountable_only`` narrows to the reports ``actor`` answers for:
    on issues it is the assignee of, or delegated to the reporter, and never
    its own report. The count and the rows apply the same filter."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    answered, answered_params = ready_queue.answered_sql(
        issue_ref="i.id", after_ref="ce.id", settler_ref="ce.actor_id"
    )
    base = (
        # Driven from the few facts rows that hold (0091's partial index), each
        # joined to its event by id. Left to itself the planner walks every
        # issue event to find them: CROSS JOIN fixes the order.
        "FROM activity_event_facts f INDEXED BY idx_activity_event_facts_held "
        "CROSS JOIN activity ce ON ce.id = f.event_id "
        "JOIN issues i ON i.id = ce.target_id "
        "JOIN users u ON u.id = ce.actor_id "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "WHERE f.verdict IN ('HOLD', 'BLOCKED') "
        "AND ce.verb = 'claim_completed' AND ce.target_kind = 'issue' "
        "AND ce.imported_at IS NULL AND ce.visibility_restricted = 0 "
        "AND i.archived_at IS NULL "
        f"AND COALESCE({statuses.exact_category_sql('i')}, '') != 'done' "
        f"AND NOT {answered} "
        # The reporter taking it up again is not an answer by another account,
        # and the work is in hand all the same.
        "AND NOT EXISTS (SELECT 1 FROM activity rc "
        "WHERE rc.target_kind = 'issue' AND rc.target_id = i.id "
        "AND rc.id > ce.id AND rc.verb = 'claimed' AND rc.imported_at IS NULL)"
    )
    params: list = [*answered_params]
    gate, gate_params = access.event_visibility_clause(conn, actor, alias="ce")
    if gate:
        base += f" AND ({gate})"
        params.extend(gate_params)
    if accountable_only:
        if actor is None:
            raise ValueError("accountable_only needs an actor")
        base += (
            " AND ce.actor_id != ? AND (i.assignee_id = ? OR EXISTS ("
            "SELECT 1 FROM issue_contributors accountable "
            "WHERE accountable.issue_id = i.id "
            "AND accountable.user_id = ce.actor_id "
            "AND accountable.added_by = ?))"
        )
        params.extend([actor["id"], actor["id"], actor["id"]])
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    rows = conn.execute(
        "SELECT ce.id AS event_id, ce.created_at, ce.actor_id, u.name AS actor_name, "
        "f.verdict, i.id AS issue_id, i.title, i.project_seq, p.key AS project_key "
        f"{base} ORDER BY ce.id LIMIT ?",
        [*params, min(limit, MAX_LIMIT)],
    ).fetchall()
    items = []
    for row in rows:
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        items.append(
            {
                "issue": {
                    "id": int(row["issue_id"]),
                    "key": key,
                    "title": row["title"],
                },
                "completion_event_id": int(row["event_id"]),
                "verdict": row["verdict"],
                "reported_by": {
                    "id": int(row["actor_id"]),
                    "name": row["actor_name"],
                },
                "reported_at": row["created_at"],
            }
        )
    return {"items": items, "total": total}
