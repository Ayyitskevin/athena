"""The operator narrative for one issue — its existing history read as a run story.

An issue's audit trail already records every claim, handoff, and outcome; the
run-control inbox records operator steering of the runs that did the work; the
check-in table records those runs reporting in. An operator reviewing an issue
has had to read all three surfaces and stitch the story together by run id.

This projection does the stitching at READ time. It walks the issue's visible
activity, joins the claim handoffs, run controls, and check-ins belonging to
the runs on that trail, and returns one ordered list of typed signal items
(claim, checkin, ask, run control, handoff, evidence, outcome). It creates no
table, no event source, no second timeline, and no ID namespace: every item
CITES the owning surface's own id (activity event id, handoff token, run
control id, run check-in) in ``source`` and names the owning read in ``via``.
Acting on an item means calling that surface — this projection never becomes a
write target, and no authority is inferred from the evidence it shows.

FAIL CLOSED, twice over:

- An activity verb with no signal mapping is never guessed at. It is counted
  in ``window.unclassified_events`` and left to the plain history view, so a
  future verb can never be silently misfiled under a known signal.
- Every lane is read through its owning surface's visibility: the activity
  trail is actor-gated exactly like the history page, run controls come from
  ``readable_controls`` (admin sees all, an agent sees its own, anyone else an
  empty lane), and check-ins are shown only to the admin cockpit that owns
  them. A lane the actor cannot see is reported in ``visibility`` instead of
  leaking or fabricating contents.

ONE CLOCK: everything derived from the current time — check-in reporting
state, control open/expired — is computed from a single per-request
``observed_at`` so freshness comparisons inside one response are honest.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3

from athena.aegis import claim_handoffs, issues
from athena.core import (
    activity,
    agent_run_checkins,
    db,
    identity,
    run_control_commands,
    run_controls,
)

SCHEMA = "athena.issue_narrative.v1"

SIGNAL_CLAIM = "claim"
SIGNAL_CHECKIN = "checkin"
SIGNAL_ASK = "ask"
SIGNAL_RUN_CONTROL = "run_control"
SIGNAL_HANDOFF = "handoff"
SIGNAL_EVIDENCE = "evidence"
SIGNAL_OUTCOME = "outcome"

#: Bounds on one build. The item list is a reading window, not a pager: when a
#: lane clips, the response says so rather than presenting a partial story as
#: the whole one (clipped-window completeness).
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
EVENT_WINDOW = 500
CONTROLS_PER_RUN_LIMIT = 20
HANDOFF_WINDOW = 50

#: Deterministic tie-break order for items sharing one timestamp (activity
#: stamps are second-resolution, so a claim and its check-in routinely tie).
_SIGNAL_RANK = {
    SIGNAL_OUTCOME: 0,
    SIGNAL_HANDOFF: 1,
    SIGNAL_EVIDENCE: 2,
    SIGNAL_CLAIM: 3,
    SIGNAL_ASK: 4,
    SIGNAL_RUN_CONTROL: 5,
    SIGNAL_CHECKIN: 6,
}

#: The only activity verbs the narrative reads as claim/outcome signals. A
#: yield recorded WITH a handoff is told by the handoff lane instead (it owns
#: the richer record), as is its resume; both still count as classified here.
_CLAIM_VERBS = {"claimed", "lease_renewed"}
_YIELD_VERB = "claim_yielded"
_RESUME_VERB = "claim_handoff_resumed"
_COMPLETE_VERB = "claim_completed"

#: Open control states read as an outstanding ask; anything past them is the
#: record of how the steering ended.
_ASK_STATES = {run_controls.STATE_REQUESTED, run_controls.STATE_ACKNOWLEDGED}


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be positive")
    return min(limit, MAX_LIMIT)


def _actor_ref(actor_id: int | None, name: str | None) -> dict | None:
    if actor_id is None:
        return None
    return {"id": actor_id, "name": name}


def build_issue_narrative(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    actor: dict | None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict | None:
    """One run narrative for one issue, or None when no such issue exists.

    Read-only; runs inside one transaction so the joined lanes are a single
    snapshot, and takes one injectable clock so every derived freshness in the
    response agrees with ``observed_at``. The actor gates visibility exactly as
    the owning surfaces do — this projection adds no read authority of its own.
    """
    bounded = _bounded_limit(limit)
    with db.transaction(conn):
        return _build(conn, issue_id, actor=actor, limit=bounded, now=now)


def _build(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    actor: dict | None,
    limit: int,
    now: datetime | None,
) -> dict | None:
    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        return None
    observed_at = run_controls.stamp(now)

    # --- the issue's own trail, gated exactly like the history page ---------
    rows = activity.list_activity(
        conn,
        target_kind="issue",
        target_id=issue_id,
        limit=EVENT_WINDOW + 1,
        actor=actor,
    )
    events_clipped = len(rows) > EVENT_WINDOW
    events = rows[:EVENT_WINDOW]

    # --- the handoff lane: the owning record for yields that carried one ----
    handoff_history = claim_handoffs.list_handoffs(
        conn, issue_id, limit=HANDOFF_WINDOW
    )
    handoffs = handoff_history["items"]
    handoff_by_yield_event = {h["yielded"]["event_id"]: h for h in handoffs}
    handoff_by_resume_event = {
        h["resumed"]["event_id"]: h for h in handoffs if h["resumed"] is not None
    }

    items: list[dict] = []
    unclassified = 0
    run_ids: set[str] = set()
    for event in events:
        if event["run_id"]:
            run_ids.add(str(event["run_id"]))
        verb = event["verb"]
        base = {
            "at": event["created_at"],
            "actor": _actor_ref(event["actor_id"], event["actor_name"]),
            "run_id": event["run_id"],
            "source": {"kind": "activity", "id": event["id"]},
            "via": f"GET /activity?target_kind=issue&target_id={issue_id}",
        }
        if verb in _CLAIM_VERBS:
            items.append(
                {
                    **base,
                    "signal": SIGNAL_CLAIM,
                    "state": None,
                    "summary": f"{event['actor_name']} {verb.replace('_', ' ')} "
                    f"({event['detail']})",
                }
            )
        elif verb == _COMPLETE_VERB:
            items.append(
                {
                    **base,
                    "signal": SIGNAL_OUTCOME,
                    "state": "claim_completed",
                    "summary": f"{event['actor_name']} completed the claim "
                    f"({event['detail']})",
                }
            )
        elif verb == _YIELD_VERB and event["id"] not in handoff_by_yield_event:
            # A yield with no handoff ends the run's involvement — an outcome.
            # A yield WITH one is told by the handoff lane below.
            items.append(
                {
                    **base,
                    "signal": SIGNAL_OUTCOME,
                    "state": "claim_yielded",
                    "summary": f"{event['actor_name']} yielded the claim "
                    f"without a handoff ({event['detail']})",
                }
            )
        elif verb == _YIELD_VERB or verb == _RESUME_VERB:
            # Classified, but narrated by the handoff lane's own record.
            if verb == _RESUME_VERB and event["id"] not in handoff_by_resume_event:
                # A resume event whose handoff fell outside the handoff window:
                # fail closed to the plain event rather than invent the link.
                unclassified += 1
        else:
            unclassified += 1

    for handoff in handoffs:
        yielded = handoff["yielded"]
        items.append(
            {
                "signal": SIGNAL_HANDOFF,
                "at": yielded["created_at"],
                "actor": _actor_ref(yielded["actor_id"], yielded["actor_name"]),
                "run_id": yielded["run_id"],
                "state": handoff["state"],
                "summary": (
                    f"handoff ({handoff['reason']}): {handoff['note']}"
                    if handoff["note"]
                    else f"handoff ({handoff['reason']})"
                ),
                "source": {"kind": "claim_handoff", "id": handoff["handoff_token"]},
                "via": f"GET /issues/{issue_id}/work-context",
                "lease_generation": handoff["lease_generation"],
                "yield_event_id": yielded["event_id"],
                "resume_event_id": (
                    None
                    if handoff["resumed"] is None
                    else handoff["resumed"]["event_id"]
                ),
            }
        )
        for ref in handoff["evidence"]:
            items.append(
                {
                    "signal": SIGNAL_EVIDENCE,
                    "at": yielded["created_at"],
                    "actor": _actor_ref(yielded["actor_id"], yielded["actor_name"]),
                    "run_id": yielded["run_id"],
                    "state": None,
                    "summary": str(ref),
                    "source": {
                        "kind": "claim_handoff",
                        "id": handoff["handoff_token"],
                    },
                    "via": f"GET /issues/{issue_id}/work-context",
                }
            )

    # --- run controls, through the owning surface's visibility --------------
    can_see_controls = actor is not None and (
        identity.is_admin(actor) or bool(actor.get("is_agent"))
    )
    controls_clipped = False
    if can_see_controls:
        for run_id in sorted(run_ids):
            controls = run_control_commands.readable_controls(
                conn,
                actor=actor,
                run_id=run_id,
                limit=CONTROLS_PER_RUN_LIMIT,
                now=now,
            )
            controls_clipped = controls_clipped or (
                len(controls) == CONTROLS_PER_RUN_LIMIT
            )
            for control in controls:
                open_ask = control["state"] in _ASK_STATES
                items.append(
                    {
                        "signal": SIGNAL_ASK if open_ask else SIGNAL_RUN_CONTROL,
                        "at": control["created_at"],
                        "actor": _actor_ref(
                            control["requested_by"], control["requested_by_name"]
                        ),
                        "run_id": control["run_id"],
                        "state": control["state"],
                        "summary": (
                            f"{control['kind']} for run {control['run_id']} — "
                            f"{control['state']}"
                        ),
                        "source": {"kind": "run_control", "id": control["id"]},
                        "via": f"GET /run-controls/{control['id']}",
                    }
                )

    # --- check-ins: the admin cockpit's lane, shown to no one else ----------
    can_see_checkins = actor is not None and identity.is_admin(actor)
    if can_see_checkins:
        checkins = agent_run_checkins.list_checkins_for_runs(
            conn, run_ids=sorted(run_ids), now=now
        )
        for run_id in sorted(checkins):
            for checkin in checkins[run_id]:
                items.append(
                    {
                        "signal": SIGNAL_CHECKIN,
                        "at": checkin["last_seen_at"],
                        "actor": _actor_ref(checkin["agent_id"], None),
                        "run_id": checkin["run_id"],
                        "state": checkin["reporting_state"],
                        "summary": (
                            f"run {checkin['run_id']} last reported "
                            f"{checkin['age_seconds']}s before observed_at "
                            f"({checkin['reporting_state']})"
                        ),
                        "source": {
                            "kind": "agent_run_checkin",
                            "id": checkin["run_id"],
                            "agent_id": checkin["agent_id"],
                        },
                        "via": "GET /activity/agent-runs",
                    }
                )

    items.sort(
        key=lambda item: (
            str(item["at"]),
            _SIGNAL_RANK[item["signal"]],
            str(item["source"]["id"]),
        ),
        reverse=True,
    )
    clipped = len(items) > limit
    return {
        "schema": SCHEMA,
        "issue_id": issue_id,
        # The single clock every derived freshness in this response used.
        "observed_at": observed_at,
        "items": items[:limit],
        "limit": limit,
        "clipped": clipped,
        "window": {
            "event_limit": EVENT_WINDOW,
            "events_clipped": events_clipped,
            "handoff_limit": HANDOFF_WINDOW,
            "handoffs_clipped": bool(handoff_history["clipped"]),
            "controls_per_run_limit": CONTROLS_PER_RUN_LIMIT,
            "controls_clipped": controls_clipped,
            # Visible events the narrative deliberately did not type — the
            # fail-closed count that keeps unknown signals out of the story.
            "unclassified_events": unclassified,
        },
        "visibility": {
            # Which lanes this actor may see at all, per the owning surfaces.
            "run_controls": can_see_controls,
            "checkins": can_see_checkins,
        },
    }
