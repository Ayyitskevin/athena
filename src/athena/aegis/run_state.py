"""One derived state per run, read from facts Athena already stores.

A run is an agent's run id (the ``X-Athena-Run`` its writes carry). Its state is
computed at read time from rows that already exist — the activity trail, the
check-in table, open claim handoffs and pending approvals — and is never stored:

``awaiting_input``
    The run asked a human something that is still open: it yielded a claim with
    a blocking question nobody has resumed, or it raised an approval that is still
    pending.
``complete``
    The run's latest claim act was ``complete_claim`` and it holds no live lease
    it claimed under this run.
``active``
    The run recorded real work — the progress view's closed work-verb list
    (``work_progress.SUBSTANTIVE_WORK_VERBS``) — within ``ACTIVE_WINDOW_MINUTES``.
``pinging``
    Only check-ins within that window: the run says it is alive, and nothing it
    did is on the trail.
``stale``
    None of those.

**Precedence is that order, and it is the point.** An open question outranks
everything, so a run that asked and kept typing still reads as waiting on a
human. A check-in can only ever produce ``pinging``: no heartbeat, however
frequent, makes a run ``active`` or clears a question. Only recorded work makes a
run active, and only an answer (a resumed handoff, a decided approval) clears
``awaiting_input``.

**Nothing enforces a response time.** These states say who is waiting on whom;
they set no deadline and change nothing.

A claim with no run id (an untagged claim) is its own run: its facts are the
holder's, scoped to that issue, and it has no check-ins to read.

Read-only. Visibility applies wherever rows are listed (questions lanes, the
ranked queue); a run's state for a claim is shown on surfaces that already gate
that claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3
from typing import Literal

from athena.aegis import work_progress
from athena.core import access

#: A run that recorded real work within this window is moving. Thirty minutes is
#: one default lease (``leases.DEFAULT_LEASE_SECONDS``): a run that holds work for
#: a whole default lease without recording any of it is not visibly moving, and
#: check-ins alone in that time read as ``pinging``.
ACTIVE_WINDOW_MINUTES = 30

RunState = Literal["awaiting_input", "complete", "active", "pinging", "stale"]
STATES: tuple[RunState, ...] = (
    "awaiting_input",
    "complete",
    "active",
    "pinging",
    "stale",
)

#: Each lane here is a short list with its own total.
LANE_LIMIT = 20

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_CLAIM_ACTS = ("claimed", "lease_renewed", "claim_completed", "claim_yielded")
_WORK_PLACEHOLDERS = ",".join("?" for _ in work_progress.SUBSTANTIVE_WORK_VERBS)
_ACT_PLACEHOLDERS = ",".join("?" for _ in _CLAIM_ACTS)
MEANING = (
    "derived at read time: awaiting_input (an open blocking question or pending "
    "approval from this run) > complete (latest claim act was complete_claim) > "
    f"active (recorded work in {ACTIVE_WINDOW_MINUTES} min) > pinging (check-ins "
    "only in that window) > stale. A check-in never makes a run active. Nothing "
    "enforces a response time."
)


def _now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime(_TS_FORMAT)


def derive_state(
    *,
    awaiting: bool,
    completed: bool,
    last_work_at: str | None,
    last_checkin_at: str | None,
    now: datetime,
) -> tuple[RunState, list[str]]:
    """The state and the facts that decided it, by the fixed precedence."""
    window_start = _stamp(_now(now) - timedelta(minutes=ACTIVE_WINDOW_MINUTES))
    if awaiting:
        return "awaiting_input", ["open_question_or_approval"]
    if completed:
        return "complete", ["latest_claim_act_completed"]
    if last_work_at is not None and last_work_at >= window_start:
        return "active", ["work_in_window"]
    if last_checkin_at is not None and last_checkin_at >= window_start:
        return "pinging", ["checkin_only_in_window"]
    return "stale", ["nothing_in_window"]


def _scope_sql(
    alias: str, *, run_id: str | None, issue_id: int | None
) -> tuple[str, list]:
    if run_id is not None:
        return f"{alias}.run_id = ?", [run_id]
    return f"{alias}.target_kind = 'issue' AND {alias}.target_id = ?", [issue_id]


def _facts(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    run_id: str | None,
    issue_id: int | None,
    now: datetime,
) -> dict:
    """Every fact the state reads, for one run (or one untagged claim). Native
    activity only: imported history never moves a run's state."""
    scope, scope_params = _scope_sql("a", run_id=run_id, issue_id=issue_id)
    work = conn.execute(
        "SELECT MAX(a.created_at) AS at FROM activity a "
        "WHERE a.actor_id = ? AND a.imported_at IS NULL AND a.target_kind = 'issue' "
        f"AND a.verb IN ({_WORK_PLACEHOLDERS}) AND {scope}",
        [agent_id, *work_progress.SUBSTANTIVE_WORK_VERBS, *scope_params],
    ).fetchone()["at"]
    last_act = conn.execute(
        "SELECT a.verb FROM activity a "
        "WHERE a.actor_id = ? AND a.imported_at IS NULL "
        f"AND a.verb IN ({_ACT_PLACEHOLDERS}) AND {scope} "
        "ORDER BY a.id DESC LIMIT 1",
        [agent_id, *_CLAIM_ACTS, *scope_params],
    ).fetchone()
    checkin = None
    if run_id is not None:
        row = conn.execute(
            "SELECT last_seen_at FROM agent_run_checkins "
            "WHERE agent_id = ? AND run_id = ?",
            (agent_id, run_id),
        ).fetchone()
        checkin = None if row is None else row["last_seen_at"]
    yscope, yparams = _scope_sql("ye", run_id=run_id, issue_id=issue_id)
    questions = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM issue_claim_handoffs h "
            "JOIN activity ye ON ye.id = h.yield_event_id "
            "WHERE h.resume_event_id IS NULL AND ye.imported_at IS NULL "
            "AND ye.actor_id = ? AND COALESCE(h.blocking_question, '') != '' "
            f"AND {yscope}",
            [agent_id, *yparams],
        ).fetchone()["n"]
    )
    approval_params: list[object]
    if run_id is not None:
        approval_scope, approval_params = "run_id = ?", [run_id]
    else:
        approval_scope = "target_kind = 'issue' AND target_id = ?"
        approval_params = [issue_id]
    approvals_pending = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM approval_requests "
            f"WHERE state = 'pending' AND requested_by = ? AND {approval_scope}",
            [agent_id, *approval_params],
        ).fetchone()["n"]
    )
    # A live lease claimed (or renewed) under this run means the run is holding
    # work again, whatever it completed before.
    holding = False
    if last_act is not None and last_act["verb"] == "claim_completed":
        hscope, hparams = _scope_sql("ca", run_id=run_id, issue_id=issue_id)
        holding = (
            conn.execute(
                "SELECT 1 FROM issue_leases l WHERE l.holder_id = ? "
                "AND l.expires_at > ? AND EXISTS (SELECT 1 FROM activity ca "
                "WHERE ca.target_kind = 'issue' AND ca.target_id = l.issue_id "
                "AND ca.actor_id = l.holder_id AND ca.imported_at IS NULL "
                f"AND ca.verb IN ('claimed', 'lease_renewed') AND {hscope} "
                "AND ca.created_at >= l.claimed_at) LIMIT 1",
                [agent_id, _stamp(now), *hparams],
            ).fetchone()
            is not None
        )
    return {
        "last_work_at": work,
        "last_claim_act": None if last_act is None else last_act["verb"],
        "last_checkin_at": checkin,
        "open_questions": questions,
        "pending_approvals": approvals_pending,
        "completed": (
            last_act is not None
            and last_act["verb"] == "claim_completed"
            and not holding
        ),
    }


