"""The security-signals REST API — refusals, made visible.

`core/security_events.py` has recorded failed logins, revoked-token use, scope
denials, and paused-account refusals since they were added. They were always on
the activity trail and always filterable — but only by an operator who already
knew the four verb names and thought to look for them.

Probing before compromise is exactly the signal that must not require knowing to
grep, so it gets its own admin-only surface: one endpoint, one MCP tool, one
cockpit panel.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from athena.core import security_events
from athena.core.deps import get_conn
from athena.core.identity import admin_actor

router = APIRouter(prefix="/security", tags=["core"])


class SecurityEventOut(BaseModel):
    id: int
    # The account whose boundary was hit — the user whose password was guessed at,
    # the owner of the revoked token, the actor whose scope ran out. Attribution is
    # to the account INVOLVED, which is the best available truth for a refusal.
    actor_id: int
    actor_name: str
    actor_email: str
    actor_is_agent: bool
    verb: Literal[
        "login_failed",
        "revoked_token_used",
        "scope_denied",
        "paused_account_refused",
    ]
    target_kind: str
    target_id: int
    detail: str
    created_at: str


class SecurityCountsOut(BaseModel):
    login_failed: int
    revoked_token_used: int
    scope_denied: int
    paused_account_refused: int


@router.get("/events", response_model=list[SecurityEventOut])
def events(
    verb: str | None = Query(None, description="one of the four refusal verbs"),
    since: str | None = Query(
        None, description="server timestamp lower bound, 'YYYY-MM-DD HH:MM:SS'"
    ),
    limit: int = Query(50, ge=1, le=security_events.MAX_LIST_LIMIT),
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """Recent boundary refusals, newest first. Admin-only: a refusal names the
    account it happened to, and a list of who has been probing is operator
    intelligence, not general history."""
    try:
        return security_events.list_failures(conn, verb=verb, since=since, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/counts", response_model=SecurityCountsOut)
def counts(
    since: str | None = Query(None, description="server timestamp lower bound"),
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """How many of each refusal — the number the attention rollup shows. Always
    zero-filled, so a quiet fleet reads as an explicit zero."""
    return security_events.failure_counts(conn, since=since)
