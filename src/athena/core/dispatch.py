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
database" rules out. Callback v1 has no sender sequence, so `evidence_ref` is one
immutable canonical pointer: Athena can prove an exact retry, but it cannot invent
an ordering between two different pointers.

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

from athena.core import access

#: Athena's knowledge of a dispatch. Read these as "what has Athena been told".
PENDING_DELIVERY = "pending_delivery"
ACCEPTED = "accepted"
UNDELIVERABLE = "undeliverable"
COMPLETED = "completed"
FAILED = "failed"
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
MAX_SQLITE_INTEGER = (1 << 63) - 1
MAX_LIST_LIMIT = 200


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


_COLUMN_NAMES = (
    "id",
    "work_item_id",
    "run_id",
    "parent_run_id",
    "icarus_run_id",
    "repo",
    "base_commit",
    "capability",
    "policy_digest",
    "approval_state",
    "idempotency_key",
    "evidence_ref",
    "completion_ref",
    "state",
    "last_error",
    "dispatched_by",
    "created_at",
    "updated_at",
)
_COLUMNS = ", ".join(_COLUMN_NAMES)
_VISIBLE_COLUMNS = ", ".join(f"d.{column}" for column in _COLUMN_NAMES)
_VISIBLE_FROM = (
    " FROM icarus_dispatches AS d "
    "JOIN issues AS i ON i.id = d.work_item_id "
    "LEFT JOIN projects AS p ON p.id = i.project_id"
)


def _visibility_clause(actor: dict | None) -> tuple[str, list]:
    """One-snapshot issue visibility for operator-facing dispatch reads."""
    project_gate, params = access.project_visibility_clause(actor, alias="p")
    return (
        f"(i.project_id IS NULL OR (p.id IS NOT NULL AND ({project_gate})))",
        params,
    )


def get_dispatch(conn: sqlite3.Connection, dispatch_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM icarus_dispatches WHERE id = ?", (dispatch_id,)
    ).fetchone()
    return dict(row) if row else None


def get_visible_dispatch(
    conn: sqlite3.Connection,
    dispatch_id: int,
    *,
    actor: dict | None,
) -> dict | None:
    """Return a dispatch only when its issue is visible to ``actor``.

    The issue, project, membership, and dispatch are resolved in one SQL statement,
    so a privacy flip or membership revocation cannot race a later row fetch. Missing
    and hidden dispatches both return ``None`` for a non-enumerating adapter response.
    """
    if type(dispatch_id) is not int or not 1 <= dispatch_id <= MAX_SQLITE_INTEGER:
        return None
    visibility, visibility_params = _visibility_clause(actor)
    row = conn.execute(
        f"SELECT {_VISIBLE_COLUMNS}{_VISIBLE_FROM} WHERE d.id = ? AND {visibility}",
        [dispatch_id, *visibility_params],
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
    actor: dict | None,
    work_item_id: int | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Visible dispatches, newest first and bounded like every operator read.

    Visibility is part of the SQL before ``ORDER BY`` and ``LIMIT``. Filtering a
    bounded page in Python would let newer hidden rows starve visible results and
    turn page fullness into a private-work oracle.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if work_item_id is not None and (
        type(work_item_id) is not int or not 1 <= work_item_id <= MAX_SQLITE_INTEGER
    ):
        raise ValueError(f"work_item_id must be between 1 and {MAX_SQLITE_INTEGER}")
    visibility, visibility_params = _visibility_clause(actor)
    clauses = [visibility]
    params: list[object] = [*visibility_params]
    if work_item_id is not None:
        clauses.append("d.work_item_id = ?")
        params.append(int(work_item_id))
    if state is not None:
        clauses.append("d.state = ?")
        params.append(state)
    params.append(min(limit, MAX_LIST_LIMIT))
    rows = conn.execute(
        f"SELECT {_VISIBLE_COLUMNS}{_VISIBLE_FROM} "
        f"WHERE {' AND '.join(clauses)} ORDER BY d.id DESC LIMIT ?",
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


def digest_mismatch_recorded(conn: sqlite3.Connection, run_id: str) -> bool:
    """Whether this dispatch run already has its one mismatch warning.

    Callback retries are expected. The warning is a fact about the dispatch, not
    a counter of how many times the executor retried the same report.
    """
    row = conn.execute(
        "SELECT 1 FROM activity WHERE run_id = ? AND verb = ? LIMIT 1",
        (run_id, VERB_DIGEST_MISMATCH),
    ).fetchone()
    return row is not None


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
) -> bool:
    """Record one pending dispatch's acceptance without committing.

    The state predicate makes concurrent or repeated delivery outcomes
    first-writer-wins. The command owner uses the return value to emit exactly one
    audit event in the same transaction.
    """
    cur = conn.execute(
        "UPDATE icarus_dispatches SET state = ?, icarus_run_id = ?, "
        "last_error = NULL, updated_at = datetime('now') "
        "WHERE id = ? AND state = ?",
        (ACCEPTED, icarus_run_id, dispatch_id, PENDING_DELIVERY),
    )
    return cur.rowcount > 0


def mark_undeliverable(
    conn: sqlite3.Connection, *, dispatch_id: int, reason: str
) -> bool:
    """Record one pending dispatch's delivery failure without committing.

    The record stays — an operator needs to see that Athena tried and failed, not
    to find nothing at all. A concurrent accepted/settled row is never rolled
    backward to undeliverable.
    """
    cur = conn.execute(
        "UPDATE icarus_dispatches SET state = ?, last_error = ?, "
        "updated_at = datetime('now') WHERE id = ? AND state = ?",
        (UNDELIVERABLE, reason[:MAX_REF_CHARS], dispatch_id, PENDING_DELIVERY),
    )
    return cur.rowcount > 0


def record_evidence(
    conn: sqlite3.Connection, *, dispatch_id: int, evidence_ref: str
) -> bool:
    """Set the dispatch's canonical evidence pointer exactly once.

    Evidence is immutable because callback v1 carries no sequence that could
    prove one different pointer is newer than another. A terminal row whose
    pointer is still NULL may accept that one missing pointer so legacy
    progress/terminal reordering does not lose evidence; no value is ever
    overwritten. The return value lets the command emit an audit event only when
    a transition actually landed.
    """
    cur = conn.execute(
        "UPDATE icarus_dispatches SET evidence_ref = ?, updated_at = datetime('now') "
        "WHERE id = ? AND evidence_ref IS NULL",
        (evidence_ref[:MAX_REF_CHARS], dispatch_id),
    )
    return cur.rowcount > 0


def record_terminal(
    conn: sqlite3.Connection,
    *,
    dispatch_id: int,
    state: str,
    completion_ref: str | None,
) -> bool:
    """The executor reported an outcome. Does not commit.

    Predicated on the dispatch still being OPEN: a settled dispatch is never
    re-settled, so a late or replayed callback that RACES the first terminal
    report (both read the row as open before either committed) is a 0-row
    update — the accepted-and-ignored path — and returns False. The caller
    records its audit event only when this returns True, so a lost race can
    produce neither a flipped outcome nor a duplicate terminal event."""
    cur = conn.execute(
        "UPDATE icarus_dispatches SET state = ?, completion_ref = ?, "
        "updated_at = datetime('now') "
        "WHERE id = ? AND state NOT IN (?, ?)",
        (
            state,
            (completion_ref or "")[:MAX_REF_CHARS] or None,
            dispatch_id,
            *TERMINAL_STATES,
        ),
    )
    return cur.rowcount > 0


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
