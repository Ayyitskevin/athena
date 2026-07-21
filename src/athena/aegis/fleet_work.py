"""Truthful active-work projection for the operator's agent fleet.

Athena already persists the facts needed to answer "what has an agent claimed?":
issue leases, claim activity, cooperative run check-ins, dependency links, and run
events.  This module joins those owners inside one read transaction.  It does not
invent a second run state or treat a heartbeat as process liveness.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena.aegis import issues
from athena.core import agent_run_checkins, db


SCHEMA = "athena.fleet_active_work.v1"
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
MAX_BLOCKER_PREVIEW = 5

_QUERY_FIELDS = {"agent_id", "limit"}

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_STATUS_CATEGORY_SQL = (
    "COALESCE(ps.category, CASE i.status "
    "WHEN 'open' THEN 'todo' WHEN 'done' THEN 'done' ELSE 'doing' END)"
)
_BLOCKER_CATEGORY_SQL = (
    "COALESCE(bps.category, CASE bi.status "
    "WHEN 'open' THEN 'todo' WHEN 'done' THEN 'done' ELSE 'doing' END)"
)


class ActiveWorkQueryError(ValueError):
    """A strict, transport-neutral rejection of active-work query criteria."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _parse_query_int(
    value: str | None,
    *,
    name: str,
    maximum: int,
    default: int | None = None,
) -> int | None:
    if value is None:
        return default
    if not value or not value.isascii() or not value.isdigit():
        raise ActiveWorkQueryError(f"{name} must be an ASCII-decimal integer")
    normalized = value.lstrip("0") or "0"
    maximum_text = str(maximum)
    if len(normalized) > len(maximum_text) or (
        len(normalized) == len(maximum_text) and normalized > maximum_text
    ):
        raise ActiveWorkQueryError(f"{name} must be between 1 and {maximum}")
    parsed = int(normalized)
    if parsed < 1:
        raise ActiveWorkQueryError(f"{name} must be between 1 and {maximum}")
    return parsed


def parse_query_pairs(pairs: list[tuple[str, str]]) -> tuple[int | None, int]:
    """Reject unknown or repeated HTTP criteria and normalize exact integers."""

    raw: dict[str, str] = {}
    for key, value in pairs:
        if key not in _QUERY_FIELDS:
            raise ActiveWorkQueryError(f"unsupported query parameter: {key}")
        if key in raw:
            raise ActiveWorkQueryError(
                f"query parameter may appear only once: {key}"
            )
        raw[key] = value
    agent_id = _parse_query_int(
        raw.get("agent_id"),
        name="agent_id",
        maximum=issues.MAX_SQLITE_INTEGER,
    )
    limit = _parse_query_int(
        raw.get("limit"),
        name="limit",
        maximum=MAX_LIMIT,
        default=DEFAULT_LIMIT,
    )
    assert limit is not None
    return agent_id, limit


