"""Commands for the worker registry — heartbeat, kill request, and the answers.

Two directions meet here, and they are authorized in opposite ways.

*Upward* (a worker reporting itself) repeats `agent_run_commands.heartbeat`'s
discipline exactly: a bearer token, an agent account, a live write scope, all
re-resolved inside the write transaction, and the client naming only its own
worker — never an actor, never a timestamp. A worker registers itself; nobody
registers a worker on someone else's behalf.

*Downward* (the operator asking a worker to stop) is admin-only and audited. It
writes an instruction; it does not, and cannot, end a process.

Each durable write here is one command owning authorization, validation, the
mutation, and its activity event in one transaction.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from athena import config
from athena.core import activity, db, identity, tokens, workers

ErrorKind = Literal["unauthorized", "forbidden", "not_found", "invalid", "capacity"]

VERB_REGISTERED = "worker_registered"
VERB_KILL_REQUESTED = "worker_kill_requested"
VERB_KILL_CANCELLED = "worker_kill_cancelled"
VERB_KILL_ACKNOWLEDGED = "worker_kill_acknowledged"
VERB_STOPPED = "worker_stopped"

# What a worker may say about itself on a heartbeat.
STATE_RUNNING = "running"
STATE_STOPPING = "stopping"
STATE_STOPPED = "stopped"
WORKER_STATES = (STATE_RUNNING, STATE_STOPPING, STATE_STOPPED)

_WRITE_SCOPES = frozenset(
    {tokens.ISSUE_WRITE_SCOPE, tokens.DOCS_WRITE_SCOPE, tokens.ADMIN_SCOPE}
)


class WorkerCommandError(Exception):
    """A transport-neutral worker-command rejection."""

    def __init__(self, kind: ErrorKind, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


STATUS_BY_KIND: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid": 422,
    "capacity": 429,
}


def _agent_credentials(actor: dict | None) -> tuple[int, int]:
    """The (agent_id, token_id) of a worker's own credential, or a refusal.

    A browser session or the trusted actor header carries no token, so it cannot
    heartbeat: a worker is a process holding a credential, and letting a human
    session impersonate one would put fiction in the registry."""
    if actor is None:
        raise WorkerCommandError("unauthorized", "authentication required")
    token_id = actor.get("_token_id")
    if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 1:
        raise WorkerCommandError("forbidden", "bearer token required")
    if not actor.get("is_agent"):
        raise WorkerCommandError("forbidden", "agent account required")
    scopes = actor.get("_token_scopes")
    if scopes is None or not _WRITE_SCOPES.intersection(scopes):
        raise WorkerCommandError("forbidden", "token scope required: a write scope")
    agent_id = actor.get("id")
    if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id < 1:
        raise WorkerCommandError("forbidden", "agent account required")
    return agent_id, token_id


def _recheck_credentials(
    conn: sqlite3.Connection, *, agent_id: int, token_id: int
) -> None:
    """Re-resolve both security facts inside the write transaction, so a stale or
    fabricated actor dict cannot keep reporting after the token was revoked or the
    account was unmarked as an agent. Mirrors agent_run_commands."""
    owner = conn.execute(
        "SELECT 1 FROM users WHERE id = ? AND is_agent = 1", (agent_id,)
    ).fetchone()
    token = conn.execute(
        "SELECT scopes FROM api_tokens WHERE id = ? AND user_id = ? "
        "AND revoked_at IS NULL",
        (token_id, agent_id),
    ).fetchone()
    if owner is None:
        raise WorkerCommandError("forbidden", "agent account required")
    if token is None:
        raise WorkerCommandError("forbidden", "live bearer token required")
    try:
        live_scopes = tokens.parse_scopes(token["scopes"])
    except ValueError as exc:
        raise WorkerCommandError(
            "forbidden", "token scope required: a write scope"
        ) from exc
    if not _WRITE_SCOPES.intersection(live_scopes):
        raise WorkerCommandError("forbidden", "token scope required: a write scope")


def _worker_key(raw: object) -> str:
    if not isinstance(raw, str):
        raise WorkerCommandError("invalid", "worker_key must be a string")
    key = raw.strip()
    if not key or len(key) > 200:
        raise WorkerCommandError("invalid", "worker_key must be 1-200 characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in key):
        raise WorkerCommandError(
            "invalid", "worker_key must not contain control characters"
        )
    return key


def _node_label(raw: object) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise WorkerCommandError("invalid", "node_label must be a string")
    label = raw.strip()
    if len(label) > 200:
        raise WorkerCommandError("invalid", "node_label must be <= 200 characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in label):
        raise WorkerCommandError(
            "invalid", "node_label must not contain control characters"
        )
    return label


def heartbeat(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    worker_key: object,
    node_label: object = None,
    capabilities: object = None,
    state: object = STATE_RUNNING,
) -> dict:
    """Register or refresh this agent's worker, and tell it what the operator said.

    The response carries ``kill_requested``: this is the entire actionable half of
    the feature. Athena cannot signal the worker's process, so the worker learns it
    was asked to stop by coming back and being told.

    ``state`` is the worker's own report about itself — ``stopping`` records that it
    heard the request, ``stopped`` that it finished. Both are claims, recorded as
    such. A worker that reports ``running`` again after claiming it stopped clears
    only that claim; the operator's instruction stands.
    """
    agent_id, token_id = _agent_credentials(actor)
    key = _worker_key(worker_key)
    label = _node_label(node_label)
    try:
        joined_capabilities = workers.join_capabilities(capabilities)
    except ValueError as exc:
        raise WorkerCommandError("invalid", str(exc)) from exc
    if state is None:
        state = STATE_RUNNING
    if state not in WORKER_STATES:
        raise WorkerCommandError(
            "invalid", f"state must be one of: {', '.join(WORKER_STATES)}"
        )

    with db.transaction(conn, immediate=True):
        _recheck_credentials(conn, agent_id=agent_id, token_id=token_id)
        existing = conn.execute(
            "SELECT id FROM agent_workers WHERE agent_id = ? AND worker_key = ?",
            (agent_id, key),
        ).fetchone()
        if existing is None:
            if workers.count_workers(conn, agent_id=agent_id) >= (
                config.WORKER_MAX_PER_AGENT
            ):
                raise WorkerCommandError(
                    "capacity",
                    "worker registry capacity reached; refresh an existing worker_key",
                )
        worker_id = workers.upsert_worker(
            conn,
            agent_id=agent_id,
            worker_key=key,
            node_label=label,
            capabilities=joined_capabilities,
            token_id=token_id,
        )
        if existing is None:
            # A new node appearing in the fleet is a fact the operator should be
            # able to read later, so first registration joins the trail. Refreshes
            # do not: a heartbeat every few seconds is operational state, and
            # recording each one would drown the log it is supposed to inform.
            activity.record(
                conn,
                actor_id=agent_id,
                verb=VERB_REGISTERED,
                target_kind="worker",
                target_id=worker_id,
                detail=label or key,
                commit=False,
            )
        current = workers.get_worker(conn, worker_id)
        assert current is not None
        if state == STATE_STOPPING and current["kill_acknowledged_at"] is None:
            if current["kill_requested_at"] is None:
                # Stopping on its own initiative is legitimate — a worker may shut
                # down for its own reasons — but there is no instruction to
                # acknowledge, so nothing is recorded as one.
                pass
            else:
                workers.set_kill_acknowledged(conn, worker_id=worker_id)
                activity.record(
                    conn,
                    actor_id=agent_id,
                    verb=VERB_KILL_ACKNOWLEDGED,
                    target_kind="worker",
                    target_id=worker_id,
                    detail=label or key,
                    commit=False,
                )
        if state == STATE_STOPPED and current["stopped_at"] is None:
            if current["kill_requested_at"] is not None:
                workers.set_kill_acknowledged(conn, worker_id=worker_id)
            workers.set_stopped(conn, worker_id=worker_id)
            activity.record(
                conn,
                actor_id=agent_id,
                verb=VERB_STOPPED,
                target_kind="worker",
                target_id=worker_id,
                detail=label or key,
                commit=False,
            )
        final = workers.get_worker(conn, worker_id)
        assert final is not None
        return final


def _admin(actor: dict | None) -> dict:
    if actor is None:
        raise WorkerCommandError("unauthorized", "authentication required")
    if not identity.is_admin(actor):
        raise WorkerCommandError("forbidden", "admin role required")
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise WorkerCommandError(
            "forbidden", f"token scope required: {tokens.ADMIN_SCOPE}"
        )
    return actor


def _locked_worker(conn: sqlite3.Connection, worker_id: int) -> dict:
    worker = workers.get_worker(conn, worker_id)
    if worker is None:
        raise WorkerCommandError("not_found", "no such worker")
    return worker


def request_kill(
    conn: sqlite3.Connection, *, actor: dict | None, worker_id: int
) -> dict:
    """Ask a worker to stop, atomically with its audit event.

    This writes an instruction. It does not end a process, and the returned
    ``kill_state`` says only ``requested`` until the worker itself comes back and
    says otherwise. Idempotent: re-asking a worker that has not answered records
    nothing new rather than resetting the clock on how long it has been ignoring
    you — which is the number the operator is actually watching."""
    admin = _admin(actor)
    with db.transaction(conn, immediate=True):
        worker = _locked_worker(conn, worker_id)
        if worker["kill_requested_at"] is not None and worker["stopped_at"] is None:
            return worker
        workers.set_kill_requested(conn, worker_id=worker_id, actor_id=admin["id"])
        if worker["stopped_at"] is not None:
            # It said it stopped and is being told again — the operator evidently
            # does not believe it, or it came back. Clear the stale claim so the
            # registry does not read as "stopped" while an instruction is live.
            conn.execute(
                "UPDATE agent_workers SET stopped_at = NULL, "
                "kill_acknowledged_at = NULL WHERE id = ?",
                (worker_id,),
            )
        activity.record(
            conn,
            actor_id=admin["id"],
            verb=VERB_KILL_REQUESTED,
            target_kind="worker",
            target_id=worker_id,
            detail=worker["node_label"] or worker["worker_key"],
            commit=False,
        )
        updated = workers.get_worker(conn, worker_id)
        assert updated is not None
        return updated


def cancel_kill(
    conn: sqlite3.Connection, *, actor: dict | None, worker_id: int
) -> dict:
    """Withdraw a kill request the worker has not answered yet.

    Refused once acknowledged: the worker has already been told and may already be
    shutting down, so "never mind" would be a claim about the world Athena cannot
    make. Restarting a stopped worker is the operator's move, not an undo."""
    admin = _admin(actor)
    with db.transaction(conn, immediate=True):
        worker = _locked_worker(conn, worker_id)
        if worker["kill_requested_at"] is None:
            return worker
        if worker["kill_acknowledged_at"] is not None:
            raise WorkerCommandError(
                "invalid",
                "the worker already acknowledged this request; it cannot be withdrawn",
            )
        workers.clear_kill_requested(conn, worker_id=worker_id)
        activity.record(
            conn,
            actor_id=admin["id"],
            verb=VERB_KILL_CANCELLED,
            target_kind="worker",
            target_id=worker_id,
            detail=worker["node_label"] or worker["worker_key"],
            commit=False,
        )
        updated = workers.get_worker(conn, worker_id)
        assert updated is not None
        return updated


def visible_worker(conn: sqlite3.Connection, *, actor: dict, worker_id: int) -> dict:
    """One worker, for an admin or for the agent that owns it.

    An agent may read its own workers — it needs to, to learn it was asked to stop
    — and nobody else's. A worker belonging to another agent is the same 404 a
    missing one gives."""
    worker = workers.get_worker(conn, worker_id)
    if worker is None:
        raise WorkerCommandError("not_found", "no such worker")
    if identity.is_admin(actor) or worker["agent_id"] == actor.get("id"):
        return worker
    raise WorkerCommandError("not_found", "no such worker")


def readable_workers(
    conn: sqlite3.Connection, *, actor: dict, agent_id: int | None = None, limit: int
) -> list[dict]:
    """The fleet for an admin; an agent's own workers for anyone else."""
    if identity.is_admin(actor):
        return workers.list_workers(conn, agent_id=agent_id, limit=limit)
    own = actor.get("id")
    if agent_id is not None and agent_id != own:
        return []
    return workers.list_workers(conn, agent_id=own, limit=limit)
