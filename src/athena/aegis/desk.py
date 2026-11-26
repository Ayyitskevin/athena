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

from athena.aegis import (
    claim_handoffs,
    delegations,
    issue_activity,
    leases,
    office,
    ready_queue,
    review_request_projection,
    run_state,
    work_progress,
)
from athena.core import (
    agent_notebook,
    approvals,
    authorship,
    budgets,
    db,
    desk_cursors,
    fleet_roster,
    identity,
    notifications,
)
from athena.core import clock, run_control_commands, workers
from athena.core.desk_cursors import DESK_CURSOR

SCHEMA = "athena.agent_desk.v2"

#: The desk is a summary, so every lane is short on purpose: enough to decide
#: what to do next, never a page of history. The owning surface is one call
#: away when a lane's total says there is more.
CONTROLS_LIMIT = 20
DELEGATIONS_LIMIT = 20
LEASES_LIMIT = 20
HANDOFFS_LIMIT = 20
WORKER_STOPS_LIMIT = 50
NOTIFICATIONS_LIMIT = 10

#: How far the "what changed" counter will count before it stops counting. An
#: exact five-figure backlog is noise; "500+, drain from here" is the number an
#: agent can act on. When this bites the projection says so rather than
#: reporting a precise-looking total it did not compute.
EVENTS_SINCE_CAP = 500