def build_active_work(
    conn: sqlite3.Connection,
    *,
    agent_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict:
    """Return a bounded admin projection of agent-held issue leases.

    Lease expiry and cooperative check-in freshness are evaluated against the same
    server-owned instant.  A claim can be active while its exact run is stale,
    unreported, or untagged; those states are explicit attention reasons rather than
    being collapsed into a misleading "healthy/running" label.
    """
    normalized_agent_id = _optional_positive_int(agent_id, name="agent_id")
    bounded_limit = _positive_bounded_int(limit, name="limit", maximum=MAX_LIMIT)
    observed_at = _utc_now(now)
    observed_text = observed_at.strftime(_TS_FORMAT)

    with db.transaction(conn):
        clauses = ["u.is_agent = 1"]
        params: list[object] = []
        if normalized_agent_id is not None:
            clauses.append("l.holder_id = ?")
            params.append(normalized_agent_id)
        where_sql = " AND ".join(clauses)

        visible_total = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM issue_leases l "
                "JOIN users u ON u.id = l.holder_id "
                f"WHERE {where_sql}",
                params,
            ).fetchone()["n"]
        )
        rows = conn.execute(
            "SELECT l.issue_id, l.holder_id, u.name AS holder_name, "
            "u.email AS holder_email, u.role AS holder_role, "
            "u.paused_at AS holder_paused_at, l.claimed_at, l.expires_at, "
            "i.title AS issue_title, i.status AS issue_status, i.priority, "
            "i.project_id, i.project_seq, i.assignee_id, i.archived_at, "
            "p.key AS project_key, p.name AS project_name, "
            f"{_STATUS_CATEGORY_SQL} AS status_category, "
            "CASE WHEN (u.role = 'admin' OR i.assignee_id = l.holder_id "
            "OR EXISTS (SELECT 1 FROM issue_contributors contributor "
            "WHERE contributor.issue_id = l.issue_id "
            "AND contributor.user_id = l.holder_id)) "
            # Keep the visibility half aligned with access.can_see_project_or_backlog.
            "AND (i.project_id IS NULL OR (p.id IS NOT NULL AND ("
            "p.visibility = 'public' OR u.role = 'admin' "
            "OR p.created_by = l.holder_id "
            "OR EXISTS (SELECT 1 FROM project_members membership "
            "WHERE membership.project_id = i.project_id "
            "AND membership.user_id = l.holder_id)))) "
            "THEN 1 ELSE 0 END "
            "AS holder_eligible, "
            "claim_event.id AS claim_event_id, claim_event.run_id "
            "FROM issue_leases l "
            "JOIN users u ON u.id = l.holder_id "
            "JOIN issues i ON i.id = l.issue_id "
            "LEFT JOIN projects p ON p.id = i.project_id "
            "LEFT JOIN project_statuses ps "
            "ON ps.project_id = i.project_id AND ps.name = i.status "
            "LEFT JOIN activity claim_event ON claim_event.id = ("
            "SELECT candidate.id FROM activity candidate "
            "WHERE candidate.target_kind = 'issue' "
            "AND candidate.target_id = l.issue_id "
            "AND candidate.actor_id = l.holder_id "
            "AND candidate.verb IN ('claimed', 'lease_renewed') "
            "AND candidate.imported_at IS NULL "
            "AND candidate.created_at >= l.claimed_at "
            "ORDER BY candidate.id DESC LIMIT 1"
            ") "
            f"WHERE {where_sql} "
            "ORDER BY CASE WHEN l.expires_at > ? THEN 0 ELSE 1 END, "
            "l.expires_at, l.issue_id LIMIT ?",
            [*params, observed_text, bounded_limit],
        ).fetchall()

        issue_ids = [int(row["issue_id"]) for row in rows]
        holder_ids = [int(row["holder_id"]) for row in rows]
        live_token_counts = _live_issue_write_token_counts(conn, holder_ids)
        blockers = _open_blocker_summaries(conn, issue_ids)
        run_ids = {row["run_id"] for row in rows if row["run_id"] is not None}
        replay_ready = _replay_ready_run_ids(conn, run_ids)

        items = [
            _item(
                conn,
                row,
                observed_at=observed_at,
                blocker_info=blockers.get(
                    int(row["issue_id"]),
                    {"items": [], "visible_total": 0, "clipped": False},
                ),
                live_issue_write_token_count=live_token_counts.get(
                    int(row["holder_id"]), 0
                ),
                replay_ready=row["run_id"] in replay_ready,
            )
            for row in rows
        ]

    return {
        "schema": SCHEMA,
        "scope": "admin_fleet_view",
        "observed_at": observed_text,
        "semantics": {
            "claim_state": "server_time_lease",
            "reporting_state": "cooperative_exact_run_checkin",
            "does_not_assert": [
                "process_alive",
                "work_executing",
                "issue_unblocked",
                "approval_granted",
            ],
        },
        "items": items,
        "visible_total": visible_total,
        "limit": bounded_limit,
        "clipped": visible_total > len(items),
        "summary": {
            "scope": "returned_items",
            "returned_count": len(items),
            "active_claim_count": sum(
                item["lease"]["claim_state"] == "active" for item in items
            ),
            "expired_claim_count": sum(
                item["lease"]["claim_state"] == "expired" for item in items
            ),
            "needs_attention_count": sum(
                item["attention_state"] == "needs_attention" for item in items
            ),
        },
    }


