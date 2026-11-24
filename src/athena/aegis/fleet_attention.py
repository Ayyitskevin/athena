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

from athena.aegis import (
    automation,
    dependencies,
    fence_overlaps,
    fleet_work,
    held_reports,
    issues,
    run_state,
    work_progress,
)
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
    counts: dict | None = None,
) -> dict:
    """Count what needs a human, and say where each number lives.

    Admin-only by the caller's contract: every input here is already an
    admin-scoped read (active work, automation rules, webhook health, the approval
    queue, the worker registry, refusal events), and aggregating them does not
    widen any of them.

    The counts and their ``total`` are :func:`needs_you`'s (U6). ``counts`` is a
    ``needs_you`` result this request already computed for the same actor and
    window (the nav badge's), reused so the card and the badge in one response
    are the same read, made once.
    """
    current = _normalized_now(now)
    projection = (
        dict(counts)
        if counts is not None
        else needs_you(conn, window_hours=window_hours, now=current, actor=actor)
    )
    # The ranked queue is an extension of this projection, not a second source
    # of truth. Callers that have an actor get both the count card and its
    # actor-filtered "what next" rows from one request clock.
    if actor is not None:
        # Alive is not progress: expired leases, in-progress work with no recorded
        # work, and the oldest request waiting on the operator. Kept out of the
        # signal total on purpose — each lane carries its own count and meaning.
        projection["progress"] = work_progress.build_progress(
            conn, actor=actor, now=current
        )
        waiting = projection["progress"].get("oldest_waiting_on_operator")
        if waiting is not None:
            projection["progress"]["oldest_waiting_on_operator"] = _worded_waiting(
                conn, waiting
            )
        ranking = build_attention_ranking(
            conn,
            signals=ranking_signals,
            actor=actor,
            window_hours=window_hours,
            now=current,
            limit=ranking_limit,
        )
        projection["now"] = public_attention_ranking(conn, ranking, actor=actor)
        projection["now"]["rows"] = cockpit_rows(conn, projection["now"]["items"])
    return projection


