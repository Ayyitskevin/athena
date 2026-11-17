"""Per-agent answerability: what was asked, and what was answered.

Buzz's roadmap calls the idea "web-of-trust reputation". Athena adapts the
useful half and refuses the rest: no score, no ranking, no reputation number —
a solo operator does not need a leaderboard, and a single scalar would launder
"the clock ran out twice" into "bad agent". What transfers is the LEDGER: for
each agent, the recorded asks (run controls addressed to it, kill requests to
its workers, approvals its gated actions raised) next to the recorded answers,
plus how often an operator reversed its work. Every number is derived at read
time from tables that already exist — this module owns no table, stores
nothing, and each count is the same predicate the owning surface lists by.

The projection deliberately reports FACTS PER LANE rather than a verdict:
an agent with three expired controls might be dead, might be busy, might never
poll its inbox — Athena records that three asks went unanswered, and the
operator judges. `docs/ANSWERABILITY.md` carries the full non-claims list.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena.core import activity, approvals, run_controls, workers

SCHEMA = "athena.agent_answerability.v1"

_ZERO_CONTROLS = {"open": 0, "expired_unanswered": 0, "completed": 0, "declined": 0}
_ZERO_APPROVALS = {"pending": 0, "approved": 0, "rejected": 0}


def build_answerability(
    conn: sqlite3.Connection,
    *,
    agent_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """The ask-and-answer ledger for every agent account (or one).

    Admin-only by the caller's contract, like the fleet-attention rollup: every
    input is already an admin-scoped read, and aggregating them widens none.
    Agents with nothing recorded appear zero-filled — "no asks yet" is an
    explicit statement, not a missing row. An unknown agent_id simply yields an
    empty list; the REST adapter turns that into its 404.
    """
    if agent_id is not None and (
        isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id < 1
    ):
        raise ValueError("agent_id must be a positive integer")
    clause = "WHERE is_agent = 1 AND removed_at IS NULL" + (
        "" if agent_id is None else " AND id = ?"
    )
    params: list[object] = [] if agent_id is None else [agent_id]
    agent_rows = conn.execute(
        f"SELECT id, name, paused_at FROM users {clause} ORDER BY id", params
    ).fetchall()

    observed = datetime.now(UTC) if now is None else now
    now_stamp = run_controls.stamp(observed)
    control_counts = run_controls.state_counts_by_agent(conn, now_stamp=now_stamp)
    approval_counts = approvals.request_counts_by_requester(conn)
    reversed_counts = activity.reversed_counts_by_actor(conn)

    kill_counts = workers.kill_counts_by_agent(conn, now=observed, agent_id=agent_id)

    agents = [
        {
            "agent_id": int(row["id"]),
            "agent_name": row["name"],
            "paused": row["paused_at"] is not None,
            "run_controls": control_counts.get(int(row["id"]), dict(_ZERO_CONTROLS)),
            "worker_kills": kill_counts.get(
                int(row["id"]), {"told_to_stop": 0, "confirmed": 0}
            ),
            "approvals": approval_counts.get(int(row["id"]), dict(_ZERO_APPROVALS)),
            "reversed_events": reversed_counts.get(int(row["id"]), 0),
        }
        for row in agent_rows
    ]
    return {
        "schema": SCHEMA,
        "generated_at": now_stamp,
        "agents": agents,
    }