def _item(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    observed_at: datetime,
    blocker_info: dict,
    live_issue_write_token_count: int,
    replay_ready: bool,
) -> dict:
    expires_at = _parse_timestamp(row["expires_at"])
    claim_state = "active" if observed_at < expires_at else "expired"
    run_id = row["run_id"]
    checkin = None
    reporting_state = "untagged"
    if run_id is not None:
        checkin = agent_run_checkins.get_checkin(
            conn,
            agent_id=int(row["holder_id"]),
            run_id=run_id,
            now=observed_at,
        )
        reporting_state = (
            "not_reported" if checkin is None else checkin["reporting_state"]
        )

    holder_eligible = bool(row["holder_eligible"])
    attention_reasons: list[str] = []
    if row["archived_at"] is not None:
        attention_reasons.append("issue_archived")
    if row["status_category"] == "done":
        attention_reasons.append("issue_done")
    if row["holder_paused_at"] is not None:
        attention_reasons.append("holder_paused")
    if row["holder_role"] == "viewer":
        attention_reasons.append("holder_read_only")
    if not holder_eligible:
        attention_reasons.append("holder_ineligible")
    if live_issue_write_token_count == 0:
        attention_reasons.append("no_live_issue_write_token")
    if claim_state == "expired":
        attention_reasons.append("lease_expired")
    if reporting_state == "untagged":
        attention_reasons.append("run_untagged")
    elif reporting_state == "not_reported":
        attention_reasons.append("checkin_missing")
    elif reporting_state == agent_run_checkins.STALE:
        attention_reasons.append("checkin_stale")
    if blocker_info["visible_total"]:
        attention_reasons.append("visible_open_blockers")

    issue_key = None
    if row["project_key"] is not None and row["project_seq"] is not None:
        issue_key = f"{row['project_key']}-{row['project_seq']}"

    return {
        "issue": {
            "id": int(row["issue_id"]),
            "key": issue_key,
            "title": row["issue_title"],
            "status": row["issue_status"],
            "status_category": row["status_category"],
            "priority": row["priority"],
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "assignee_id": row["assignee_id"],
            "archived_at": row["archived_at"],
        },
        "holder": {
            "id": int(row["holder_id"]),
            "name": row["holder_name"],
            "email": row["holder_email"],
            "role": row["holder_role"],
            "paused_at": row["holder_paused_at"],
            "eligible_for_issue": holder_eligible,
            "live_issue_write_token_count": live_issue_write_token_count,
        },
        "lease": {
            "claimed_at": row["claimed_at"],
            "expires_at": row["expires_at"],
            "claim_state": claim_state,
        },
        "run": {
            "claim_event_id": row["claim_event_id"],
            "run_id": run_id,
            "reporting_state": reporting_state,
            "last_seen_at": None if checkin is None else checkin["last_seen_at"],
            "age_seconds": None if checkin is None else checkin["age_seconds"],
            "replay_ready": replay_ready,
            "evidence_links_available": _run_links_available(run_id, replay_ready),
        },
        "open_blockers": blocker_info,
        "attention_state": (
            "needs_attention" if attention_reasons else "observed"
        ),
        "attention_reasons": attention_reasons,
    }