def _block(
    facts: dict, *, scope: str, now: datetime, completed: bool | None = None
) -> dict:
    done = facts["completed"] if completed is None else completed
    state, reasons = derive_state(
        awaiting=facts["open_questions"] > 0 or facts["pending_approvals"] > 0,
        completed=done,
        last_work_at=facts["last_work_at"],
        last_checkin_at=facts["last_checkin_at"],
        now=now,
    )
    return {
        "state": state,
        "reasons": reasons,
        "scope": scope,
        "window_minutes": ACTIVE_WINDOW_MINUTES,
        "last_work_at": facts["last_work_at"],
        "last_checkin_at": facts["last_checkin_at"],
        "open_questions": facts["open_questions"],
        "pending_approvals": facts["pending_approvals"],
        "meaning": MEANING,
    }


def state_for_run(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    run_id: str,
    now: datetime | None = None,
) -> dict:
    """The state of one run (agent + run id)."""
    current = _now(now)
    facts = _facts(conn, agent_id=agent_id, run_id=run_id, issue_id=None, now=current)
    return _block(facts, scope="run", now=current)


def state_for_claim(
    conn: sqlite3.Connection,
    *,
    holder_id: int,
    run_id: str | None,
    issue_id: int,
    now: datetime | None = None,
) -> dict:
    """The state shown beside a held claim: its run's state when the claim is
    tagged, else the untagged claim's own (holder, issue) facts. A claim that is
    still held is never ``complete`` — completing it releases the lease."""
    current = _now(now)
    facts = _facts(
        conn, agent_id=holder_id, run_id=run_id, issue_id=issue_id, now=current
    )
    return _block(
        facts,
        scope="run" if run_id is not None else "claim",
        now=current,
        completed=False,
    )


