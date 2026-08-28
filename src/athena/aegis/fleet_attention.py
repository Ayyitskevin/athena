"""One rollup of everything asking for the operator's attention.

Athena grew its exception surfaces one at a time, and each landed on its own page:
claims needing attention in Mission Control, failing automation rules beside them,
failing webhooks somewhere else, and — before this — refused logins nowhere at
all, pending approvals only on the agents page, and unanswered kill requests only
on the worker list. An operator who is supposed to *steer by exception* had to
already know all six places to look.

This is the one number-per-thing card, and it is deliberately only that: counts
plus the link to the surface that owns each one. It computes no state of its own,
so it cannot disagree with the pages it points at.

**Every count says what it counts.** `needs_attention` claims are the ones the
bounded active-work window examined, not a fleet-wide total (see
`fleet_work.build_active_work`). Security refusals and budget exhaustions are
counted over a stated recent window, because "someone probed the boundary once,
months ago" is not the same alarm as "someone is probing now".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from typing import Literal, cast
from urllib.parse import quote, urlencode

from athena.aegis import automation, dependencies, fleet_work, issues
from athena.core import (
    access,
    approvals,
    budgets,
    run_controls,
    security_events,
    users,
    webhooks,
    workers,
)
from athena.core import identity, tokens

SCHEMA = "athena.fleet_attention.v1"

#: How far back the event-counted signals look. A day is long enough to catch an
#: overnight probe and short enough that a stale number never reads as a live one.
DEFAULT_WINDOW_HOURS = 24

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _normalized_now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current


def _since_text(*, window_hours: int, now: datetime | None) -> str:
    current = _normalized_now(now)
    return (current - timedelta(hours=window_hours)).strftime(_TS_FORMAT)


def build_attention(
    conn: sqlite3.Connection,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
    actor: dict | None = None,
    ranking_signals: set[str] | None = None,
    ranking_limit: int = 20,
) -> dict:
    """Count what needs a human, and say where each number lives.

    Admin-only by the caller's contract: every input here is already an
    admin-scoped read (active work, automation rules, webhook health, the approval
    queue, the worker registry, refusal events), and aggregating them does not
    widen any of them.
    """
    if isinstance(window_hours, bool) or not isinstance(window_hours, int):
        raise ValueError("window_hours must be an integer")
    if window_hours < 1:
        raise ValueError("window_hours must be positive")
    current = _normalized_now(now)
    since = _since_text(window_hours=window_hours, now=current)

    # Ask the active-work projection for exactly the attention rows, so this card
    # and Mission Control's filtered view are the same query with the same bounds.
    attention_work = fleet_work.build_active_work(
        conn, attention_state=fleet_work.NEEDS_ATTENTION, now=current
    )
    failing_rules = automation.list_rules(conn, failing_only=True)
    failing_webhooks = [
        hook for hook in webhooks.list_webhooks(conn) if hook["failure_count"]
    ]
    pending_approvals = approvals.list_requests(conn, state="pending", limit=200)
    unanswered_kills = [
        worker
        for worker in workers.list_workers(conn, limit=workers.MAX_LIST_LIMIT)
        if worker["kill_state"] in (workers.KILL_REQUESTED, workers.KILL_DEFIED)
    ]
    # Live run controls: asked, not yet answered, clock still running. Standing
    # like the kill count — a week-old unanswered steer is still unanswered —
    # and derived with the same predicate the controls page lists by.
    open_controls = run_controls.count_open(conn, now_stamp=run_controls.stamp(current))
    refusals = security_events.failure_counts(conn, since=since)
    # Native rows only, like the refusal counts above: a hostile import bundle
    # could back-date this verb into the window and inflate the card.
    budget_exhaustions = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM activity "
            "WHERE verb = ? AND created_at >= ? AND imported_at IS NULL",
            (budgets.VERB_BUDGET_EXHAUSTED, since),
        ).fetchone()["n"]
    )

    signals = [
        {
            "key": "claims_needing_attention",
            "label": "Claims needing attention",
            "count": len(attention_work["items"]),
            "href": "/admin/agents/runs?attention_state=needs_attention",
            # This one is window-bounded twice over: by the active-work limit and
            # by what the projection examined. Saying so here keeps the card from
            # implying a fleet-wide total it never computed.
            "scope": (
                f"of {attention_work['examined_count']} claims examined"
                + (" (more exist)" if attention_work["clipped"] else "")
            ),
        },
        {
            "key": "pending_approvals",
            "label": "Approvals waiting on you",
            "count": len(pending_approvals),
            "href": "/admin/agents",
            "scope": "open requests",
        },
        {
            "key": "unanswered_kills",
            "label": "Workers told to stop",
            "count": len(unanswered_kills),
            "href": "/admin/agents",
            "scope": "asked, not yet confirmed",
        },
        {
            "key": "open_run_controls",
            "label": "Run controls awaiting an agent",
            "count": open_controls,
            "href": "/admin/run-controls",
            "scope": "asked, not yet answered",
        },
        {
            "key": "failing_automation_rules",
            "label": "Failing automation rules",
            "count": len(failing_rules),
            "href": "/admin/automation",
            "scope": "rules with a recorded failure",
        },
        {
            "key": "failing_webhooks",
            "label": "Failing webhooks",
            "count": len(failing_webhooks),
            "href": "/admin/webhooks",
            "scope": "endpoints with a delivery failure",
        },
        {
            "key": "budget_exhaustions",
            "label": "Budget ceilings hit",
            "count": budget_exhaustions,
            "href": "/admin/agents",
            "scope": f"in the last {window_hours}h",
        },
        {
            "key": "security_refusals",
            "label": "Boundary refusals",
            "count": sum(refusals.values()),
            "href": "/admin/security",
            "scope": f"in the last {window_hours}h",
        },
    ]
    projection = {
        "schema": SCHEMA,
        "window_hours": window_hours,
        "since": since,
        "signals": signals,
        "total": sum(cast(int, signal["count"]) for signal in signals),
        # Kept apart from the rolled-up total so the card can break the refusals
        # down without a second query.
        "refusals_by_verb": refusals,
    }
    # The ranked queue is an extension of this projection, not a second source
    # of truth. Callers that have an actor get both the count card and its
    # actor-filtered "what next" rows from one request clock.
    if actor is not None:
        ranking = build_attention_ranking(
            conn,
            signals=ranking_signals,
            actor=actor,
            window_hours=window_hours,
            now=current,
            limit=ranking_limit,
        )
        projection["now"] = public_attention_ranking(conn, ranking, actor=actor)
    return projection


# ---------------------------------------------------------------------------
# Ranked "Now" queue extension
# ---------------------------------------------------------------------------
#
# The count card above tells an operator *that* something needs them; this
# projection tells them *what* needs them next. It is still read-time, still
# owns no tables, and still points at the surfaces that own each fact.

# Closed signal vocabulary. Each name maps to a collector below. Unknown types
# fail closed at the boundary so the queue can never be widened by a typo.
AttentionSignal = Literal[
    "pending_approval",
    "claim_needs_attention",
    "open_blocker",
    "open_run_control",
    "failing_automation_rule",
    "failing_webhook",
    "budget_exhaustion",
    "security_refusal",
]
Severity = Literal["critical", "high", "medium", "low"]
NextAction = Literal["agent-command", "operator-link", "none"]
SourceKind = Literal[
    "approval_request",
    "issue",
    "run_control",
    "automation_rule",
    "webhook",
    "activity_event",
]

RANKING_SIGNALS: tuple[AttentionSignal, ...] = (
    "pending_approval",
    "claim_needs_attention",
    "open_blocker",
    "open_run_control",
    "failing_automation_rule",
    "failing_webhook",
    "budget_exhaustion",
    "security_refusal",
)

# Closed severity vocabulary. Lower numeric rank = more urgent.
SEVERITIES: tuple[Severity, ...] = ("critical", "high", "medium", "low")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class AttentionRankItem:
    """One row in the ranked attention queue.

    ``next_action`` is one of three classes:
      - "agent-command": the current actor is authorized to execute ``command``.
      - "operator-link": a human follows ``link`` and decides.
      - "none": informational; no safe next action is known.
    This slice labels actions; it never executes them.
    """

    signal: AttentionSignal
    severity: Severity
    source_kind: SourceKind
    source_id: int
    owner_id: int | None
    reason: str
    freshness: str
    examined: int
    total: int
    next_action: NextAction
    command: str | None = None
    link: str | None = None
    source_link: str | None = None


def _format_issue_ref(issue: dict) -> str:
    return issue.get("key") or f"#{issue['id']}"


def _has_admin_visibility(actor: dict) -> bool:
    """Match the admin role + token-scope boundary owning private signals."""
    return identity.is_admin(actor) and identity.token_has_scope(
        actor, tokens.ADMIN_SCOPE
    )


def _collect_pending_approvals(
    conn: sqlite3.Connection, *, actor: dict
) -> tuple[list[AttentionRankItem], int, int]:
    if not _has_admin_visibility(actor):
        return [], 0, 0
    rows = approvals.list_requests(conn, state="pending", limit=200)
    items: list[AttentionRankItem] = []
    examined = len(rows)
    for req in rows:
        items.append(
            AttentionRankItem(
                signal="pending_approval",
                severity="high",
                source_kind="approval_request",
                source_id=req.id,
                owner_id=req.requested_by,
                reason=f"{req.action_kind} on {req.target_kind} #{req.target_id}",
                freshness=req.created_at,
                examined=examined,
                total=examined,
                next_action="operator-link",
                link="/admin/agents",
                source_link="/admin/agents",
            )
        )
    return items, examined, examined


def _collect_claims_needing_attention(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> tuple[list[AttentionRankItem], int, int]:
    work = fleet_work.build_active_work(
        conn,
        agent_id=None if _has_admin_visibility(actor) else actor["id"],
        attention_state=fleet_work.NEEDS_ATTENTION,
        limit=200,
        now=now,
    )
    items: list[AttentionRankItem] = []
    examined = work["examined_count"]
    total = work["visible_total"]
    for work_item in work["items"]:
        reasons = work_item.get("attention_reasons") or ["needs attention"]
        holder_id = work_item["holder"]["id"]
        issue = work_item["issue"]
        run_id = work_item["run"]["run_id"]
        can_check_in = (
            work_item["lease"]["claim_state"] == "active"
            and run_id is not None
            and any(
                reason in {"checkin_missing", "checkin_stale"} for reason in reasons
            )
        )
        items.append(
            AttentionRankItem(
                signal="claim_needs_attention",
                severity="high",
                source_kind="issue",
                source_id=issue["id"],
                owner_id=holder_id,
                reason="; ".join(reasons),
                freshness=work_item["lease"]["claimed_at"],
                examined=examined,
                total=total,
                next_action="agent-command" if can_check_in else "operator-link",
                command="heartbeat_agent_run" if can_check_in else None,
                link=f"/admin/agents/runs?agent_id={holder_id}",
                source_link=f"/aegis/issues/{issue['id']}/history",
            )
        )
    return items, examined, total


def _collect_open_blockers(
    conn: sqlite3.Connection,
    *,
    actor: dict,
) -> tuple[list[AttentionRankItem], int, int]:
    """Issues that are blocked by an open issue, across all visible projects.

    Admin callers see everything; the visibility clause is still applied so a
    future non-admin consumer cannot leak private-project issues.
    """
    visible_project_ids = access.visible_project_filter(conn, actor)
    candidates = issues.list_issues(
        conn,
        include_archived=False,
        visible_project_ids=visible_project_ids,
        limit=200,
    )
    total_candidates = issues.count_issues(
        conn,
        include_archived=False,
        visible_project_ids=visible_project_ids,
    )
    items: list[AttentionRankItem] = []
    for issue in candidates:
        blockers = dependencies.open_blockers(conn, issue["id"], actor=actor)
        if not blockers:
            continue
        owner_id = (
            issue["assignee_id"]
            if issue["assignee_id"] is not None
            else issue["created_by"]
        )
        freshness = issue.get("updated_at") or issue["created_at"]
        items.append(
            AttentionRankItem(
                signal="open_blocker",
                severity="high",
                source_kind="issue",
                source_id=issue["id"],
                owner_id=owner_id,
                reason=(
                    "blocked by "
                    + ", ".join(
                        (
                            f"{_format_issue_ref(b)} · {b['title']}"
                            if b.get("title")
                            else _format_issue_ref(b)
                        )
                        for b in blockers
                    )
                ),
                freshness=freshness,
                examined=len(candidates),
                total=total_candidates,
                next_action="operator-link",
                link=f"/aegis/issues/{issue['id']}",
                source_link=f"/aegis/issues/{issue['id']}/history",
            )
        )
    return items, len(candidates), total_candidates


def _collect_open_run_controls(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> tuple[list[AttentionRankItem], int, int]:
    rows = run_controls.list_rows(
        conn,
        agent_id=None if _has_admin_visibility(actor) else actor["id"],
        state=run_controls.STATE_FILTER_OPEN,
        limit=run_controls.MAX_LIST_LIMIT,
        now_stamp=run_controls.stamp(now),
    )
    items: list[AttentionRankItem] = []
    examined = len(rows)
    for row in rows:
        run_link = f"/aegis/activity/runs/{quote(row['run_id'], safe='')}/lineage"
        control_query = urlencode({"run_id": row["run_id"]})
        items.append(
            AttentionRankItem(
                signal="open_run_control",
                severity="high",
                source_kind="run_control",
                source_id=row["id"],
                owner_id=row["agent_id"],
                reason=f"{row['kind']} control on run {row['run_id']}",
                freshness=row["created_at"],
                examined=examined,
                total=examined,
                next_action="agent-command",
                command="acknowledge_run_control",
                link=f"/admin/run-controls?{control_query}",
                source_link=run_link,
            )
        )
    return items, examined, examined


def _collect_failing_automation_rules(
    conn: sqlite3.Connection, *, actor: dict
) -> tuple[list[AttentionRankItem], int, int]:
    if not _has_admin_visibility(actor):
        return [], 0, 0
    rows = automation.list_rules(conn, failing_only=True)
    items: list[AttentionRankItem] = []
    examined = len(rows)
    for row in rows:
        items.append(
            AttentionRankItem(
                signal="failing_automation_rule",
                severity="high",
                source_kind="automation_rule",
                source_id=row["id"],
                owner_id=row["created_by"],
                reason=f"last error: {row.get('last_error') or 'unknown'}",
                freshness=row.get("last_error_at") or row["created_at"],
                examined=examined,
                total=examined,
                next_action="operator-link",
                link="/admin/automation",
                source_link="/admin/automation",
            )
        )
    return items, examined, examined


def _collect_failing_webhooks(
    conn: sqlite3.Connection, *, actor: dict
) -> tuple[list[AttentionRankItem], int, int]:
    if not _has_admin_visibility(actor):
        return [], 0, 0
    rows = [hook for hook in webhooks.list_webhooks(conn) if hook["failure_count"]]
    items: list[AttentionRankItem] = []
    examined = len(rows)
    for wh in rows:
        severity: Severity = "critical" if wh["failure_count"] >= 5 else "high"
        items.append(
            AttentionRankItem(
                signal="failing_webhook",
                severity=severity,
                source_kind="webhook",
                source_id=wh["id"],
                owner_id=wh["created_by"],
                reason=(
                    f"{wh['failure_count']} consecutive failures: "
                    f"{wh['last_error'] or 'unknown'}"
                ),
                freshness=wh["last_attempt_at"] or wh["created_at"],
                examined=examined,
                total=examined,
                next_action="operator-link",
                link="/admin/webhooks",
                source_link="/admin/webhooks",
            )
        )
    return items, examined, examined


def _collect_budget_exhaustions(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
) -> tuple[list[AttentionRankItem], int, int]:
    if not _has_admin_visibility(actor):
        return [], 0, 0
    since = _since_text(window_hours=window_hours, now=now)
    rows = conn.execute(
        "SELECT id, actor_id, target_kind, target_id, created_at, detail "
        "FROM activity "
        "WHERE verb = ? AND created_at >= ? AND imported_at IS NULL "
        "ORDER BY id DESC LIMIT 200",
        (budgets.VERB_BUDGET_EXHAUSTED, since),
    ).fetchall()
    items: list[AttentionRankItem] = []
    examined = len(rows)
    for row in rows:
        source_query = urlencode(
            {
                "actor": row["actor_id"],
                "verb": budgets.VERB_BUDGET_EXHAUSTED,
                "kind": row["target_kind"],
                "target": row["target_id"],
            }
        )
        items.append(
            AttentionRankItem(
                signal="budget_exhaustion",
                severity="medium",
                source_kind="activity_event",
                source_id=row["id"],
                owner_id=row["actor_id"],
                reason=row["detail"] or "budget exhausted",
                freshness=row["created_at"],
                examined=examined,
                total=examined,
                next_action="operator-link",
                link="/admin/agents",
                source_link=f"/aegis/activity?{source_query}",
            )
        )
    return items, examined, examined


def _collect_security_refusals(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
) -> tuple[list[AttentionRankItem], int, int]:
    if not _has_admin_visibility(actor):
        return [], 0, 0
    since = _since_text(window_hours=window_hours, now=now)
    rows = security_events.list_failures(conn, since=since, limit=200)
    items: list[AttentionRankItem] = []
    examined = len(rows)
    for row in rows:
        source_query = urlencode(
            {
                "actor": row["actor_id"],
                "verb": row["verb"],
                "kind": row["target_kind"],
                "target": row["target_id"],
            }
        )
        items.append(
            AttentionRankItem(
                signal="security_refusal",
                severity="medium",
                source_kind="activity_event",
                source_id=row["id"],
                owner_id=row["actor_id"],
                reason=f"{row['verb']}: {row['detail'] or 'boundary refusal'}",
                freshness=row["created_at"],
                examined=examined,
                total=examined,
                next_action="operator-link",
                link="/admin/security",
                source_link=f"/aegis/activity?{source_query}",
            )
        )
    return items, examined, examined


def _rank(items: list[AttentionRankItem]) -> list[AttentionRankItem]:
    """Most urgent first; within a severity, oldest first."""
    return sorted(
        items,
        key=lambda i: (SEVERITY_RANK[i.severity], i.freshness),
    )


def _agent_command_authorized(item: AttentionRankItem, actor: dict) -> bool:
    """Whether ``actor`` may execute an agent-command on this row.

    The command class is only offered when the caller is the party assigned to
    act. For a claim-needs-attention row that party is the agent holder
    (``owner_id``); admins and even the issue creator see a link instead. This
    keeps the label honest: the slice never executes a command, and it only
    labels one when the caller could actually perform it through the existing
    agent API.
    """
    if item.owner_id is None or actor.get("id") != item.owner_id:
        return False
    token_id = actor.get("_token_id")
    scopes = actor.get("_token_scopes")
    return (
        bool(actor.get("is_agent"))
        and isinstance(token_id, int)
        and not isinstance(token_id, bool)
        and token_id > 0
        and scopes is not None
        and bool(
            {
                tokens.ISSUE_WRITE_SCOPE,
                tokens.DOCS_WRITE_SCOPE,
                tokens.ADMIN_SCOPE,
            }.intersection(scopes)
        )
    )


def resolve_owner_name(conn: sqlite3.Connection, owner_id: int | None) -> str | None:
    if owner_id is None:
        return None
    user = users.get_user(conn, owner_id)
    return user["name"] if user else None


def to_public_rank_item(
    conn: sqlite3.Connection,
    item: AttentionRankItem,
    *,
    actor: dict,
) -> dict:
    """Render an AttentionRankItem for the API, applying actor-scoped command
    authorization at the boundary."""
    next_action = item.next_action
    command = item.command
    if next_action == "agent-command" and not _agent_command_authorized(item, actor):
        next_action = "operator-link"
        command = None
    return {
        "signal": item.signal,
        "severity": item.severity,
        "source_kind": item.source_kind,
        "source_id": item.source_id,
        "owner_id": item.owner_id,
        "owner_name": resolve_owner_name(conn, item.owner_id),
        "reason": item.reason,
        "freshness": item.freshness,
        "examined": item.examined,
        "total": item.total,
        "next_action": next_action,
        "command": command,
        "link": item.link,
        "source_link": item.source_link,
    }


def public_attention_ranking(
    conn: sqlite3.Connection,
    ranking: dict,
    *,
    actor: dict,
) -> dict:
    """Apply owner resolution and command gates once for every adapter."""
    return {
        **ranking,
        "items": [
            to_public_rank_item(conn, item, actor=actor) for item in ranking["items"]
        ],
    }


def build_attention_ranking(
    conn: sqlite3.Connection,
    *,
    signals: set[str] | None = None,
    actor: dict,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
    limit: int = 20,
) -> dict:
    """Build the ranked "Now" attention queue.

    ``signals`` restricts the result to a subset of the known signal vocabulary.
    An unknown signal type raises ``ValueError`` so the boundary can fail closed
    with a 422.

    The returned dict includes:
      - items: ranked list of ``AttentionRankItem`` dicts
      - examined: total candidates examined across all signals
      - total: total candidates considered across all signals
      - signals: the signal types that contributed to this response
    """
    if isinstance(window_hours, bool) or not isinstance(window_hours, int):
        raise ValueError("window_hours must be an integer")
    if window_hours < 1:
        raise ValueError("window_hours must be positive")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if signals is None:
        signals = set(RANKING_SIGNALS)
    unknown = signals - set(RANKING_SIGNALS)
    if unknown:
        raise ValueError(f"unknown signal types: {', '.join(sorted(unknown))}")

    all_items: list[AttentionRankItem] = []
    examined_total = 0
    candidate_total = 0

    current = _normalized_now(now)
    for signal in sorted(signals):
        if signal == "pending_approval":
            items, examined, total = _collect_pending_approvals(conn, actor=actor)
        elif signal == "claim_needs_attention":
            items, examined, total = _collect_claims_needing_attention(
                conn, actor=actor, now=current
            )
        elif signal == "open_blocker":
            items, examined, total = _collect_open_blockers(conn, actor=actor)
        elif signal == "open_run_control":
            items, examined, total = _collect_open_run_controls(
                conn, actor=actor, now=current
            )
        elif signal == "failing_automation_rule":
            items, examined, total = _collect_failing_automation_rules(
                conn, actor=actor
            )
        elif signal == "failing_webhook":
            items, examined, total = _collect_failing_webhooks(conn, actor=actor)
        elif signal == "budget_exhaustion":
            items, examined, total = _collect_budget_exhaustions(
                conn, actor=actor, window_hours=window_hours, now=current
            )
        else:
            items, examined, total = _collect_security_refusals(
                conn, actor=actor, window_hours=window_hours, now=current
            )
        all_items.extend(items)
        examined_total += examined
        candidate_total += total

    ranked = _rank(all_items)
    return {
        "items": ranked[:limit],
        "examined": examined_total,
        "total": candidate_total,
        "signals": sorted(signals),
        "returned": min(len(ranked), limit),
        "limit": limit,
        "clipped": len(ranked) > limit,
    }
