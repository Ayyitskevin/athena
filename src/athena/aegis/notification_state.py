"""Read-time state for inbox rows.

Core notifications say what happened. This module says what each row means now, from
the reader's point of view. It is a read. It writes nothing and acknowledges nothing.

The state describes the ROW, not only the target. Precedence, first match wins:

    archived          the target issue is archived
    completed_by_you  the reader completed a claim on the target at or after this
                      row's own event, so the row describes something they had
                      already handled. A row that arrived after their completion is
                      not labelled, even though the target is the same.
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

    Two queries cover every issue in the list, so the cost does not grow per row."""
    issue_ids = sorted({r["target_id"] for r in rows if r["target_kind"] == "issue"})
    issues: dict[int, tuple[int, int]] = {}
    last_completion: dict[int, int] = {}
    if issue_ids:
        marks = ",".join("?" for _ in issue_ids)
        category = statuses.category_sql(
            status_column="i.status", category_column="ips.category"
        )
        for row in conn.execute(
            f"SELECT i.id, i.archived_at IS NOT NULL AS archived, "
            f"({category}) = 'done' AS closed "
            f"FROM issues i "
            f"LEFT JOIN project_statuses ips "
            f"ON ips.project_id = i.project_id AND ips.name = i.status "
            f"WHERE i.id IN ({marks})",
            issue_ids,
        ).fetchall():
            issues[row["id"]] = (row["archived"], row["closed"])
        # The reader's own native completions, newest id per issue. Imported rows are
        # foreign history and never count as the reader's completion.
        for row in conn.execute(
            f"SELECT c.target_id, MAX(c.id) AS last_id FROM activity c "
            f"WHERE c.target_kind = 'issue' AND c.actor_id = ? "
            f"AND c.verb = 'claim_completed' AND c.imported_at IS NULL "
            f"AND c.target_id IN ({marks}) GROUP BY c.target_id",
            (reader_id, *issue_ids),
        ).fetchall():
            last_completion[row["target_id"]] = row["last_id"]
    return [
        {**row, "target_state": _state(row, issues, last_completion)} for row in rows
    ]


def _state(
    row: dict,
    issues: dict[int, tuple[int, int]],
    last_completion: dict[int, int],
) -> TargetState | None:
    if row["target_kind"] != "issue" or row["target_id"] not in issues:
        return None
    archived, closed = issues[row["target_id"]]
    if archived:
        return "archived"
    completed_at = last_completion.get(row["target_id"])
    # The completion must come at or after this row's own event: the row was already
    # there when the reader finished.
    if completed_at is not None and completed_at >= row["event_id"]:
        return "completed_by_you"
    if closed:
        return "closed"
    return None
