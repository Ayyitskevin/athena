"""Audited lifecycle for inbound event sources.

Registering a source is a trust decision: it hands someone a credential that
writes foreign history into this workspace. Every step of that decision —
granted, paused, resumed, revoked — is a command with an actor and an audit
event, for the same reason webhook registration is.

The secret never reaches the trail. The detail records the source's name, kind,
and host — *where events will come from* — not the credential that authenticates
them.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, event_sources

VERB_REGISTERED = "registered_event_source"
VERB_PAUSED = "paused_event_source"
VERB_RESUMED = "resumed_event_source"
VERB_DELETED = "deleted_event_source"


class EventSourceCommandError(Exception):
    """A transport-neutral rejection carrying the status the boundary should use."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _detail(source: dict) -> str:
    return f"{source['name']} ({source['kind']} @ {source['host']})"


def register_source(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    name: str,
    kind: str,
    host: str,
) -> dict:
    """Register a source and record its ``registered_event_source`` event atomically.

    Returns the source **with** its one-time secret, which is never written to the
    audit log and never readable again. Raises on an unknown dialect (so a request
    can never arrive for a parser that does not exist) and on a duplicate name (so
    a trail entry naming a source is unambiguous).
    """
    if not event_sources.valid_kind(kind):
        raise EventSourceCommandError(f"unknown source kind '{kind}'", status_code=422)
    with db.transaction(conn, immediate=True):
        if event_sources.get_source_by_name(conn, name) is not None:
            raise EventSourceCommandError(
                "an event source with that name already exists", status_code=409
            )
        created = event_sources.create_source(
            conn,
            name=name,
            kind=kind,
            host=host,
            created_by=actor_id,
            commit=False,
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_REGISTERED,
            target_kind="event_source",
            target_id=created["id"],
            detail=_detail(created),
            commit=False,
        )
        return created


def set_source_enabled(
    conn: sqlite3.Connection, *, actor_id: int, source_id: int, enabled: bool
) -> dict:
    """Pause or resume a source, recording the change only when it actually flips.

    Pausing does NOT rotate the secret: an operator silencing a noisy forge should
    be able to restore it without re-registering the webhook on the far side.
    """
    with db.transaction(conn, immediate=True):
        before = event_sources.get_source(conn, source_id)
        if before is None:
            raise EventSourceCommandError("no such event source", status_code=404)
        after = event_sources.set_enabled(conn, source_id, enabled, commit=False)
        assert after is not None
        if before["enabled"] != after["enabled"]:
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_RESUMED if after["enabled"] else VERB_PAUSED,
                target_kind="event_source",
                target_id=source_id,
                detail=_detail(after),
                commit=False,
            )
        return after


def delete_source(conn: sqlite3.Connection, *, actor_id: int, source_id: int) -> bool:
    """Revoke a source and record it. The history it already landed stays: those
    events happened and were authentic when recorded, and deleting the credential
    is not a reason to rewrite the trail."""
    with db.transaction(conn, immediate=True):
        before = event_sources.get_source(conn, source_id)
        if before is None:
            return False
        event_sources.delete_source(conn, source_id, commit=False)
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_DELETED,
            target_kind="event_source",
            target_id=source_id,
            detail=_detail(before),
            commit=False,
        )
        return True
