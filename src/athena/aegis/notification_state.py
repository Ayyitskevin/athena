"""Read-time target state for inbox rows.

Core notifications say what happened. This module says what the target looks like
now, from the reader's point of view: is it archived, did this reader complete it, is
it closed. It is a read. It writes nothing and acknowledges nothing.

The four states, in precedence order (first match wins):

    archived          the target issue is archived
    completed_by_you  the reader has a claim_completed event on the target
    closed            the target's status is in the done category
    None              none of the above, or the target is not an issue

Only rows the reader is allowed to see reach this function, because the caller
gates the list first (core ``list_notifications``). A hidden target's row is never
passed in, so its state can never be returned.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from athena.aegis import statuses

TargetState = Literal["archived", "completed_by_you", "closed"]


def annotate_inbox(
    conn: sqlite3.Connection, reader_id: int, rows: list[dict]
) -> list[dict]:
    """Return copies of ``rows`` with a ``target_state`` key added to each.

    One query covers every issue in the list, so the cost does not grow per row."""
    issue_ids = sorted({r["target_id"] for r in rows if r["target_kind"] == "issue"})
    states: dict[int, TargetState | None] = {}
    if issue_ids:
        marks = ",".join("?" for _ in issue_ids)
        category = statuses.category_sql(
            status_column="i.status", category_column="ips.category"
        )
        completed = (
            "EXISTS (SELECT 1 FROM activity c "
            "WHERE c.target_kind = 'issue' AND c.target_id = i.id "
            "AND c.actor_id = ? AND c.verb = 'claim_completed' "
            "AND c.imported_at IS NULL)"
        )
        found = conn.execute(
            f"SELECT i.id, i.archived_at IS NOT NULL AS archived, "
            f"{completed} AS completed, ({category}) = 'done' AS closed "
            f"FROM issues i "
            f"LEFT JOIN project_statuses ips "
            f"ON ips.project_id = i.project_id AND ips.name = i.status "
            f"WHERE i.id IN ({marks})",
            (reader_id, *issue_ids),
        ).fetchall()
        for row in found:
            states[row["id"]] = _state(row["archived"], row["completed"], row["closed"])
    return [
        {
            **row,
            "target_state": states.get(row["target_id"])
            if row["target_kind"] == "issue"
            else None,
        }
        for row in rows
    ]


def _state(archived: int, completed: int, closed: int) -> TargetState | None:
    if archived:
        return "archived"
    if completed:
        return "completed_by_you"
    if closed:
        return "closed"
    return None
