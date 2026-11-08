"""Alive is not progress: a read-only view of whether claimed work is moving.

A cooperative check-in says a run reported. A lease renewal says the holder asked
for more time. Neither says that any work happened, and on a quiet fleet both can
keep a claim looking healthy for weeks while nothing moves. This module answers the
other question — *when was work last recorded on this issue?* — from the activity
trail the app already writes, and keeps that answer next to (never merged with) the
check-in and lease times.

**What counts as work.** An explicit, closed list of issue verbs that record a change
to the work itself (``SUBSTANTIVE_WORK_VERBS``). Everything else on an issue's trail
is bookkeeping about who holds it or how the board is organised:

- lease bookkeeping: ``claimed``, ``lease_renewed`` — the clock, not the work;
- coordination: ``claim_yielded``, ``claim_handoff_resumed``, ``delegated``,
  ``delegation_declined`` — who should hold it;
- triage: ``assigned``/``unassigned``, ``added_contributor``/``removed_contributor``,
  ``labeled``/``unlabeled``, ``moved_to_sprint``/``removed_from_sprint``,
  ``changed_priority``, ``changed_project``/``removed_from_project``,
  ``archived``/``unarchived`` — the operator arranging the board;
- removals and birth: ``created``, ``comment_deleted``, ``removed_attachment``,
  ``unlinked``, ``removed_parent`` — not progress on the work.

Check-ins and heartbeats never reach the activity trail at all (they live in
``agent_run_checkins``), so they cannot be mistaken for work. Imported rows are
excluded, as elsewhere: a back-dated bundle must not make old work look new.

**The proxy's limit.** Any qualifying event counts, however small: a one-word
comment, a touch-up edit or a no-op field change refreshes "last work" exactly as a
real change does. This is a verb list, not a judge of effort. What keeps it honest
is the append-only trail: every event that refreshed freshness is on the issue's
history, so a run that only pokes an issue to look busy is visible there.

**It computes, it does not write.** Expired leases are shown as expired with their
holder and age and are never cleared or swept (K1). Every lane applies the viewer's
project visibility to its count and its rows alike; requests waiting on the operator
are the operator's and are shown only to an admin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
import sqlite3

from athena.aegis import statuses
from athena.core import access, approvals, identity, tokens

SCHEMA = "athena.work_progress.v1"

#: Issue verbs that record work on the issue itself. See the module docstring for
#: every verb that is deliberately left out, and why.
SUBSTANTIVE_WORK_VERBS: tuple[str, ...] = (
    "issue_edited",
    "changed_status",
    "overrode_blocked_issue_close",
    "commented",
    "comment_edited",
    "added_attachment",
    "linked",
    "set_parent",
    "claim_completed",
)

#: An in-progress issue with no recorded work for longer than this is listed as
#: stale. A week covers a weekend and a multi-day task, so a normal work cycle never
#: trips it; it is short enough that an idle item surfaces in days, not after the
#: two to five weeks such items have been seen to sit unnoticed.
STALE_WORK_DAYS = 7

#: A delegation the delegate has neither claimed nor worked on for longer than
#: this is "not picked up". A day spans an overnight gap and a full working day,
#: so a delegate that orients once a session has had at least one chance to see
#: it; past that, the hand-off has most likely been missed, not queued.
DELEGATION_UNCLAIMED_HOURS = 24

#: Each lane is a short list with its own total, like the desk's lanes.
LANE_LIMIT = 20

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_DOING_CATEGORY = "doing"
_STATUS_CATEGORY_SQL = statuses.category_sql(
    status_column="i.status", category_column="ps.category"
)


def _now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, _TS_FORMAT).replace(tzinfo=UTC)


def _age_seconds(stamp: str | None, *, now: datetime) -> int | None:
    if stamp is None:
        return None
    return max(0, int((now - _parse(stamp)).total_seconds()))


def age_text(seconds: int | None) -> str:
    """A short, honest age: 'unknown' when there is no time to measure from."""
    if seconds is None:
        return "unknown"
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def work_state(last_work_at: str | None, *, now: datetime) -> tuple[str, int | None]:
    """('recent' | 'stale' | 'unknown', age in seconds or None).

    Unknown is its own state: no recorded work is not the same as recent work, and
    it is never rendered as fresh."""
    age = _age_seconds(last_work_at, now=_now(now))
    if age is None:
        return "unknown", None
    if age > STALE_WORK_DAYS * 86400:
        return "stale", age
    return "recent", age


def _verb_placeholders() -> str:
    return ",".join("?" for _ in SUBSTANTIVE_WORK_VERBS)


_IDENT = re.compile(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*")
_VERB = re.compile(r"[a-z_]+")
# The verbs are inlined as SQL literals in the correlated form below, so a
# subquery can sit in an ORDER BY or a SELECT list without threading parameters
# through every caller. They are this module's own constants, never input; the
# check makes that a fact rather than a promise.
if not all(_VERB.fullmatch(verb) for verb in SUBSTANTIVE_WORK_VERBS):
    raise RuntimeError("SUBSTANTIVE_WORK_VERBS must be plain lowercase verb names")
_VERB_LITERALS = ", ".join(f"'{verb}'" for verb in SUBSTANTIVE_WORK_VERBS)


def last_work_sql(
    issue_ref: str, *, actor_ref: str | None = None, since_ref: str | None = None
) -> str:
    """A correlated scalar subquery: the latest native work time on one issue.

    ``issue_ref`` names the outer issue id column (``i.id``); ``actor_ref``
    narrows to one actor's work and ``since_ref`` to work at or after a time,
    both as outer column references. Arguments are column names chosen by the
    caller's own code and are checked as such, never values.

    Cost: one range read of ``idx_activity_target (target_kind, target_id, id)``
    over that issue's own trail per outer row — never a scan of the whole
    activity table."""
    for ref in (issue_ref, actor_ref, since_ref):
        if ref is not None and not _IDENT.fullmatch(ref):
            raise ValueError(f"not a column reference: {ref!r}")
    clauses = [
        "wa.target_kind = 'issue'",
        f"wa.target_id = {issue_ref}",
        "wa.imported_at IS NULL",
        f"wa.verb IN ({_VERB_LITERALS})",
    ]
    if actor_ref is not None:
        clauses.append(f"wa.actor_id = {actor_ref}")
    if since_ref is not None:
        clauses.append(f"wa.created_at >= {since_ref}")
    return f"(SELECT MAX(wa.created_at) FROM activity wa WHERE {' AND '.join(clauses)})"


def last_work_at(
    conn: sqlite3.Connection,
    pairs: list[tuple[int, int | None]],
) -> dict[tuple[int, int | None], str | None]:
    """Latest native work event per (issue id, actor id); actor None means anyone."""
    result: dict[tuple[int, int | None], str | None] = {}
    for issue_id, actor_id in sorted(set(pairs), key=lambda p: (p[0], p[1] or 0)):
        clauses = [
            "target_kind = 'issue'",
            "target_id = ?",
            "imported_at IS NULL",
            f"verb IN ({_verb_placeholders()})",
        ]
        params: list[object] = [issue_id, *SUBSTANTIVE_WORK_VERBS]
        if actor_id is not None:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        row = conn.execute(
            f"SELECT MAX(created_at) AS at FROM activity WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        result[(issue_id, actor_id)] = row["at"] if row else None
    return result


def progress_for(
    last_work: str | None,
    *,
    now: datetime,
    checkin_at: str | None = None,
    lease_activity_at: str | None = None,
) -> dict:
    """The progress block shown beside a claim: work and pulse, side by side."""
    state, age = work_state(last_work, now=_now(now))
    return {
        "last_work_at": last_work,
        "work_age_seconds": age,
        "work_state": state,
        "stale_after_days": STALE_WORK_DAYS,
        "last_checkin_at": checkin_at,
        "last_lease_activity_at": lease_activity_at,
        "meaning": (
            "last_work_at is the holder's latest recorded work on this issue "
            "(edit, status change, comment, attachment, link, completion). Check-ins "
            "and lease renewals are shown separately and never reset it; unknown "
            "means no work is recorded, not that the work is fresh."
        ),
    }


def _visibility_clause(conn: sqlite3.Connection, actor: dict) -> tuple[str, list[int]]:
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


def _issue_ref(row: sqlite3.Row) -> dict:
    key = None
    if row["project_key"] is not None and row["project_seq"] is not None:
        key = f"{row['project_key']}-{row['project_seq']}"
    return {
        "id": int(row["issue_id"]),
        "key": key,
        "title": row["title"],
        "project_id": row["project_id"],
    }


def _expired_leases(
    conn: sqlite3.Connection, *, actor: dict, now: datetime, limit: int
) -> dict:
    vis_sql, vis_params = _visibility_clause(conn, actor)
    now_text = now.strftime(_TS_FORMAT)
    base = (
        "FROM issue_leases l JOIN users u ON u.id = l.holder_id "
        "JOIN issues i ON i.id = l.issue_id "
        "LEFT JOIN projects p ON p.id = i.project_id "
        f"WHERE l.expires_at <= ?{vis_sql}"
    )
    params: list[object] = [now_text, *vis_params]
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    rows = conn.execute(
        "SELECT l.issue_id, l.holder_id, u.name AS holder_name, u.is_agent, "
        "l.claimed_at, l.expires_at, i.title, i.project_id, i.project_seq, "
        f"p.key AS project_key {base} ORDER BY l.expires_at, l.issue_id LIMIT ?",
        [*params, limit],
    ).fetchall()
    items = []
    for row in rows:
        age = _age_seconds(row["expires_at"], now=now)
        items.append(
            {
                "issue": _issue_ref(row),
                "holder": {
                    "id": int(row["holder_id"]),
                    "name": row["holder_name"],
                    "is_agent": bool(row["is_agent"]),
                },
                "state": "expired",
                "claimed_at": row["claimed_at"],
                "expires_at": row["expires_at"],
                "expired_for_seconds": age,
                "expired_for": age_text(age),
            }
        )
    return {
        "items": items,
        "visible_total": total,
        "limit": limit,
        "clipped": total > len(items),
        "meaning": (
            "leases whose clock has run out, oldest first, with their holder; "
            "shown as they are and never cleared or swept by reading them"
        ),
    }


def _stale_in_progress(
    conn: sqlite3.Connection, *, actor: dict, now: datetime, limit: int
) -> dict:
    vis_sql, vis_params = _visibility_clause(conn, actor)
    cutoff = (now - timedelta(days=STALE_WORK_DAYS)).strftime(_TS_FORMAT)
    # One grouped pass over the native work events, joined to the in-progress
    # issues — not one MAX per issue. This card is polled, and a per-issue
    # subquery grew with every in-progress issue (about 78 ms at 2,200 of them
    # against 50,000 events; see the PR body). Same meaning, pinned equal to the
    # per-issue reading by test_the_grouped_stale_query_matches_the_per_issue_reading.
    inner = (
        "SELECT i.id AS issue_id, i.title, i.project_id, i.project_seq, "
        "p.key AS project_key, w.last_work_at "
        "FROM issues i LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN project_statuses ps "
        "ON ps.project_id = i.project_id AND ps.name = i.status "
        "LEFT JOIN (SELECT a.target_id, MAX(a.created_at) AS last_work_at "
        "FROM activity a WHERE a.target_kind = 'issue' AND a.imported_at IS NULL "
        f"AND a.verb IN ({_verb_placeholders()}) GROUP BY a.target_id) w "
        "ON w.target_id = i.id "
        f"WHERE i.archived_at IS NULL AND {_STATUS_CATEGORY_SQL} = ?{vis_sql}"
    )
    params: list[object] = [*SUBSTANTIVE_WORK_VERBS, _DOING_CATEGORY, *vis_params]
    stale_where = "WHERE last_work_at IS NULL OR last_work_at < ?"
    # The total rides the same pass as a window count, taken before LIMIT, so the
    # grouped read runs once rather than once for the count and once for the rows.
    rows = conn.execute(
        f"SELECT *, COUNT(*) OVER () AS stale_total FROM ({inner}) {stale_where} "
        "ORDER BY last_work_at IS NOT NULL, last_work_at, issue_id LIMIT ?",
        [*params, cutoff, limit],
    ).fetchall()
    total = int(rows[0]["stale_total"]) if rows else 0
    items = []
    for row in rows:
        state, age = work_state(row["last_work_at"], now=now)
        items.append(
            {
                "issue": _issue_ref(row),
                "last_work_at": row["last_work_at"],
                "work_age_seconds": age,
                "work_age": age_text(age),
                "work_state": state,
            }
        )
    return {
        "items": items,
        "visible_total": total,
        "limit": limit,
        "clipped": total > len(items),
        "stale_after_days": STALE_WORK_DAYS,
        "meaning": (
            f"in-progress issues with no recorded work by anyone in "
            f"{STALE_WORK_DAYS} days, never-worked ones first; check-ins and lease "
            "renewals do not count. Per-claim progress on active work measures the "
            "holder's own work, so a claim can be stale there and absent here"
        ),
    }


def _oldest_waiting(
    conn: sqlite3.Connection, *, actor: dict, now: datetime
) -> dict | None:
    # Same boundary as the approval queue itself: the operator's asks are the
    # operator's to see.
    if not (
        identity.is_admin(actor) and identity.token_has_scope(actor, tokens.ADMIN_SCOPE)
    ):
        return None
    oldest, pending_total = approvals.oldest_pending(conn)
    if oldest is None:
        return None
    age = _age_seconds(oldest.created_at, now=now)
    return {
        "kind": "approval_request",
        "id": oldest.id,
        "action_kind": oldest.action_kind,
        "target_kind": oldest.target_kind,
        "target_id": oldest.target_id,
        "requested_by": oldest.requested_by,
        "created_at": oldest.created_at,
        "age_seconds": age,
        "age": age_text(age),
        "pending_total": pending_total,
        "link": f"/admin/agents#approval-{oldest.id}",
    }


def unclaimed_delegations(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    all_projects: bool = False,
    delegator_id: int | None = None,
    now: datetime | None = None,
    limit: int = LANE_LIMIT,
) -> dict:
    """Delegations nobody picked up: handed to someone more than
    ``DELEGATION_UNCLAIMED_HOURS`` ago, and since then the delegate has neither
    claimed the issue nor recorded any work on it.

    "Picked up" is the delegate's own act: a lease they hold (live or lapsed), a
    native ``claimed`` event since the delegation, or recorded work since it.
    Someone else commenting is not the delegate picking it up. Done and archived
    issues are not waiting on anyone. A self-delegation is not a hand-off.

    Visibility is the actor's (counts and rows alike). ``all_projects`` is for the
    admin-only attention card, whose caller contract is already admin-scoped;
    ``delegator_id`` narrows to one delegator's hand-offs (their desk). Read-only.
    """
    current = _now(now)
    vis_sql: str
    vis_params: list[int]
    if all_projects:
        vis_sql, vis_params = "", []
    elif actor is None:
        # No viewer: the backlog alone, the same narrowing an empty visible set gets.
        vis_sql, vis_params = " AND i.project_id IS NULL", []
    else:
        vis_sql, vis_params = _visibility_clause(conn, actor)
    cutoff = (current - timedelta(hours=DELEGATION_UNCLAIMED_HOURS)).strftime(
        _TS_FORMAT
    )
    delegator_sql = "" if delegator_id is None else " AND c.added_by = ?"
    delegator_params: list[object] = [] if delegator_id is None else [delegator_id]
    base = (
        "FROM issue_contributors c "
        "JOIN issues i ON i.id = c.issue_id "
        "JOIN users du ON du.id = c.user_id "
        "JOIN users bu ON bu.id = c.added_by "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN project_statuses ps "
        "ON ps.project_id = i.project_id AND ps.name = i.status "
        "WHERE c.user_id != c.added_by AND c.added_at <= ? "
        f"AND i.archived_at IS NULL AND {_STATUS_CATEGORY_SQL} != 'done' "
        "AND NOT EXISTS (SELECT 1 FROM issue_leases dl "
        "WHERE dl.issue_id = c.issue_id AND dl.holder_id = c.user_id) "
        "AND NOT EXISTS (SELECT 1 FROM activity da WHERE da.target_kind = 'issue' "
        "AND da.target_id = c.issue_id AND da.actor_id = c.user_id "
        "AND da.verb = 'claimed' AND da.imported_at IS NULL "
        "AND da.created_at >= c.added_at) "
        f"AND {last_work_sql('c.issue_id', actor_ref='c.user_id', since_ref='c.added_at')} "
        f"IS NULL{delegator_sql}{vis_sql}"
    )
    params: list[object] = [cutoff, *delegator_params, *vis_params]
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    rows = conn.execute(
        "SELECT c.issue_id, c.user_id AS delegate_id, du.name AS delegate_name, "
        "du.is_agent, c.added_by, bu.name AS delegator_name, c.added_at, "
        "i.title, i.project_id, i.project_seq, p.key AS project_key "
        f"{base} ORDER BY c.added_at, c.issue_id, c.user_id LIMIT ?",
        [*params, limit],
    ).fetchall()
    items = []
    for row in rows:
        waited = _age_seconds(row["added_at"], now=current)
        items.append(
            {
                "issue": _issue_ref(row),
                "delegate": {
                    "id": int(row["delegate_id"]),
                    "name": row["delegate_name"],
                    "is_agent": bool(row["is_agent"]),
                },
                "delegated_by": {
                    "id": int(row["added_by"]),
                    "name": row["delegator_name"],
                },
                "delegated_at": row["added_at"],
                "waiting_seconds": waited,
                "waiting": age_text(waited),
            }
        )
    return {
        "items": items,
        "visible_total": total,
        "limit": limit,
        "clipped": total > len(items),
        "older_than_hours": DELEGATION_UNCLAIMED_HOURS,
        "meaning": (
            f"delegations older than {DELEGATION_UNCLAIMED_HOURS}h that the delegate "
            "has neither claimed nor recorded work on since, oldest first; work by "
            "anyone else does not count as pickup, and done or archived issues are "
            "not waiting"
        ),
    }


def build_progress(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
    limit: int = LANE_LIMIT,
) -> dict:
    """The progress lanes for this viewer: expired leases, stale in-progress work and
    the oldest request waiting on the operator. One clock, read-only."""
    current = _now(now)
    return {
        "schema": SCHEMA,
        "observed_at": current.strftime(_TS_FORMAT),
        "work_verbs": list(SUBSTANTIVE_WORK_VERBS),
        "expired_leases": _expired_leases(conn, actor=actor, now=current, limit=limit),
        "stale_in_progress": _stale_in_progress(
            conn, actor=actor, now=current, limit=limit
        ),
        "oldest_waiting_on_operator": _oldest_waiting(conn, actor=actor, now=current),
    }
