"""The operator's "since you last looked": what changed after your desk cursor.

The desk cursor (``agent_cursors``, name ``desk``) is any user's acknowledged
position in the activity trail; the table takes a human or admin as readily as
an agent, so the operator reuses the same cursor and needs no migration. This
read counts what arrived after it, by kind:

- **asks:** claims yielded with a blocking question (``claim_yielded``);
- **completions:** ``claim_completed``, each with the counts its report carries
  when it has one — counts of each reported state, never an overall verdict;
- **blockers:** ``linked`` events whose relation is ``blocks`` or ``blocked_by``;
- **approvals:** ``approval_requested``;
- **other:** every other event in the window, so "nothing new" is never claimed
  while something did arrive;
- **stalled:** in-progress issues whose last recorded work (creation when none)
  crossed ``work_progress.STALE_WORK_DAYS`` since the cursor last moved. This one
  is a clock, not an event.

**Reading never moves the cursor.** Only an explicit "mark seen" does, and it
moves to ``last_shown_event_id``: the newest event this read examined. The read
takes the oldest ``SCAN_LIMIT`` events after the cursor, so marking seen never
skips an event the card did not show, and anything that arrives after the
render is still new next time.

Native events only, and every event passes the trail's own visibility gate
(``access.event_visibility_clause``); the stalled rows apply project visibility.
Read-only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3

from athena.aegis import lease_commands, statuses, work_progress
from athena.core import access, desk_cursors, identity

SCHEMA = "athena.operator_since.v1"

#: How many events after the cursor one read examines, oldest first — the same
#: bound the desk's own "events since cursor" counter uses.
SCAN_LIMIT = 500
#: Rows listed for completions and stalled items; each kind's count is the total.
ROW_LIMIT = 10

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_BLOCK_RELATIONS = ("blocks ", "blocked_by ")
_WORK_PLACEHOLDERS = ",".join("?" for _ in work_progress.SUBSTANTIVE_WORK_VERBS)


def _now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _issue_refs(conn: sqlite3.Connection, issue_ids: list[int]) -> dict[int, dict]:
    if not issue_ids:
        return {}
    placeholders = ",".join("?" for _ in issue_ids)
    rows = conn.execute(
        "SELECT i.id, i.title, i.project_seq, p.key AS project_key FROM issues i "
        f"LEFT JOIN projects p ON p.id = i.project_id WHERE i.id IN ({placeholders})",
        issue_ids,
    ).fetchall()
    out = {}
    for row in rows:
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        out[int(row["id"])] = {"id": int(row["id"]), "key": key, "title": row["title"]}
    return out


def _reported_counts(event: dict) -> dict | None:
    """The report's own counts, or None when the completion carried no report."""
    report = lease_commands.reported_completion(event)
    if report is None:
        return None
    counts: dict[str, int] = {}
    for state in ("met", "not_met", "not_assessed", "inconclusive"):
        n = sum(result["state"] == state for result in report.get("results", []))
        if n:
            counts[state] = n
    for field in ("unfinished", "uncertainty"):
        n = len(report.get(field, []))
        if n:
            counts[field] = n
    return counts


def _events_after(
    conn: sqlite3.Connection, *, actor: dict, after_id: int
) -> tuple[list[dict], bool]:
    """The oldest ``SCAN_LIMIT`` visible native events after the cursor."""
    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    where = f"a.id > ? AND a.imported_at IS NULL{f' AND {gate}' if gate else ''}"
    rows = conn.execute(
        "SELECT a.id, a.verb, a.target_kind, a.target_id, a.detail, a.created_at, "
        "a.imported_at, a.actor_id, u.name AS actor_name FROM activity a "
        f"JOIN users u ON u.id = a.actor_id WHERE {where} ORDER BY a.id LIMIT ?",
        [after_id, *params, SCAN_LIMIT + 1],
    ).fetchall()
    events = [dict(row) for row in rows]
    return events[:SCAN_LIMIT], len(events) > SCAN_LIMIT


