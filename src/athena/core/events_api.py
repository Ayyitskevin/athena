"""The event-feed REST API — the agent/integration view of the audit trail.

`/activity` (activity_api.py) serves humans scrolling recent history newest-first.
`/events` serves a *consumer*: an agent or integration that wants to drain the same
append-only trail in order and resume exactly where it left off. The audit log is
already an event log (every action has a monotonic id), so this is a pure read over
it — no new store, no duplicated truth.

Contract: pass the last event id you processed as `?after=`; you get the next batch
in ascending id order plus a `next_after` cursor and a `has_more` flag. Loop while
`has_more` to catch up, then poll `next_after` for new events. Authentication is the
gate, like the activity feed — a `read`-scoped token is enough.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from athena.core import activity
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/events", tags=["core"])


class EventOut(BaseModel):
    id: int
    actor_id: int
    actor_name: str
    verb: str
    target_kind: str
    target_id: int
    detail: str
    created_at: str
    # The run this event belongs to (the X-Athena-Run it was recorded under), or None
    # for untagged actions — so a consumer can group a stream into runs by id; and the
    # run that spawned that run (lineage), or None at top level.
    run_id: str | None = None
    parent_run_id: str | None = None


class EventFeed(BaseModel):
    # The batch, oldest-first, plus the cursor to resume from. next_after is the id
    # to pass as ?after= next time: the last event in this batch, or — when the
    # consumer is already caught up (empty batch) — the same cursor they sent, so a
    # poll loop is stable. has_more says another full batch is waiting right now
    # (drain it before polling).
    events: list[EventOut]
    next_after: int | None
    has_more: bool


@router.get("", response_model=EventFeed)
def events(
    after: int | None = Query(
        None, description="cursor: only events with id greater than this"
    ),
    kind: str | None = Query(
        None, alias="kind", description="filter to one target kind, e.g. 'issue'"
    ),
    target: int | None = Query(
        None, description="with kind, subscribe to one target's events"
    ),
    actor_id: int | None = Query(None, description="filter to one actor's actions"),
    actor_type: Literal["agent", "human"] | None = Query(
        None, description="filter by actor type: agents only, or humans only"
    ),
    run_id: str | None = Query(
        None, description="replay one run: exactly the events tagged with this id"
    ),
    parent_run_id: str | None = Query(
        None, description="lineage: the events of the child runs this run spawned"
    ),
    verb: str | None = Query(None, description="filter to one event type"),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # A target id without its kind is ambiguous — reject that half-query, exactly
    # as the activity feed does.
    if target is not None and kind is None:
        raise HTTPException(status_code=422, detail="target requires kind")
    # Over-fetch by one to learn whether a further batch is waiting without a second
    # count query (the same trick the web activity feed uses), then trim it off.
    rows = activity.list_events(
        conn,
        after_id=after,
        target_kind=kind,
        target_id=target,
        actor_id=actor_id,
        actor_is_agent=None if actor_type is None else actor_type == "agent",
        run_id=run_id,
        parent_run_id=parent_run_id,
        verb=verb,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    batch = rows[:limit]
    # Advance the cursor to the last delivered event; if nothing was delivered the
    # consumer is caught up, so hand back the cursor they came in with.
    next_after = batch[-1]["id"] if batch else after
    return {"events": batch, "next_after": next_after, "has_more": has_more}