def _last_run(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """The run of this account's newest run-tagged event, and when it wrote."""
    row = conn.execute(
        "SELECT run_id, created_at FROM activity "
        "WHERE actor_id = ? AND run_id IS NOT NULL AND imported_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {"run_id": row["run_id"], "last_write_at": row["created_at"]}


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


def latest_visible_event_id(conn: sqlite3.Connection, *, actor: dict) -> int | None:
    """The newest event id this actor can see — the value a fully-drained
    reader would advance its cursor to. Null on an empty (or fully hidden)
    trail, which is the honest answer: there is nothing to acknowledge.

    Asked one visibility arm at a time, as the event feed asks (H1). The arms
    are disjoint on ``target_kind``, so each walks ``idx_activity_kind_id``
    newest first and stops at its first visible event; the answer is the
    largest of those four ids, which is the id the single OR-joined predicate
    gives. Asked as ``MAX`` over that OR, SQLite read every event of every
    kind and ran the gate on each, on every desk read. Ungated (an admin), the
    plain ``MAX`` is answered from the end of the rowid index.

    Each arm names its index, so a database with statistics plans it the same
    way. The bound is the caller's newest visible event of each kind: a caller
    whose newest events of a kind are hidden (a seat in a workspace that went
    private around it) still walks past every hidden one, as the OR-joined
    read did."""
    from athena.core import access

    arms = access.event_visibility_arms(conn, actor, alias="a")
    if arms is None:
        row = conn.execute(
            "SELECT MAX(a.id) AS m FROM activity a NOT INDEXED "
            "CROSS JOIN users u ON u.id = a.actor_id"
        ).fetchone()
    else:
        newest = " UNION ALL ".join(
            "SELECT * FROM (SELECT a.id FROM activity a "
            "INDEXED BY idx_activity_kind_id "
            f"CROSS JOIN users u ON u.id = a.actor_id WHERE {clause} "
            "ORDER BY a.id DESC LIMIT 1)"
            for _kind, clause, _params in arms
        )
        row = conn.execute(
            f"SELECT MAX(id) AS m FROM ({newest})",
            [param for _kind, _clause, params in arms for param in params],
        ).fetchone()
    return None if row is None or row["m"] is None else int(row["m"])


def build_desk(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> dict:
    """Compose every lane at one database snapshot and observation instant."""
    with db.transaction(conn):
        return _build_desk(
            conn, actor=actor, now=now if now is not None else clock.now()
        )


def _build_desk(conn: sqlite3.Connection, *, actor: dict, now: datetime) -> dict:
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
    unanswered_workers, stop_total = (
        workers.unanswered_page(
            conn, agent_id=user_id, limit=WORKER_STOPS_LIMIT, now=now
        )
        if is_agent
        else ([], 0)
    )

    # --- work: what I am holding -------------------------------------------
    inbox = delegations.list_delegations(
        conn, actor, viewer=actor, limit=DELEGATIONS_LIMIT
    )
    # "Held" has to be true: this lane is what the clock still says is yours.
    # The rows the clock already released are their own list below, because a
    # lapsed lease is a different thing to do (renew it, or clear it) than a
    # live one — not a footnote on the same list.
    # Capture the default once: expiry must not put one possession in both
    # lanes or make the total/embedded office disagree with the held list.
    lease_now = now
    held = leases.active_leases_held_by(
        conn, holder_id=user_id, limit=LEASES_LIMIT, now=lease_now
    )
    lapsed, lapsed_total = leases.lapsed_leases_held_by(
        conn, holder_id=user_id, limit=LEASES_LIMIT, now=lease_now
    )
    # Holding is not working: beside each held lease, my own last recorded work on
    # that issue. A check-in or renewal never resets it; none recorded is "unknown".
    held_work = work_progress.last_work_at(
        conn, [(int(lease["issue_id"]), user_id) for lease in held]
    )
    for lease in held:
        lease["progress"] = work_progress.progress_for(
            held_work.get((int(lease["issue_id"]), user_id)), now=lease_now
        )
    # The other side of a hand-off: what I delegated that nobody picked up.
    waiting_on = work_progress.unclaimed_delegations(
        conn,
        actor=actor,
        delegator_id=user_id,
        now=lease_now,
        limit=DELEGATIONS_LIMIT,
    )
    # Filter visible asks across all live possessions before applying their own
    # bound; the held-work display window is not the universe of handoffs.
    handoffs = claim_handoffs.open_handoffs_for_holder(
        conn, actor=actor, now=lease_now, limit=HANDOFFS_LIMIT
    )

    review_requests = review_request_projection.visible_requests(
        conn, actor=actor, lane="desk", scope_id=user_id, now=lease_now
    )
    # The ready lane and what the rule holds back from it, from one evaluation
    # of the ready rule (H3): the same rows the two lanes' own reads give at
    # this instant, without asking the rule once per lane and per total.
    ready, not_offered = ready_queue.build_lanes(
        conn,
        actor=actor,
        now=lease_now,
        limit=ready_queue.READY_LIMIT,
        not_offered_limit=run_state.LANE_LIMIT,
    )

    # --- signals: what changed while I was away ------------------------------
    cursor = desk_cursors.get_cursor(conn, user_id=user_id, name=DESK_CURSOR)
    after_id = None if cursor is None else int(cursor["after_id"])
    since_count, since_capped = _events_since(conn, actor=actor, after_id=after_id)

    desk = {
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
            "seat_slug": fleet_roster.seat_slug_for_email(conn, actor.get("email")),
            # Your notebook page: id, last edit and a bounded excerpt, or null
            # when you have none yet (POST /desk/notebook creates it; this read
            # never does).
            "notebook": agent_notebook.desk_lane(conn, actor),
            # Your newest write tagged with a run (AD10, D32), or null: the
            # run to continue with ATHENA_RUN_ID or begin_run, so one agent's
            # work on one slice stays on one run across processes.
            "last_run": _last_run(conn, user_id),
        },
        "asks": {
            # Present when empty, like every other lane (A13): an absent lane
            # reads as "no such thing", not "nothing asked of you".
            "review_requests": review_requests
            if review_requests is not None
            else review_request_projection.empty_lane("desk"),
            "run_controls": {
                "items": open_controls,
                "limit": CONTROLS_LIMIT,
                "clipped": len(open_controls) == CONTROLS_LIMIT,
                # Wording matches RUN_CONTROLS.md: a control is a recorded
                # request, and "open" means nobody has answered it yet.
                "meaning": "asked of you and not yet answered",
            },
            "worker_kill_requests": {
                "items": [
                    {
                        "worker_id": worker["id"],
                        "worker_key": worker["worker_key"],
                        "kill_state": worker["kill_state"],
                        "kill_requested_at": worker["kill_requested_at"],
                        "last_reported_at": worker["last_seen_at"],
                        "reporting_state": worker["reporting_state"],
                    }
                    for worker in unanswered_workers
                ],
                "total": stop_total,
                "limit": WORKER_STOPS_LIMIT,
                "clipped": stop_total > len(unanswered_workers),
                "meaning": (
                    "the operator asked these workers to stop; Athena cannot "
                    "signal a process, so honoring it is yours to do"
                ),
            },
            "claim_handoffs": {
                "items": handoffs["items"],
                "limit": HANDOFFS_LIMIT,
                "clipped": handoffs["clipped"],
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
            # What I could claim next, in claim order, with its own total. The
            # same rule as the is:ready atom and claim_next_ready.
            "ready": ready,
            # My own open asks: blocking questions nobody resumed and pending
            # approvals. So an agent sees what it already asked, not re-asks.
            "waiting_on_operator": run_state.waiting_on_operator(
                conn, actor=actor, now=lease_now, not_offered=not_offered
            ),
            "waiting_on": {
                "items": waiting_on["items"],
                "total": waiting_on["visible_total"],
                "limit": DELEGATIONS_LIMIT,
                "clipped": waiting_on["clipped"],
                "older_than_hours": waiting_on["older_than_hours"],
                "meaning": (
                    "issues you delegated that are waiting on their delegate: "
                    + waiting_on["meaning"]
                ),
            },
            "leases": {
                "items": held,
                "total": leases.count_active_leases_held_by(
                    conn, holder_id=user_id, now=lease_now
                ),
                "limit": LEASES_LIMIT,
                "lapsed": lapsed,
                "lapsed_total": lapsed_total,
                "lapsed_clipped": lapsed_total > len(lapsed),
                "lapsed_oldest_expires_at": (
                    min((row["expires_at"] for row in lapsed), default=None)
                    if lapsed_total == len(lapsed)
                    else None
                ),
                "meaning": (
                    "`items` are the issues you hold at this read; `active` is "
                    "the clock's verdict, not a stored state, and `total` "
                    "counts the same instant. `lapsed` are leases the clock "
                    "already released, including done issues (`issue_done`). "
                    "These are your own residual rows, not active possession. "
                    "`lapsed_total` counts them before the shared `limit`; "
                    "`lapsed_clipped` says whether rows are omitted. "
                    "`lapsed_oldest_expires_at` is null when empty or clipped "
                    "(unknown when clipped); total/clipped distinguish these cases. "
                    "Claim order does not establish oldest expiry. "
                    "Holders settle their own rows deliberately with complete_claim "
                    "and the observed generation; an admin alone settles another "
                    "actor's expired row, by generation. Reading changes nothing. "
                    "complete_claim releases the "
                    "lease and does not mark the issue done. declared_paths is "
                    "the optional file fence. path_fence: rooted (a catalog root, "
                    "not a verified location), lexical, root unknown (no catalog), "
                    "unresolved (conflicts conservatively) or issue_only (no files)."
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
            "latest_visible_event_id": latest_visible_event_id(conn, actor=actor),
        },
        "office": office.build_office(
            conn,
            actor=actor,
            now=lease_now,
            inbox_items=inbox.get("items") or [],
        ),
        "cursor": cursor,
    }
    _mark_authors(conn, desk, actor)
    return desk


def _mark_authors(conn: sqlite3.Connection, desk: dict, actor: dict) -> None:
    """Give every desk object that holds another account's words its
    ``authored_by`` (core/authorship.py), as this actor may see it: each issue
    a lane names (its title, and a delegated issue's body), each blocking
    question and run control (its asker), and the notebook (its current
    version's editor). Claim handoffs and notifications carry theirs from their
    own reads."""
    work, office_lane = desk["work"], desk["office"]
    waiting = work["waiting_on_operator"]
    issue_refs: list[dict] = []
    for item in work["delegations"]["items"]:
        issue_refs.append(item["issue"])
        issue_refs.extend(item.get("visible_open_blockers") or [])
    issue_refs.extend(item["issue"] for item in work["ready"]["items"])
    issue_refs.extend(item["issue"] for item in waiting["items"] if "issue" in item)
    issue_refs.extend(item["issue"] for item in waiting["not_offered"]["items"])
    issue_refs.extend(item["issue"] for item in work["waiting_on"]["items"])
    for overlap in office_lane["fence_overlaps"]:
        issue_refs.extend((overlap["your_issue"], overlap["issue"]))
    seats = [office_lane[key] for key in ("chair", "next_to_sit") if office_lane[key]]
    if office_lane["chair"]:
        issue_refs.extend(office_lane["chair"]["blocked_by"])
    authors = issue_activity.issue_text_authors(
        conn,
        [ref["id"] for ref in issue_refs] + [seat["issue_id"] for seat in seats],
        actor=actor,
    )
    for ref in issue_refs:
        ref[authorship.KEY] = authors[ref["id"]]
    for seat in seats:
        seat[authorship.KEY] = authors[seat["issue_id"]]
    questions = [item for item in waiting["items"] if item["kind"] == "question"]
    askers = authorship.accounts(conn, (item["asked_by"]["id"] for item in questions))
    for item in questions:
        item[authorship.KEY] = askers.get(item["asked_by"]["id"])
    # An open control's payload is its requester's; nobody has settled it, so
    # its result_summary is still empty.
    authorship.stamp(conn, desk["asks"]["run_controls"]["items"], id_key="requested_by")
    notebook = desk["identity"]["notebook"]
    if notebook is not None:
        notebook[authorship.KEY] = authorship.page_authors(conn, [notebook["page_id"]])[
            notebook["page_id"]
        ]
