"""Athena's half of the contract with an external execution fleet.

Athena is the **control plane**. An executor (Icarus) is a separate system with
its own store, its own runs, and its own idea of progress. They share no database
and neither imports the other: they reconcile over an asynchronous HTTP contract,
and `icarus_dispatches` (migration 0067) is what Athena remembers about it.

**Every state name here describes Athena's knowledge, not the executor's
progress.** `accepted` means "it said it accepted"; it does not mean work is
running, and nothing in this module will ever claim otherwise. That is the same
discipline `workers.py` applies to a heartbeat and `agent_run_checkins.py` applies
to a check-in — Athena reports what it was told, timestamped by its own clock.

**Evidence is referenced, never copied.** `evidence_ref` and `completion_ref` are
opaque strings the executor chose. Copying artifacts across the boundary would
make one system's storage the other's problem, which is precisely what "no shared
database" rules out.

**The policy digest is tamper-evident, not tamper-proof.** It hashes the
authorization state in force at dispatch. A callback carrying a different digest is
recorded and **flagged**, not discarded and not trusted: the point of a digest is
to notice a mismatch, and hiding the evidence would defeat that.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from athena.core import db

#: Athena's knowledge of a dispatch. Read these as "what has Athena been told".
PENDING_DELIVERY = "pending_delivery"
ACCEPTED = "accepted"
UNDELIVERABLE = "undeliverable"
COMPLETED = "completed"
FAILED = "failed"
OPEN_STATES = (PENDING_DELIVERY, ACCEPTED)
TERMINAL_STATES = (COMPLETED, FAILED)

VERB_REQUESTED = "dispatch_requested"
VERB_ACCEPTED = "dispatch_accepted"
VERB_UNDELIVERABLE = "dispatch_undeliverable"
VERB_EVIDENCE = "dispatch_evidence_recorded"
VERB_TERMINAL = "dispatch_completed"
VERB_DIGEST_MISMATCH = "dispatch_policy_digest_mismatch"

#: The run namespace Athena mints for a dispatch. Reserved like `automation:`, so a
#: client cannot stamp its own writes with an execution run it does not own.
RUN_PREFIX = "icarus:"

MAX_REF_CHARS = 500


class DispatchError(Exception):
    """A transport-neutral rejection. ``kind`` maps to each adapter's status."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


STATUS_BY_KIND: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "unavailable": 503,
}