def _stalled(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    since_at: str | None,
    now: datetime,
) -> tuple[list[dict], int]:
    """In-progress issues whose last work (creation when none) crossed the stale
    threshold after ``since_at``; with no earlier look, every one now stale."""
    threshold = timedelta(days=work_progress.STALE_WORK_DAYS)
    stale_before = (now - threshold).strftime(_TS_FORMAT)
    category = statuses.category_sql(
        status_column="i.status", category_column="ps.category"
    )
    clauses = [
        "i.archived_at IS NULL",
        f"{category} = 'doing'",
        "COALESCE(w.last_work_at, i.created_at) < ?",
    ]
    params: list[object] = [*work_progress.SUBSTANTIVE_WORK_VERBS, stale_before]
    if since_at is not None:
        crossed_after = (
            datetime.strptime(since_at, _TS_FORMAT).replace(tzinfo=UTC) - threshold
        ).strftime(_TS_FORMAT)
        clauses.append("COALESCE(w.last_work_at, i.created_at) >= ?")
        params.append(crossed_after)
    visible = access.visible_project_filter(conn, actor)
    if visible is not None:
        if visible:
            placeholders = ",".join("?" for _ in visible)
            clauses.append(
                f"(i.project_id IS NULL OR i.project_id IN ({placeholders}))"
            )
            params.extend(sorted(visible))
        else:
            clauses.append("i.project_id IS NULL")
    rows = conn.execute(
        "SELECT i.id, COALESCE(w.last_work_at, i.created_at) AS anchor, "
        "w.last_work_at, COUNT(*) OVER () AS stalled_total "
        "FROM issues i LEFT JOIN project_statuses ps "
        "ON ps.project_id = i.project_id AND ps.name = i.status "
        "LEFT JOIN (SELECT a.target_id, MAX(a.created_at) AS last_work_at "
        "FROM activity a WHERE a.target_kind = 'issue' AND a.imported_at IS NULL "
        f"AND a.verb IN ({_WORK_PLACEHOLDERS}) GROUP BY a.target_id) w "
        "ON w.target_id = i.id "
        f"WHERE {' AND '.join(clauses)} ORDER BY anchor, i.id LIMIT ?",
        [*params, ROW_LIMIT],
    ).fetchall()
    total = int(rows[0]["stalled_total"]) if rows else 0
    refs = _issue_refs(conn, [int(row["id"]) for row in rows])
    items = [
        {
            "issue": refs[int(row["id"])],
            "last_work_at": row["last_work_at"],
            "idle_since": row["anchor"],
        }
        for row in rows
    ]
    return items, total


def build_since(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> dict:
    """This actor's changes since their desk cursor, by kind. Read-only: the
    cursor moves only through ``desk_commands.advance_cursor``."""
    current = _now(now)
    cursor = desk_cursors.get_cursor(
        conn, user_id=int(actor["id"]), name=desk_cursors.DESK_CURSOR
    )
    after_id = 0 if cursor is None else int(cursor["after_id"])
    since_at = None if cursor is None else cursor["updated_at"]
    events, capped = _events_after(conn, actor=actor, after_id=after_id)

    asks = completions = blockers = approvals = other = 0
    completion_events: list[dict] = []
    for event in events:
        verb = event["verb"]
        if verb == "claim_yielded":
            asks += 1
        elif verb == "claim_completed" and event["target_kind"] == "issue":
            completions += 1
            completion_events.append(event)
        elif verb == "linked" and str(event["detail"] or "").startswith(
            _BLOCK_RELATIONS
        ):
            blockers += 1
        elif verb == "approval_requested":
            approvals += 1
        else:
            other += 1
    shown = completion_events[:ROW_LIMIT]
    refs = _issue_refs(conn, [int(event["target_id"]) for event in shown])
    completion_rows = [
        {
            "issue": refs.get(int(event["target_id"]), {"id": int(event["target_id"])}),
            "holder": {"id": int(event["actor_id"]), "name": event["actor_name"]},
            "completed_at": event["created_at"],
            "reported_counts": _reported_counts(event),
        }
        for event in shown
    ]
    stalled_rows, stalled_total = _stalled(
        conn, actor=actor, since_at=since_at, now=current
    )

    operator = identity.is_admin(actor)
    kinds = [
        {
            "key": "asks",
            "label": "New questions from runs",
            "count": asks,
            "href": "/admin/agents#questions" if operator else None,
        },
        {
            "key": "completions",
            "label": "Claims completed",
            "count": completions,
            "href": "/admin/agents/runs" if operator else None,
        },
        {
            "key": "blockers",
            "label": "New blockers",
            "count": blockers,
            "href": "/find?q=has:blockers",
        },
        {
            "key": "approvals",
            "label": "Approvals raised",
            "count": approvals,
            "href": "/admin/agents" if operator else None,
        },
        {
            "key": "stalled",
            "label": f"Newly stalled (no work in {work_progress.STALE_WORK_DAYS} days)",
            "count": stalled_total,
            "href": "/aegis/dashboard#fleet-attention-heading" if operator else None,
        },
        {"key": "other", "label": "Other events", "count": other, "href": None},
    ]
    return {
        "schema": SCHEMA,
        "observed_at": current.strftime(_TS_FORMAT),
        "cursor": cursor,
        "since": since_at,
        "never_marked": cursor is None,
        "kinds": kinds,
        "completions": completion_rows,
        "stalled": stalled_rows,
        "examined_events": len(events),
        "capped": capped,
        # What "mark seen" advances to: the newest event this read examined, so an
        # event that arrives after this read is still new next time.
        "last_shown_event_id": max((int(e["id"]) for e in events), default=None),
        "empty": not events and stalled_total == 0,
        "meaning": (
            "events after your desk cursor, oldest first up to "
            f"{SCAN_LIMIT}, counted by kind, plus in-progress issues that crossed "
            f"the {work_progress.STALE_WORK_DAYS}-day stale threshold since you "
            "last marked seen. Reading never moves the cursor; mark seen moves it "
            "to last_shown_event_id. Completion counts are the report's own, never "
            "an overall verdict."
        ),
    }
