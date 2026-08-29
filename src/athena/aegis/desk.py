"""The Desk: one bounded read that orients an agent for a session.

An agent starting work has had to discover its own situation through five or
six calls — whoami, the delegation inbox, the run-control inbox, notifications,
its budget, its leases — each with different bounds and none of them saying
what changed while it was away. That is a lot of ceremony before any work
happens, and a fresh context continuing someone else's run had no single place
to look at all.

The desk is that place: **who you are, what is asked of you, what you are
holding, and what has changed since you last looked.** It is the "open your
inbox in the morning" habit, made available in one call.

**It composes, it does not compute.** Every lane calls the reader that already
owns that surface, with THIS actor's visibility applied — the run-control
inbox, the delegation list, the worker registry, the notification inbox, the
lease table's own holder read. So the desk cannot show an agent anything the
owning surface would not, and it cannot disagree with the page or tool that
owns each number. Nothing here is a second source of truth.

**What the desk is not** (docs/DESK.md carries the full list):

- Not a lock, a lease, or a queue. Seeing work here reserves nothing; two
  agents can read the same desk contents at the same instant.
- Not a liveness or health claim. A control that is "open" is a request nobody
  has answered; a worker's kill state is what the worker last reported.
- Not a delivery guarantee. ``events_since_cursor`` counts what is visible to
  this actor right now, capped, and the cursor moves only when the agent says
  it has drained.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3

from athena.aegis import claim_handoffs, delegations, leases, office
from athena.core import (
    approvals,
    budgets,
    desk_cursors,
    fleet_roster,
    identity,
    notifications,
)
from athena.core import run_control_commands, workers
from athena.core.desk_cursors import DESK_CURSOR

SCHEMA = "athena.agent_desk.v2"

#: The desk is a summary, so every lane is short on purpose: enough to decide
#: what to do next, never a page of history. The owning surface is one call
#: away when a lane's total says there is more.
CONTROLS_LIMIT = 20
DELEGATIONS_LIMIT = 20
LEASES_LIMIT = 20
HANDOFFS_LIMIT = 20
NOTIFICATIONS_LIMIT = 10

#: How far the "what changed" counter will count before it stops counting. An
#: exact five-figure backlog is noise; "500+, drain from here" is the number an
#: agent can act on. When this bites the projection says so rather than
#: reporting a precise-looking total it did not compute.
EVENTS_SINCE_CAP = 500


def _events_since(
    conn: sqlite3.Connection, *, actor: dict, after_id: int | None
) -> tuple[int | None, bool]:
    """How many visible events sit past this actor's cursor, and whether the
    count hit its ceiling. ``(None, False)`` when no cursor is set — never a
    zero, which would read as "nothing new" to an agent that has never looked.

    The visibility gate is the feeds' own: the desk counts exactly the events
    ``GET /events`` would hand this actor, so the number and the drain agree.
    """
    if after_id is None:
        return None, False
    from athena.core import access

    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    where = f"a.id > ?{f' AND {gate}' if gate else ''}"
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT a.id FROM activity a "
        f"JOIN users u ON u.id = a.actor_id WHERE {where} LIMIT ?)",
        [after_id, *params, EVENTS_SINCE_CAP + 1],
    ).fetchone()
    count = int(row["n"])
    if count > EVENTS_SINCE_CAP:
        return EVENTS_SINCE_CAP, True
    return count, False


def _latest_event_id(conn: sqlite3.Connection, *, actor: dict) -> int | None:
    """The newest event id this actor can see — the value a fully-drained
    reader would advance its cursor to. Null on an empty (or fully hidden)
    trail, which is the honest answer: there is nothing to acknowledge."""
    from athena.core import access

    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    where = f" WHERE {gate}" if gate else ""
    row = conn.execute(
        f"SELECT MAX(a.id) AS m FROM activity a "
        f"JOIN users u ON u.id = a.actor_id{where}",
        params,
    ).fetchone()
    return None if row is None or row["m"] is None else int(row["m"])


def build_desk(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> dict:
    """This actor's desk: identity, asks, work, signals, cursor.

    Every lane is the owning surface's own read with this actor's visibility,
    bounded, and carrying its own total so a clipped list never implies it is
    the whole story.
    """
    user_id = int(actor["id"])
    is_agent = bool(actor.get("is_agent"))

    budget = budgets.observed(conn, user_id)
    scopes = actor.get("_token_scopes")

    # --- asks: what someone has asked of me and not had answered ------------
    open_controls = run_control_commands.readable_controls(
        conn, actor=actor, state="open", limit=CONTROLS_LIMIT, now=now
    )
    # Workers are an agent concept; a human actor has none, and asking the
    # registry for a human's workers would be a query with no meaning.
    kill_requests = (
        [
            {
                "worker_id": worker["id"],
                "worker_key": worker["worker_key"],
                "kill_state": worker["kill_state"],
                "kill_requested_at": worker["kill_requested_at"],
                # What the worker last REPORTED — never an observation of the
                # process, per WORKERS.md.
                "last_reported_at": worker["last_seen_at"],
                "reporting_state": worker["reporting_state"],
            }
            for worker in workers.list_workers(
                conn, agent_id=user_id, limit=50, now=now
            )
            if worker["kill_state"] in (workers.KILL_REQUESTED, workers.KILL_DEFIED)
        ]
        if is_agent
        else []
    )

    # --- work: what I am holding -------------------------------------------
    inbox = delegations.list_delegations(
        conn, actor, viewer=actor, limit=DELEGATIONS_LIMIT
    )
    # "Held" has to be true: this lane is what the clock still says is yours.
    # The rows the clock already released are their own list below, because a
    # lapsed lease is a different thing to do (renew it, or clear it) than a
    # live one — not a footnote on the same list.
    held = leases.active_leases_held_by(
        conn, holder_id=user_id, limit=LEASES_LIMIT, now=now
    )
    lapsed, lapsed_total = leases.lapsed_leases_held_by(
        conn, holder_id=user_id, limit=LEASES_LIMIT, now=now
    )
    # A handoff is attached to an issue, so "mine to acknowledge" is exactly
    # "open on an issue I hold" — composed rather than re-queried. The reader
    # returns a dict keyed by issue id; the desk presents an ordered list.
    handoffs_by_issue = claim_handoffs.open_handoffs_for_issues(
        conn, [lease["issue_id"] for lease in held]
    )
    handoffs = [handoffs_by_issue[key] for key in sorted(handoffs_by_issue)]

    # --- signals: what changed while I was away ------------------------------
    cursor = desk_cursors.get_cursor(conn, user_id=user_id, name=DESK_CURSOR)
    after_id = None if cursor is None else int(cursor["after_id"])
    since_count, since_capped = _events_since(conn, actor=actor, after_id=after_id)

    return {
        "schema": SCHEMA,
        "identity": {
            "id": user_id,
            "name": actor.get("name"),
            "email": actor.get("email"),
            "role": actor.get("role"),
            "is_agent": is_agent,
            "paused": actor.get("paused_at") is not None,
            # None means the credential is not scope-limited (a session), which
            # is a different fact from an empty scope set.
            "scopes": None if scopes is None else sorted(scopes),
            "budget": None if budget is None else budget.public(),
            "approval_required": approvals.gated_kinds(conn, user_id),
            "is_admin": identity.is_admin(actor),
            # None when this account is not a declared fleet seat. Same slug
            # as Buzz / systemd / drift-check when it is.
            "seat_slug": fleet_roster.seat_slug_for_email(actor.get("email")),
        },
        "asks": {
            "run_controls": {
                "items": open_controls,
                "limit": CONTROLS_LIMIT,
                "clipped": len(open_controls) == CONTROLS_LIMIT,
                # Wording matches RUN_CONTROLS.md: a control is a recorded
                # request, and "open" means nobody has answered it yet.
                "meaning": "asked of you and not yet answered",
            },
            "worker_kill_requests": {
                "items": kill_requests,
                "meaning": (
                    "the operator asked these workers to stop; Athena cannot "
                    "signal a process, so honoring it is yours to do"
                ),
            },
            "claim_handoffs": {
                "items": handoffs[:HANDOFFS_LIMIT],
                "limit": HANDOFFS_LIMIT,
                "clipped": len(handoffs) > HANDOFFS_LIMIT,
                "meaning": "context handed to you that you have not acknowledged",
            },
        },
        "work": {
            "protocol": {
                "claim_one_issue": True,
                "do_not_touch_other_work": True,
                "complete_does_not_close_issue": True,
                "meaning": (
                    "The desk is orientation, not a lock. Claim at most one "
                    "issue. Do not edit files another active lease has fenced. "
                    "complete_claim releases the lease only."
                ),
            },
            "delegations": {
                "items": inbox.get("items", []),
                "limit": DELEGATIONS_LIMIT,
                # The inbox pages by offset rather than reporting a total, so
                # the desk repeats its own honesty rather than inventing a
                # count it would have to query for separately.
                "has_more": bool(inbox.get("has_more")),
                "next_offset": inbox.get("next_offset"),
            },
            "leases": {
                "items": held,
                "total": leases.count_active_leases_held_by(
                    conn, holder_id=user_id, now=now
                ),
                "limit": LEASES_LIMIT,
                "lapsed": lapsed,
                "lapsed_total": lapsed_total,
                "meaning": (
                    "`items` are the issues you hold at this read; `active` is "
                    "the clock's verdict, not a stored state, and `total` "
                    "counts the same instant. `lapsed` are leases the clock "
                    "already released on issues still open — nobody else holds "
                    "them unless GET /issues/{id}/lease says so; complete_claim "
                    "with a lapsed row's generation clears it. A lapsed lease "
                    "on a done issue is not listed here; its row stays readable "
                    "at GET /issues/{id}/lease. complete_claim releases the "
                    "lease and does not mark the issue done. declared_paths is "
                    "the optional file fence."
                ),
            },
        },
        "signals": {
            "unread_notifications": notifications.unread_count(
                conn, user_id, actor=actor
            ),
            "notifications": notifications.list_notifications(
                conn, user_id, unread_only=True, limit=NOTIFICATIONS_LIMIT, actor=actor
            ),
            # Null (not zero) when no cursor is set: "never looked" is not
            # "nothing new".
            "events_since_cursor": since_count,
            "events_since_cursor_capped": since_capped,
            # Where a fully-drained reader would advance to. Drain GET /events
            # from `cursor.after_id`, then acknowledge this.
            "latest_visible_event_id": _latest_event_id(conn, actor=actor),
        },
        "office": office.build_office(
            conn,
            actor=actor,
            now=now,
            inbox_items=inbox.get("items") or [],
        ),
        "cursor": cursor,
    }