def needs_you(
    conn: sqlite3.Connection,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
    actor: dict | None = None,
) -> dict:
    """The one "needs you" number, with the signals it sums.

    ``total`` is everything asking for a human (``docs/EXCEPTION_SURFACES.md``):
    the sum of the signal counts below. It is the only number the Intervene badge
    and the dashboard's fleet-attention card show as "needs you"; both read it
    from here, for the same actor, and a page renders both from one call. The
    card's live refresh carries the badge with it (an out-of-band swap), so a
    refreshed card never sits beside a stale badge. The unread inbox is not in
    it: an inbox row says something happened, not that a decision waits, and the
    surfaces show it beside this number, never added to it.

    Admin-only by the caller's contract, like :func:`build_attention`.
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
    # Questions an agent asked that no person has answered since: the ranked
    # queue's only ``critical`` kind, so they are in the number (U7). Counted by
    # the operator rule the ranked rows and the questions panel use.
    questions = run_state.open_questions(
        conn, actor=actor, now=current, limit=1, with_answers=True
    )
    # Completions that reported HOLD or BLOCKED and that nothing has answered
    # (F15): a decision about the work, waiting on whoever answers for it.
    held = held_reports.open_held_reports(conn, actor=actor, limit=1)
    unanswered_kills = workers.count_unanswered(conn, now=current)
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
            "WHERE verb IN (?, ?) AND created_at >= ? AND imported_at IS NULL",
            (*budgets.EXHAUSTION_VERBS, since),
        ).fetchone()["n"]
    )

    # Hand-offs into silence. With no actor this is the admin nav-badge path, whose
    # caller contract is admin-scoped like every other input here; with one, the
    # actor's own visibility applies to the count.
    unclaimed = work_progress.unclaimed_delegations(
        conn, actor=actor, all_projects=actor is None, now=current, limit=1
    )
    # Live leases whose file fences overlap. A count here; each pair is a ranked
    # row in the Now queue (not a composed lane, per the card's lane rule).
    overlaps = fence_overlaps.find_overlaps(conn, actor=actor, now=current, limit=1)

    signals = [
        {
            "key": "open_questions",
            "label": "Questions waiting on you",
            "count": questions["total"] - questions["answered_total"],
            "href": "/admin/agents#questions",
            "scope": (
                "standing; blocking questions from yielded claims nobody has "
                "resumed, less those a person has answered since"
            ),
        },
        {
            "key": "held_reports",
            "label": "Reports of HOLD or BLOCKED",
            "count": held["total"],
            "href": "/aegis/dashboard#fleet-attention-heading",
            "scope": (
                "standing; completions that reported HOLD or BLOCKED on open "
                "issues, with no answer and no new claim since; each is in the Now queue"
            ),
        },
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
            "scope": (
                "standing queue; up to 200 newest pending requests read; "
                "input completeness unknown"
            ),
        },
        {
            "key": "unanswered_kills",
            "label": "Workers told to stop",
            "count": unanswered_kills,
            "href": "/admin/agents",
            "scope": "all registered workers with an unconfirmed stop request",
        },
        {
            "key": "open_run_controls",
            "label": "Run controls awaiting an agent",
            "count": open_controls,
            "href": "/admin/run-controls",
            "scope": "asked, not yet answered",
        },
        {
            "key": "delegation_unclaimed",
            "label": "Delegations not picked up",
            "count": unclaimed["visible_total"],
            # The agents page lists each agent's delegation inbox: the owning
            # surface for who was handed what.
            "href": "/admin/agents",
            "scope": (
                f"standing; delegated over {work_progress.DELEGATION_UNCLAIMED_HOURS}h "
                "ago, delegate has neither claimed nor recorded work since"
            ),
        },
        {
            "key": "fence_overlaps",
            "label": "Overlapping file fences",
            "count": overlaps["total"],
            "href": "/admin/agents/runs",
            "scope": (
                "pairs of live leases whose declared paths overlap; each pair is "
                "in the Now queue with both issues. Shown only, never blocked"
            ),
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
    return {
        "schema": SCHEMA,
        "window_hours": window_hours,
        "since": since,
        "signals": signals,
        "total": sum(cast(int, signal["count"]) for signal in signals),
        # Kept apart from the rolled-up total so the card can break the refusals
        # down without a second query.
        "refusals_by_verb": refusals,
    }


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
    "awaiting_input",
    "held_report",
    "fence_overlap",
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
    "awaiting_input",
    "held_report",
    "fence_overlap",
    "pending_approval",
    "claim_needs_attention",
    "open_blocker",
    "open_run_control",
    "failing_automation_rule",
    "failing_webhook",
    "budget_exhaustion",
    "security_refusal",
)

#: The claim reasons that only say a heartbeat is overdue. A claim with no
#: other reason is ranked ``medium``: below a pending decision (U7).
HEARTBEAT_REASONS = frozenset({"checkin_missing", "checkin_stale", "run_untagged"})
#: Reasons that are not yet a decision. A claim whose only trouble is among
#: these ranks ``medium``: a missed heartbeat, or a holder that keeps renewing
#: with no recorded work (C7), which is alive and may only be quiet.
QUIET_REASONS = HEARTBEAT_REASONS | {"no_progress"}

#: How an answered question's reason opens: it waits on the agent now (U7).
ANSWERED_WAITING = "answered, waiting on the agent"

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


def _collect_awaiting_input(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> tuple[list[AttentionRankItem], int, int]:
    """Runs that stopped and asked a human: open blocking questions on issues
    this actor can see. Ranked ``critical`` — above stale claims (``high``) and
    the quiet event signals (``medium``) — because a run that asked is waiting on
    a person, not on the clock. Pending approvals keep their own signal.

    A question someone other than the asker has answered since it was asked (the
    ready lane's rule, ``ready_queue.answered_sql``) stays in the queue under the
    same signal, since only the agent's resume closes it, but it no longer waits
    on a human: it ranks ``low``, its reason opens "answered, waiting on the
    agent" and it offers no next action (U7)."""
    lane = run_state.open_questions(
        conn, actor=actor, now=now, limit=200, with_answers=True
    )
    operator = _has_admin_visibility(actor)
    items: list[AttentionRankItem] = []
    examined = len(lane["items"])
    for question in lane["items"]:
        issue = question["issue"]
        text = question["blocking_question"]
        short = text if len(text) <= 160 else text[:157] + "..."
        asked = f"{question['asked_by']['name']} asks: {short}"
        answered = question["answered"]
        items.append(
            AttentionRankItem(
                signal="awaiting_input",
                severity="low" if answered else "critical",
                source_kind="issue",
                source_id=issue["id"],
                owner_id=question["asked_by"]["id"],
                reason=f"{ANSWERED_WAITING}: {asked}" if answered else asked,
                freshness=question["asked_at"],
                examined=examined,
                total=lane["total"],
                next_action="none" if answered else "operator-link",
                link=f"/aegis/issues/{issue['id']}"
                if answered or not operator
                else "/admin/agents#questions",
                source_link=f"/aegis/issues/{issue['id']}",
            )
        )
    return items, examined, lane["total"]


def _collect_held_reports(
    conn: sqlite3.Connection, *, actor: dict
) -> tuple[list[AttentionRankItem], int, int]:
    """Completions that reported HOLD or BLOCKED and that nothing has answered
    (``held_reports``). An admin sees every one it may read. Anyone else sees
    the ones it answers for: on issues it is the assignee of or delegated to
    the reporter, never its own report (F15). Ranked ``high``, with the claims
    that need a decision: the work stopped and says why, and what happens to it
    is somebody's call. The row leaves when that somebody acts on the issue."""
    lane = held_reports.open_held_reports(
        conn, actor=actor, accountable_only=not _has_admin_visibility(actor)
    )
    items: list[AttentionRankItem] = []
    examined = len(lane["items"])
    for report in lane["items"]:
        issue = report["issue"]
        items.append(
            AttentionRankItem(
                signal="held_report",
                severity="high",
                source_kind="issue",
                source_id=issue["id"],
                owner_id=report["reported_by"]["id"],
                reason=f"reported {report['verdict']}; nothing has answered it",
                freshness=report["reported_at"],
                examined=examined,
                total=lane["total"],
                next_action="operator-link",
                link=f"/aegis/issues/{issue['id']}",
                source_link=f"/aegis/issues/{issue['id']}/history",
            )
        )
    return items, examined, lane["total"]


def _collect_fence_overlaps(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> tuple[list[AttentionRankItem], int, int]:
    """One row per pair of live leases whose file fences overlap, among issues
    this actor can see. The row links both issues (``source_link`` and ``link``)
    and names both holders. ``high``: two agents may be editing the same files.
    Shown only; the claim command's check stays the only gate."""
    found = fence_overlaps.find_overlaps(conn, actor=actor, now=now)
    items: list[AttentionRankItem] = []
    for pair in found["pairs"]:
        a, b = pair["a"], pair["b"]
        a_label = a["issue"]["key"] or f"#{a['issue']['id']}"
        b_label = b["issue"]["key"] or f"#{b['issue']['id']}"
        items.append(
            AttentionRankItem(
                signal="fence_overlap",
                severity="high",
                source_kind="issue",
                source_id=a["issue"]["id"],
                owner_id=a["holder"]["id"],
                reason=(
                    f"fences overlap: {a['holder']['name']} on {a_label} "
                    f"({a['path']}) and {b['holder']['name']} on {b_label} "
                    f"({b['path']}); {pair['basis']}"
                ),
                freshness=_observed_stamp(now),
                examined=found["examined_leases"],
                total=found["total"],
                next_action="operator-link",
                link=f"/aegis/issues/{b['issue']['id']}",
                source_link=f"/aegis/issues/{a['issue']['id']}",
            )
        )
    return items, found["examined_leases"], found["total"]


def _observed_stamp(now: datetime | None) -> str:
    """The read's own clock, as the row's freshness: an overlap is a standing
    condition observed now, not an event with its own time."""
    return _normalized_now(now).strftime("%Y-%m-%d %H:%M:%S")


def _collect_claims_needing_attention(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> tuple[list[AttentionRankItem], int, int]:
    # An admin sees the fleet. Anyone else sees the claims it answers for: the
    # leases it holds, and those on issues it may see and is the assignee of or
    # delegated to the holder (C4), so a lapse reaches whoever is accountable.
    work = fleet_work.build_active_work(
        conn,
        viewer=None if _has_admin_visibility(actor) else actor,
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
                # A claim whose only trouble is a missed heartbeat is not a
                # decision: it ranks under every pending approval and question.
                # Any other reason (a lapsed lease, a paused holder, ...) keeps
                # it ``high`` (U7).
                severity="medium" if set(reasons) <= QUIET_REASONS else "high",
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
        "SELECT id, actor_id, verb, target_kind, target_id, created_at, detail "
        "FROM activity "
        "WHERE verb IN (?, ?) AND created_at >= ? AND imported_at IS NULL "
        "ORDER BY id DESC LIMIT 200",
        (*budgets.EXHAUSTION_VERBS, since),
    ).fetchall()
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


# ---------------------------------------------------------------------------
# How the cockpit words a ranked row
# ---------------------------------------------------------------------------
#
# The API's rows keep their codes (``signal``, the claim's reason codes), which
# conductors read. The operator reads words: what kind of thing it is, the issue
# by its key and title, and what is wrong in a sentence (U7). This is wording
# only; it ranks, filters and decides nothing.

SIGNAL_WORDS: dict[str, str] = {
    "awaiting_input": "Question",
    "held_report": "Report",
    "fence_overlap": "Overlapping fences",
    "pending_approval": "Approval",
    "claim_needs_attention": "Claim",
    "open_blocker": "Blocked",
    "open_run_control": "Run control",
    "failing_automation_rule": "Automation rule failing",
    "failing_webhook": "Webhook failing",
    "budget_exhaustion": "Budget ceiling hit",
    "security_refusal": "Boundary refusal",
}

#: Every reason ``fleet_work._item`` can give, in words.
CLAIM_REASON_WORDS: dict[str, str] = {
    "issue_archived": "the issue was archived while claimed",
    "issue_done": "the issue is done but still claimed",
    "holder_paused": "the holder is paused",
    "holder_read_only": "the holder is read-only",
    "holder_ineligible": "the holder may not work this issue",
    "no_live_issue_write_token": "the holder has no live token that can write issues",
    "lease_expired": "the lease expired",
    "no_progress": "keeps renewing with no work of its own recorded on the issue",
    "run_untagged": "never tagged with a run, so it cannot check in",
    "checkin_missing": "missed its check-in: nothing heard since the claim",
    "checkin_stale": "missed its check-in: the last one is overdue",
    "visible_open_blockers": "blocked by an open issue",
    "open_claim_handoff": "a handoff is open",
}


def _words(code: str) -> str:
    return code.replace("_", " ").replace(".", " ")


def _action_words(action_kind: str) -> str:
    """``issue.close`` reads "close the issue"."""
    noun, _, verb = str(action_kind).partition(".")
    return f"{_words(verb)} the {_words(noun)}" if verb else _words(noun)


def _worded_waiting(conn: sqlite3.Connection, waiting: dict) -> dict:
    """The progress lane's oldest pending approval, with what it would act on
    (the issue's key, title and link) and a sentence, for the cockpit (U7)."""
    subject = None
    if waiting.get("target_kind") == "issue":
        subject = _issue_subjects(conn, {int(waiting["target_id"])}).get(
            int(waiting["target_id"])
        )
    owner = resolve_owner_name(conn, waiting.get("requested_by")) or "An agent"
    return {
        **waiting,
        "subject": subject,
        "headline": f"{owner} asks to {_action_words(waiting['action_kind'])}",
    }


def _issue_subjects(conn: sqlite3.Connection, issue_ids: set[int]) -> dict[int, dict]:
    if not issue_ids:
        return {}
    ordered = sorted(issue_ids)
    placeholders = ",".join("?" for _ in ordered)
    rows = conn.execute(
        "SELECT i.id, i.title, i.project_seq, p.key AS project_key FROM issues i "
        f"LEFT JOIN projects p ON p.id = i.project_id WHERE i.id IN ({placeholders})",
        ordered,
    ).fetchall()
    subjects = {}
    for row in rows:
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        subjects[int(row["id"])] = {
            "key": key,
            "title": row["title"],
            "href": f"/aegis/issues/{row['id']}",
        }
    return subjects


def cockpit_rows(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """One worded row per public ranked item, in the same order: ``kind`` (the
    signal in words), ``subject`` (the issue's key, title and link, when the row
    is about an issue) and ``headline`` (what is wrong, in words).

    Called only with items the actor's ranking already returned, so it reads the
    key and title of issues the actor was already shown, and nothing more."""
    approval_ids = [
        int(item["source_id"])
        for item in items
        if item["source_kind"] == "approval_request"
    ]
    approvals_by_id: dict[int, sqlite3.Row] = {}
    if approval_ids:
        placeholders = ",".join("?" for _ in approval_ids)
        approvals_by_id = {
            int(row["id"]): row
            for row in conn.execute(
                "SELECT id, action_kind, target_kind, target_id "
                f"FROM approval_requests WHERE id IN ({placeholders})",
                approval_ids,
            )
        }
    issue_ids = {
        int(item["source_id"]) for item in items if item["source_kind"] == "issue"
    }
    issue_ids |= {
        int(row["target_id"])
        for row in approvals_by_id.values()
        if row["target_kind"] == "issue"
    }
    subjects = _issue_subjects(conn, issue_ids)

    rows = []
    for item in items:
        signal = item["signal"]
        owner = item.get("owner_name") or "someone"
        subject = None
        headline = item["reason"]
        if item["source_kind"] == "issue":
            subject = subjects.get(int(item["source_id"]))
        if signal == "claim_needs_attention":
            reasons = [r for r in item["reason"].split("; ") if r]
            headline = f"Held by {owner}: " + "; ".join(
                CLAIM_REASON_WORDS.get(code, _words(code)) for code in reasons
            )
        elif signal == "pending_approval":
            approval = approvals_by_id.get(int(item["source_id"]))
            if approval is not None:
                headline = f"{owner} asks to {_action_words(approval['action_kind'])}"
                if approval["target_kind"] == "issue":
                    subject = subjects.get(int(approval["target_id"]))
        elif signal == "held_report":
            verdict = item["reason"].partition(";")[0].removeprefix("reported ")
            headline = f"{owner} reported {verdict}: decide what happens to the work"
        elif signal == "budget_exhaustion":
            headline = f"{owner} hit an action budget ceiling"
        elif signal == "security_refusal":
            verb, _, detail = item["reason"].partition(": ")
            headline = f"{_words(verb)}: {detail}" if detail else _words(verb)
        rows.append(
            {
                "kind": SIGNAL_WORDS.get(signal, _words(signal)),
                "subject": subject,
                "headline": headline,
            }
        )
    return rows


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
      - total: total candidates considered across all signals, not a full input
        population; a collector's input bound is separate from output clipping
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
        if signal == "awaiting_input":
            items, examined, total = _collect_awaiting_input(
                conn, actor=actor, now=current
            )
        elif signal == "held_report":
            items, examined, total = _collect_held_reports(conn, actor=actor)
        elif signal == "fence_overlap":
            items, examined, total = _collect_fence_overlaps(
                conn, actor=actor, now=current
            )
        elif signal == "pending_approval":
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
