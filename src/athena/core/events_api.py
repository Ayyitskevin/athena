"""The event-feed REST API — the agent/integration view of the audit trail.

`/activity` (activity_api.py) serves humans scrolling recent history newest-first.
`/events` serves a *consumer*: an agent or integration that wants to drain the same
append-only trail in order and resume exactly where it left off. The audit log is
already an event log (every action has a monotonic id), so this is a read over it —
no new store, no duplicated truth.

Contract: pass the last event id you processed as `?after=`; you get the next batch
in ascending id order plus a `next_after` cursor and a `has_more` flag. Loop while
`has_more` to catch up, then poll `next_after` for new events. Authentication is the
gate, like the activity feed — a `read`-scoped token is enough.

The read itself writes nothing. On entry, before it, the registered feed observers
(``feed_observers``) record what the clock made and nobody wrote down: a lapsed
lease the caller answers for becomes one ``lease_lapsed`` event (C4, D11), written
in its own transaction, so this read returns it. The observers run whatever the
filters, so a filtered read or wait still records the lapse and, when the event
matches its filter, returns it.

Two filters narrow the feed to what a waiting agent asked about (F8):
``project_id`` and ``mine=true`` (the issues the caller is assigned, delegated
or created, and those it delegated to another account). A filtered wait polls the filtered read, so only a matching event
wakes it. A project the caller cannot see answers exactly as one that does not
exist: an empty batch, and a wait that runs to its timeout.

An event that recorded a lease generation, a claim handoff token or a reported
verdict carries each as a field (``generation``, ``handoff_token``,
``verdict``), read from the facts its recorder wrote beside the prose (0086).
An event with none carries none of the keys, so it reads exactly as before.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from athena.core import access, activity, event_wait, feed_observers
from athena.core.deps import get_conn
from athena.core.identity import current_actor
from athena.core.ids import MAX_SQLITE_INTEGER
from athena.core.packet_time import Timestamp
from athena.core.refusals import Refusal

router = APIRouter(prefix="/events", tags=["core"])


class EventOut(BaseModel):
    id: int
    actor_id: int
    actor_name: str
    verb: str
    target_kind: str
    target_id: int
    detail: str
    created_at: Timestamp
    # Stored import provenance, not inferred from the verb. Any non-null value
    # marks an imported event; native events have None.
    imported_at: Timestamp | None = None
    # The run this event belongs to (the X-Athena-Run it was recorded under), or None
    # for untagged actions — so a consumer can group a stream into runs by id; and the
    # run that spawned that run (lineage), or None at top level.
    run_id: str | None = None
    parent_run_id: str | None = None
    # The parent activity event this event's run forked from, if known. This lets a
    # stream consumer place a child run at the exact branch point in its parent.
    forked_from_event_id: int | None = None
    # The facts the event's recorder wrote beside its prose detail (0086), as
    # fields: present only on an event that recorded them, absent (not null)
    # otherwise. `generation` is the lease generation a claim, renewal, lapse,
    # yield, handoff resume, completion or releasing decline names;
    # `handoff_token` the claim handoff a yield opened or a resume acknowledged;
    # `verdict` the verdict a completion reported. `generation` and
    # `handoff_token` go only to a caller who may read the issue, as
    # GET /issues/{id}/lease gives them.
    generation: str | None = None
    handoff_token: str | None = None
    verdict: str | None = None


class WaitOut(BaseModel):
    # What a parked request did: how long it was asked to wait, how long it did, and
    # why it came back. `event` — a visible batch landed; `timeout` — nothing visible
    # landed in time, the batch is empty and next_after is the cursor you sent;
    # `capacity` — every parking place was taken, so this is the immediate answer
    # (elapsed 0): back off before asking to wait again. `disconnected` is recorded
    # for completeness; nobody reads it.
    requested_seconds: int
    elapsed_seconds: float
    outcome: event_wait.WaitOutcome


class EventFeed(BaseModel):
    # The batch, oldest-first, plus the cursor to resume from. next_after is the id
    # to pass as ?after= next time: the last event in this batch, or — when the
    # consumer is already caught up (empty batch) — the same cursor they sent, so a
    # poll loop is stable. has_more says another full batch is waiting right now
    # (drain it before polling).
    events: list[EventOut]
    next_after: int | None
    has_more: bool
    # Present only when the request asked to wait (`wait` above 0). An immediate
    # read answers exactly as before: the key is absent, not null (the route
    # excludes unset fields, the same way the bulk issue routes do).
    wait: WaitOut | None = None


@router.get("", response_model=EventFeed, response_model_exclude_unset=True)
async def events(
    request: Request,
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
    project_id: int | None = Query(
        None,
        ge=1,
        le=MAX_SQLITE_INTEGER,
        description=(
            "only events of this project: its own, and those of the issues that "
            "were in it when they were recorded. A project you cannot see reads "
            "as one that does not exist: no events"
        ),
    ),
    mine: bool = Query(
        False,
        description=(
            "only events of the issues you are assigned, delegated or created, "
            "and those you delegated to another account, as they stand at this "
            "read"
        ),
    ),
    limit: int = Query(50, ge=1, le=200),
    wait: int = Query(
        0,
        ge=0,
        le=event_wait.EVENT_WAIT_MAX_SECONDS,
        description=(
            "seconds to park this request for a visible event before answering "
            "empty; 0 answers at once (see athena.core.event_wait)"
        ),
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # A target id without its kind is ambiguous — reject that half-query, exactly
    # as the activity feed does.
    if target is not None and kind is None:
        raise Refusal(
            status_code=422, detail="target requires kind", code="validation_error"
        )

    # Over-fetch by one to learn whether a further batch is waiting without a second
    # count query (the same trick the web activity feed uses), then trim it off.
    # The read runs on a worker thread, as it did when this handler was synchronous;
    # the coroutine only exists so a parked request can sleep without holding one.
    async def read_batch() -> list[dict]:
        return await asyncio.to_thread(
            activity.list_events,
            conn,
            after_id=after,
            target_kind=kind,
            target_id=target,
            actor_id=actor_id,
            actor_is_agent=None if actor_type is None else actor_type == "agent",
            run_id=run_id,
            parent_run_id=parent_run_id,
            verb=verb,
            project_id=project_id,
            mine_of=int(actor["id"]) if mine else None,
            limit=limit + 1,
            actor=actor,
        )

    # On entry, before the read: record what the clock made and nobody wrote
    # down, such as a lapsed lease this caller is accountable for (C4, D11), so
    # this very read can return it. Each observer reads without a transaction
    # and writes in its own; nothing below runs under the writer lock.
    await asyncio.to_thread(feed_observers.observe, conn, actor)

    waited: dict | None = None
    if wait <= 0:
        rows = await read_batch()
    else:
        rows, outcome, elapsed = await event_wait.wait_for_events(
            read_batch,
            timeout=wait,
            slots=event_wait.SLOTS,
            interval=event_wait.EVENT_WAIT_POLL_SECONDS,
            is_disconnected=request.is_disconnected,
        )
        waited = {
            "requested_seconds": wait,
            "elapsed_seconds": round(elapsed, 3),
            "outcome": outcome,
        }
        # Tell the slow-request timer how long this request parked on purpose, so
        # a wait is not reported as a slow read (middleware.SlowRequestLogMiddleware).
        request.state.parked_ms = elapsed * 1000.0
    has_more = len(rows) > limit
    batch = rows[:limit]
    # Advance the cursor to the last delivered event; if nothing was delivered the
    # consumer is caught up, so hand back the cursor they came in with.
    next_after = batch[-1]["id"] if batch else after
    if batch:
        batch = await asyncio.to_thread(_with_facts, conn, batch, actor)
    feed: dict = {"events": batch, "next_after": next_after, "has_more": has_more}
    if waited is not None:
        feed["wait"] = waited
    return feed


#: The facts a caller gets only when it may read the issue they belong to: the
#: lease generation and the handoff token, which GET /issues/{id}/lease and the
#: handoff history in an issue's work context give to exactly the accounts that
#: can read the issue. A handoff token names the context a later holder must
#: acknowledge; resuming it also takes that holder's own active lease, but the
#: token is still given no wider than the handoff itself.
_ISSUE_READER_FACTS = ("generation", "handoff_token")


def _with_facts(conn: sqlite3.Connection, batch: list[dict], actor: dict) -> list[dict]:
    """Each event of the batch with the facts recorded beside it (0086).

    Only the keys an event recorded are added, so an event with none is the very
    row it was before. The event gate has already dropped every event on an issue
    the caller cannot read; the issue check here states the handoff's own rule
    where the token is handed out, so no later change to the event gate can widen
    who receives it."""
    facts = activity.event_facts(conn, [row["id"] for row in batch])
    if not facts:
        return batch
    readable: dict[int, bool] = {}
    out: list[dict] = []
    for row in batch:
        recorded = facts.get(int(row["id"]))
        if not recorded:
            out.append(row)
            continue
        shown = dict(recorded)
        if any(name in shown for name in _ISSUE_READER_FACTS):
            issue_id = int(row["target_id"])
            if issue_id not in readable:
                readable[issue_id] = row["target_kind"] == "issue" and (
                    access.can_see_issue(conn, actor, issue_id)
                )
            if not readable[issue_id]:
                for name in _ISSUE_READER_FACTS:
                    shown.pop(name, None)
        out.append({**row, **shown})
    return out
