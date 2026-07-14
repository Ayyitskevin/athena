"""Application command for cooperative agent-run heartbeats."""
from __future__ import annotations

import sqlite3
from typing import Literal

from athena import config
from athena.core import agent_run_checkins, db, run_context, tokens

ErrorKind = Literal["unauthorized", "forbidden", "invalid", "capacity"]


class AgentRunCommandError(Exception):
    """A transport-neutral heartbeat rejection."""

    def __init__(self, kind: ErrorKind, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


_WRITE_SCOPES = frozenset(
    {tokens.ISSUE_WRITE_SCOPE, tokens.DOCS_WRITE_SCOPE, tokens.ADMIN_SCOPE}
)


def heartbeat(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    run_id: object,
) -> dict:
    """Create or refresh this bearer-authenticated agent's run check-in.

    Authentication metadata is accepted only from ``current_actor``: a caller without
    ``_token_id`` is a browser/session or trusted-header actor and is refused. The
    command repeats policy checks below the transport, then serializes the upsert with
    SQLite's writer reservation so concurrent first heartbeats converge on one row.
    """
    if actor is None:
        raise AgentRunCommandError("unauthorized", "authentication required")

    token_id = actor.get("_token_id")
    if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 1:
        raise AgentRunCommandError("forbidden", "bearer token required")
    if not actor.get("is_agent"):
        raise AgentRunCommandError("forbidden", "agent account required")

    scopes = actor.get("_token_scopes")
    if scopes is None or not _WRITE_SCOPES.intersection(scopes):
        raise AgentRunCommandError("forbidden", "token scope required: a write scope")

    try:
        normalized_run_id = run_context.strict_run_id(run_id)
    except ValueError as exc:
        raise AgentRunCommandError("invalid", str(exc)) from exc

    agent_id = actor.get("id")
    if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id < 1:
        raise AgentRunCommandError("forbidden", "agent account required")

    with db.transaction(conn, immediate=True):
        # Re-resolve both security facts under the same writer transaction as the
        # check-in. A fabricated/stale actor dict cannot bind another user's token or
        # keep reporting after the user is unmarked as an agent.
        owner = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND is_agent = 1", (agent_id,)
        ).fetchone()
        token = conn.execute(
            "SELECT scopes FROM api_tokens WHERE id = ? AND user_id = ? "
            "AND revoked_at IS NULL",
            (token_id, agent_id),
        ).fetchone()
        if owner is None:
            raise AgentRunCommandError("forbidden", "agent account required")
        if token is None:
            raise AgentRunCommandError("forbidden", "live bearer token required")
        try:
            live_scopes = tokens.parse_scopes(token["scopes"])
        except ValueError as exc:
            raise AgentRunCommandError(
                "forbidden", "token scope required: a write scope"
            ) from exc
        if not _WRITE_SCOPES.intersection(live_scopes):
            raise AgentRunCommandError(
                "forbidden", "token scope required: a write scope"
            )
        existing = conn.execute(
            "SELECT 1 FROM agent_run_checkins WHERE agent_id = ? AND run_id = ?",
            (agent_id, normalized_run_id),
        ).fetchone()
        if existing is None:
            row_count = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_run_checkins WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()["n"]
            if row_count >= config.AGENT_RUN_MAX_CHECKINS_PER_AGENT:
                raise AgentRunCommandError(
                    "capacity",
                    "agent run check-in capacity reached; refresh an existing run_id",
                )
        agent_run_checkins.upsert_checkin(
            conn, agent_id=agent_id, run_id=normalized_run_id, token_id=token_id
        )

    result = agent_run_checkins.get_checkin(
        conn, agent_id=agent_id, run_id=normalized_run_id
    )
    if result is None:  # defensive: the transaction just made this row durable
        raise RuntimeError("agent run check-in disappeared after commit")
    return result
