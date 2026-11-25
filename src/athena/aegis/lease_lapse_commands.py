"""A lapsed lease is an event: the one writer of ``lease_lapsed`` (C4, D11).

A lease's expiry makes "active" a pure function of the clock, so a lease whose
holder went away simply stops being active, and until this command nobody was
told. There is still no background sweeper. The first request that observes an
expired, unsettled lease records ONE ``lease_lapsed`` event for that lease
generation and puts it in the inboxes of the accounts accountable for the work:
the issue's assignee and the account that delegated it to the holder.

How it stays honest:

* **Once per generation.** The command re-reads the possession under SQLite's
  writer reservation (``BEGIN IMMEDIATE``) and writes only if no native
  ``lease_lapsed`` event names that generation. Writers are serialized, so a
  second observer, racing or later, finds the first one's event and writes
  nothing. A generation never becomes live again (migration 0057's triggers), so
  a reacquired lease that lapses later is a new generation and a new lapse.
* **Readers never hold the writer lock while they read.** An observer reads its
  candidates with no transaction open, and the command writes in its own
  immediate transaction, after the read it serves (the attention ranking) or
  before it (the event feed, on entry). A failed write (the lock is busy) is
  logged and left for the next observer: the read answers regardless.
* **The observer is the actor; the holder is the subject.** The holder did
  nothing; the event's actor is the account whose request observed the lapse,
  and the detail names the holder and the generation. The event carries no run
  coordinates: observing is not work in the observer's run.
* **Nothing is released or swept** (ruling K1). The lease row stays until a
  claimer or an operator settles it, exactly as before.
* **Hidden reads as missing.** An observer records only lapses on issues it may
  see, and a recipient who cannot see the issue gets no inbox row.

Automation never fires on the verb (``automation.OBSERVED_VERBS``), and the verb
is not work (``work_progress``) or a reason a schedule's inactivity clock resets.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from datetime import datetime
import logging
import sqlite3

from athena.aegis import issue_activity, leases
from athena.core import access, clock, db, notifications, run_context, users

_logger = logging.getLogger("athena")

#: At most this many lapses are recorded per observing request; the next
#: request picks up the rest. Keeps an observing read bounded however many
#: leases lapsed at once.
MAX_LAPSES_PER_OBSERVATION = 20


def _observer_id(actor: object) -> int | None:
    if not isinstance(actor, dict):
        return None
    actor_id = actor.get("id")
    if isinstance(actor_id, bool) or not isinstance(actor_id, int):
        return None
    return actor_id


@contextlib.contextmanager
def _untagged() -> Iterator[None]:
    """No run coordinates for the event: the lapse is not work done in the
    observing request's run, and a read may carry a run id it does not own."""
    run = run_context.set_run_id(None)
    parent = run_context.set_parent_run_id(None)
    fork = run_context.set_forked_from_event_id(None)
    try:
        yield
    finally:
        run_context.reset_forked_from_event_id(fork)
        run_context.reset_parent_run_id(parent)
        run_context.reset_run_id(run)


def unrecorded_lapses(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
    limit: int = MAX_LAPSES_PER_OBSERVATION,
) -> list[dict]:
    """What ``actor`` would observe: expired, unsettled, unrecorded leases on
    open issues it may see and is accountable for (it is the assignee, or it
    delegated the issue to the holder). The read half of an observer: it takes
    no lock and writes nothing."""
    actor_id = _observer_id(actor)
    if actor_id is None:
        return []
    return leases.unrecorded_lapses(
        conn, viewer=actor, accountable_id=actor_id, now=now, limit=limit
    )


def _recipients(conn: sqlite3.Connection, lapse: dict) -> list[int]:
    """The accountable accounts that may still see the issue."""
    recipients = []
    for user_id in {lapse["assignee_id"], lapse["delegated_by"]} - {None}:
        user = users.get_user(conn, int(user_id))
        if user is None or user.get("removed_at") is not None:
            continue
        if access.can_see_project_or_backlog(conn, user, lapse["project_id"]):
            recipients.append(int(user_id))
    return recipients


def record_lease_lapse(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    issue_id: int,
    generation: object,
) -> dict | None:
    """Record that ``generation`` of the issue's lease lapsed unsettled, as
    observed by ``actor``. Returns the event, or None when there is nothing to
    record: the possession is live, settled, replaced, on finished or archived
    work, hidden from the actor, or already recorded.

    Everything is re-read under the writer reservation, with the clock sampled
    after it is acquired, so the once-per-generation check and the write are one
    serialized step. The event and its inbox rows commit or roll back together.
    """
    actor_id = _observer_id(actor)
    if actor_id is None or not leases.is_valid_generation(generation):
        return None
    with _untagged(), db.transaction(conn, immediate=True):
        found = leases.unrecorded_lapses(
            conn,
            viewer=actor,
            accountable_id=None,
            now=clock.now(),
            limit=1,
            issue_id=issue_id,
            generation=generation,
        )
        if not found:
            return None
        lapse = found[0]
        event = issue_activity.record_lease_lapsed(
            conn,
            actor_id=actor_id,
            issue_id=issue_id,
            generation=generation,
            holder_id=int(lapse["holder_id"]),
            holder_name=str(lapse["holder_name"]),
            expired_at=str(lapse["expires_at"]),
            commit=False,
        )
        notifications.notify_users(
            conn, event_id=event["id"], user_ids=_recipients(conn, lapse)
        )
        return event


def observe_lapses(conn: sqlite3.Connection, actor: dict) -> list[dict]:
    """The observer a read path runs: record each lapse ``actor`` is
    accountable for and nobody has recorded, and return the events written.

    Call it with no transaction open, after the response's read snapshot has
    ended (or, on the event feed, before its read begins): the candidate read
    then takes no lock, and each write takes the writer lock only for itself.
    Best effort, like the security-event recorder: a write that fails is
    logged and left for the next observer, and never fails the read."""
    if conn.in_transaction:
        # Writing here would upgrade the caller's read snapshot to the writer
        # lock, which D11 forbids. A wiring mistake, so say so loudly, but the
        # read still answers.
        _logger.error("observe_lapses called inside an open transaction; skipped")
        return []
    recorded: list[dict] = []
    try:
        for lapse in unrecorded_lapses(conn, actor=actor):
            event = record_lease_lapse(
                conn,
                actor=actor,
                issue_id=int(lapse["issue_id"]),
                generation=lapse["generation"],
            )
            if event is not None:
                recorded.append(event)
    except Exception:  # noqa: BLE001 — the read must go out regardless
        if conn.in_transaction:
            conn.rollback()
        _logger.exception("could not record a lapsed lease; the next observer will")
    return recorded