@dataclass(frozen=True)
class PolicyFacts:
    """The authorization state in force when Athena decided to dispatch.

    Hashed into the digest, so a callback can be checked against the world as it
    was — not as it is now. Deliberately small and explicit: a digest over a
    loosely-defined blob would be a number nobody could reproduce or argue with.
    """

    actor_id: int
    scopes: tuple[str, ...]
    work_item_id: int
    repo: str
    base_commit: str
    capability: str
    approval_state: str
    budget_window: str | None
    budget_limit: int | None

    def digest(self) -> str:
        canonical = json.dumps(
            {
                "actor_id": self.actor_id,
                "scopes": sorted(self.scopes),
                "work_item_id": self.work_item_id,
                "repo": self.repo,
                "base_commit": self.base_commit,
                "capability": self.capability,
                "approval_state": self.approval_state,
                "budget_window": self.budget_window,
                "budget_limit": self.budget_limit,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


_COLUMNS = (
    "id, work_item_id, run_id, parent_run_id, icarus_run_id, repo, base_commit, "
    "capability, policy_digest, approval_state, idempotency_key, evidence_ref, "
    "completion_ref, state, last_error, dispatched_by, created_at, updated_at"
)


def get_dispatch(conn: sqlite3.Connection, dispatch_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM icarus_dispatches WHERE id = ?", (dispatch_id,)
    ).fetchone()
    return dict(row) if row else None


def get_by_idempotency_key(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM icarus_dispatches WHERE idempotency_key = ?", (key,)
    ).fetchone()
    return dict(row) if row else None


def get_by_icarus_run(conn: sqlite3.Connection, icarus_run_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM icarus_dispatches WHERE icarus_run_id = ?",
        (icarus_run_id,),
    ).fetchone()
    return dict(row) if row else None


def list_dispatches(
    conn: sqlite3.Connection,
    *,
    work_item_id: int | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Newest first. Bounded, like every other operator read here."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    clauses: list[str] = []
    params: list[object] = []
    if work_item_id is not None:
        clauses.append("work_item_id = ?")
        params.append(int(work_item_id))
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(limit, 200))
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM icarus_dispatches{where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def fork_run_ids(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """The runs that named this dispatch's run as their parent.

    Derived from the activity trail rather than stored on the dispatch: lineage
    already lives in one place, and a second copy would be one more thing that can
    disagree with the log."""
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM activity "
        "WHERE parent_run_id = ? AND run_id IS NOT NULL ORDER BY run_id",
        (run_id,),
    ).fetchall()
    return [row["run_id"] for row in rows]


def create_dispatch(
    conn: sqlite3.Connection,
    *,
    work_item_id: int,
    run_id: str,
    parent_run_id: str | None,
    repo: str,
    base_commit: str,
    capability: str,
    policy_digest: str,
    approval_state: str,
    idempotency_key: str,
    dispatched_by: int,
) -> int:
    """Insert the record. Does not commit — the command owns the transaction."""
    cursor = conn.execute(
        "INSERT INTO icarus_dispatches "
        "(work_item_id, run_id, parent_run_id, repo, base_commit, capability, "
        "policy_digest, approval_state, idempotency_key, dispatched_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            work_item_id,
            run_id,
            parent_run_id,
            repo,
            base_commit,
            capability,
            policy_digest,
            approval_state,
            idempotency_key,
            dispatched_by,
        ),
    )
    dispatch_id = cursor.lastrowid
    assert dispatch_id is not None
    return int(dispatch_id)


def mark_accepted(
    conn: sqlite3.Connection, *, dispatch_id: int, icarus_run_id: str
) -> None:
    """The executor answered with its own run id. Its own transaction: this happens
    AFTER the dispatch record committed, because the outbound call must never run
    inside a write transaction."""
    with db.transaction(conn, immediate=True):
        conn.execute(
            "UPDATE icarus_dispatches SET state = ?, icarus_run_id = ?, "
            "last_error = NULL, updated_at = datetime('now') WHERE id = ?",
            (ACCEPTED, icarus_run_id, dispatch_id),
        )


def mark_undeliverable(
    conn: sqlite3.Connection, *, dispatch_id: int, reason: str
) -> None:
    """Athena could not hand the work over. The record stays — an operator needs to
    see that Athena tried and failed, not to find nothing at all."""
    with db.transaction(conn, immediate=True):
        conn.execute(
            "UPDATE icarus_dispatches SET state = ?, last_error = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (UNDELIVERABLE, reason[:MAX_REF_CHARS], dispatch_id),
        )


def record_evidence(
    conn: sqlite3.Connection, *, dispatch_id: int, evidence_ref: str
) -> None:
    """Point at what the executor produced. Does not commit."""
    conn.execute(
        "UPDATE icarus_dispatches SET evidence_ref = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (evidence_ref[:MAX_REF_CHARS], dispatch_id),
    )


def record_terminal(
    conn: sqlite3.Connection,
    *,
    dispatch_id: int,
    state: str,
    completion_ref: str | None,
) -> None:
    """The executor reported an outcome. Does not commit."""
    conn.execute(
        "UPDATE icarus_dispatches SET state = ?, completion_ref = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (state, (completion_ref or "")[:MAX_REF_CHARS] or None, dispatch_id),
    )


def envelope(dispatch: dict, *, fork_runs: list[str]) -> dict:
    """The adapter contract, exactly as transmitted.

    Every field is either something Athena produced or something the executor is
    expected to fill in later. There is no free-form payload: an envelope a reader
    cannot fully enumerate is one nobody can audit."""
    return {
        "schema": "athena.icarus_dispatch.v1",
        "dispatch_id": dispatch["id"],
        "work_item_id": dispatch["work_item_id"],
        "run_id": dispatch["run_id"],
        "parent_run_id": dispatch["parent_run_id"],
        "fork_run_ids": fork_runs,
        "icarus_run_id": dispatch["icarus_run_id"],
        "repo": dispatch["repo"],
        "base_commit": dispatch["base_commit"],
        "capability": dispatch["capability"],
        "policy_digest": dispatch["policy_digest"],
        "approval_state": dispatch["approval_state"],
        "idempotency_key": dispatch["idempotency_key"],
        "evidence_ref": dispatch["evidence_ref"],
        "completion_ref": dispatch["completion_ref"],
    }