def _visibility(conn: sqlite3.Connection, actor: dict) -> tuple[str, list[int]]:
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


def _age(stamp: str | None, now: datetime) -> int | None:
    if stamp is None:
        return None
    then = datetime.strptime(stamp, _TS_FORMAT).replace(tzinfo=UTC)
    return max(0, int((now - then).total_seconds()))


def open_questions(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    asked_by: int | None = None,
    now: datetime | None = None,
    limit: int = LANE_LIMIT,
) -> dict:
    """Open blocking questions — yielded claims nobody has resumed — oldest first,
    on issues this actor can see. ``asked_by`` narrows to one asker (a desk).
    Count and rows apply the same visibility. Read-only."""
    current = _now(now)
    vis_sql, vis_params = _visibility(conn, actor)
    asker_sql = "" if asked_by is None else " AND ye.actor_id = ?"
    asker_params: list[object] = [] if asked_by is None else [asked_by]
    base = (
        "FROM issue_claim_handoffs h "
        "JOIN activity ye ON ye.id = h.yield_event_id "
        "JOIN users u ON u.id = ye.actor_id "
        "JOIN issues i ON i.id = h.issue_id "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "WHERE h.resume_event_id IS NULL AND ye.imported_at IS NULL "
        "AND COALESCE(h.blocking_question, '') != '' "
        f"AND i.archived_at IS NULL{asker_sql}{vis_sql}"
    )
    params = [*asker_params, *vis_params]
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    rows = conn.execute(
        "SELECT h.handoff_token, h.reason, h.blocking_question, h.issue_id, "
        "ye.actor_id AS asked_by_id, u.name AS asked_by_name, ye.run_id, "
        "ye.created_at AS asked_at, i.title, i.project_seq, p.key AS project_key "
        f"{base} ORDER BY ye.created_at, ye.id LIMIT ?",
        [*params, limit],
    ).fetchall()
    items = []
    for row in rows:
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        waited = _age(row["asked_at"], current)
        items.append(
            {
                "kind": "question",
                "issue": {
                    "id": int(row["issue_id"]),
                    "key": key,
                    "title": row["title"],
                },
                "asked_by": {
                    "id": int(row["asked_by_id"]),
                    "name": row["asked_by_name"],
                },
                "run_id": row["run_id"],
                "asked_at": row["asked_at"],
                "waiting_seconds": waited,
                "reason": row["reason"],
                # Untrusted advisory text from the asking agent. Rendered escaped,
                # never executed.
                "blocking_question": row["blocking_question"],
                "handoff_token": row["handoff_token"],
            }
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "clipped": total > len(items),
        "meaning": (
            "blocking questions from yielded claims that nobody has resumed, oldest "
            "first; answered by resuming the handoff. Nothing enforces a response "
            "time."
        ),
    }


def waiting_on_operator(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
    limit: int = LANE_LIMIT,
) -> dict:
    """An agent's own open asks: its unresumed blocking questions and its pending
    approvals, oldest first, with the total behind a clipped list."""
    current = _now(now)
    me = int(actor["id"])
    questions = open_questions(conn, actor=actor, asked_by=me, now=current, limit=limit)
    approval_total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM approval_requests "
            "WHERE state = 'pending' AND requested_by = ?",
            (me,),
        ).fetchone()["n"]
    )
    approval_rows = conn.execute(
        "SELECT id, action_kind, target_kind, target_id, run_id, created_at "
        "FROM approval_requests WHERE state = 'pending' AND requested_by = ? "
        "ORDER BY created_at, id LIMIT ?",
        (me, limit),
    ).fetchall()
    approvals = [
        {
            "kind": "approval",
            "id": int(row["id"]),
            "action_kind": row["action_kind"],
            "target_kind": row["target_kind"],
            "target_id": row["target_id"],
            "run_id": row["run_id"],
            "asked_at": row["created_at"],
            "waiting_seconds": _age(row["created_at"], current),
        }
        for row in approval_rows
    ]
    merged = sorted([*questions["items"], *approvals], key=lambda row: row["asked_at"])[
        :limit
    ]
    total = questions["total"] + approval_total
    return {
        "items": merged,
        "total": total,
        "limit": limit,
        "clipped": total > len(merged),
        "meaning": (
            "what you asked a human and is still open: your blocking questions "
            "nobody has resumed and your pending approvals, oldest first. Nothing "
            "enforces a response time; do not re-ask what is already here."
        ),
    }
