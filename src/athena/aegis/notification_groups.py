"""The inbox one row per target (AD12, D34).

In docs/plans/reports/agent-day-2026-11-26.md (DAY-10) one closed slice left
the conductor nine unread rows, most of them on one issue. Grouped, that is
one row: the target, how many rows it holds, the newest of them in full, and
the ids that acknowledge exactly that group. Grouping is a read over the
existing rows: nothing is stored, merged or marked.

The read scans the newest ``SCAN_ROWS`` rows (the inbox's own maximum page),
then groups them in order of each target's newest row. ``complete`` says
whether the scan reached the end of the inbox; when it did not, older rows of a
listed target are not in its count (the row inbox reads them).
"""

from __future__ import annotations

import sqlite3

from athena.aegis import notification_state
from athena.core import notification_commands, notifications

#: How many rows one grouped read scans: the row inbox's own largest page.
SCAN_ROWS = 200


def grouped_inbox(
    conn: sqlite3.Connection,
    actor: dict,
    *,
    unread_only: bool,
    limit: int,
) -> dict:
    rows = notifications.list_notifications(
        conn, actor["id"], unread_only=unread_only, limit=SCAN_ROWS, actor=actor
    )
    rows = notification_state.annotate_inbox(conn, actor["id"], rows)
    groups: dict[tuple[str, int], dict] = {}
    for row in rows:
        key = (row["target_kind"], int(row["target_id"]))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "target_kind": row["target_kind"],
                "target_id": int(row["target_id"]),
                "count": 0,
                "unread": 0,
                "verbs": [],
                "newest": row,
                "unread_ids": [],
            }
        group["count"] += 1
        if row["verb"] not in group["verbs"]:
            group["verbs"].append(row["verb"])
        if row.get("read_at") is None:
            group["unread"] += 1
            if len(group["unread_ids"]) < notification_commands.MAX_ACKNOWLEDGE_IDS:
                group["unread_ids"].append(int(row["id"]))
    listed = list(groups.values())[:limit]
    return {
        "items": listed,
        "complete": len(rows) < SCAN_ROWS,
        "rows_scanned": len(rows),
        "oldest_id_scanned": int(rows[-1]["id"]) if rows else None,
    }