def _live_issue_write_token_statement(
    holder_ids: list[int],
) -> tuple[str, list[int]]:
    ordered = sorted(set(holder_ids))
    placeholders = ",".join("?" for _ in ordered)
    return (
        "SELECT token.user_id, COUNT(*) AS n FROM api_tokens token "
        f"WHERE token.user_id IN ({placeholders}) "
        "AND token.revoked_at IS NULL "
        "AND (COALESCE(TRIM(token.scopes), '') = '' "
        "OR INSTR(' ' || token.scopes || ' ', ' issue:write ') > 0 "
        "OR INSTR(' ' || token.scopes || ' ', ' admin ') > 0) "
        "GROUP BY token.user_id",
        ordered,
    )


def _live_issue_write_token_counts(
    conn: sqlite3.Connection, holder_ids: list[int]
) -> dict[int, int]:
    if not holder_ids:
        return {}
    statement, params = _live_issue_write_token_statement(holder_ids)
    return {
        int(row["user_id"]): int(row["n"])
        for row in conn.execute(statement, params)
    }


def _open_blocker_summaries(
    conn: sqlite3.Connection, issue_ids: list[int]
) -> dict[int, dict]:
    if not issue_ids:
        return {}
    placeholders = ",".join("?" for _ in issue_ids)
    rows = conn.execute(
        "WITH open_blockers AS ("
        "SELECT links.to_id AS blocked_id, bi.id, bi.title, bi.status, "
        "bi.project_seq, bp.key AS project_key, "
        "ROW_NUMBER() OVER (PARTITION BY links.to_id ORDER BY bi.id) AS preview_rank, "
        "COUNT(*) OVER (PARTITION BY links.to_id) AS blocker_count "
        "FROM issue_links links "
        "JOIN issues bi ON bi.id = links.from_id "
        "LEFT JOIN projects bp ON bp.id = bi.project_id "
        "LEFT JOIN project_statuses bps "
        "ON bps.project_id = bi.project_id AND bps.name = bi.status "
        f"WHERE links.kind = 'blocks' AND links.to_id IN ({placeholders}) "
        f"AND {_BLOCKER_CATEGORY_SQL} != 'done'"
        ") "
        "SELECT blocked_id, id, title, status, project_seq, project_key, "
        "preview_rank, blocker_count FROM open_blockers "
        "WHERE preview_rank <= ? ORDER BY blocked_id, preview_rank",
        [*issue_ids, MAX_BLOCKER_PREVIEW],
    ).fetchall()
    grouped: dict[int, dict] = {}
    for row in rows:
        blocked_id = int(row["blocked_id"])
        bucket = grouped.setdefault(
            blocked_id,
            {
                "items": [],
                "visible_total": int(row["blocker_count"]),
                "clipped": int(row["blocker_count"]) > MAX_BLOCKER_PREVIEW,
            },
        )
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        bucket["items"].append(
            {
                "id": int(row["id"]),
                "key": key,
                "title": row["title"],
                "status": row["status"],
            }
        )
    return grouped


def _replay_ready_run_ids(
    conn: sqlite3.Connection, run_ids: set[str]
) -> set[str]:
    if not run_ids:
        return set()
    ordered = sorted(run_ids)
    placeholders = ",".join("?" for _ in ordered)
    return {
        row["run_id"]
        for row in conn.execute(
            "SELECT DISTINCT a.run_id FROM activity a "
            "JOIN users u ON u.id = a.actor_id "
            f"WHERE a.run_id IN ({placeholders}) AND u.is_agent = 1",
            ordered,
        )
    }


def _run_links_available(run_id: str | None, replay_ready: bool) -> bool:
    """Whether existing path-parameter evidence routes can address this run id."""
    if not replay_ready or run_id is None or run_id in {".", ".."}:
        return False
    return all(
        char.isascii() and (char.isalnum() or char in "-._~:")
        for char in run_id
    )


def _optional_positive_int(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    return _positive_bounded_int(
        value,
        name=name,
        maximum=issues.MAX_SQLITE_INTEGER,
    )


def _positive_bounded_int(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _utc_now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)
