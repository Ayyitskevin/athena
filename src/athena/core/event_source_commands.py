"""Audited lifecycle for inbound event sources.

Registering a source is a trust decision: it hands someone a credential that
writes foreign history into this workspace. Every step of that decision —
granted, paused, resumed, revoked — is a command with an actor and an audit
event, for the same reason webhook registration is.

Because it is a trust decision, these commands OWN the authorization: each takes
a resolved actor and requires the admin role plus, for bearer callers, the admin
token scope — the same rule ``run_control_commands._admin`` applies to the other
operator-only command family. The transport's ``admin_actor`` dependency still
runs first for its wire statuses, but a caller reaching the command directly is
refused rather than trusted.

The secret never reaches the trail. The detail records the source's name, kind,
and host — *where events will come from* — not the credential that authenticates
them.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from athena.core import activity, db, event_sources, identity, tokens

VERB_REGISTERED = "registered_event_source"
VERB_PAUSED = "paused_event_source"
VERB_RESUMED = "resumed_event_source"
VERB_DELETED = "deleted_event_source"


ErrorKind = Literal["unauthorized", "forbidden", "invalid", "not_found", "conflict"]


class EventSourceCommandError(Exception):
    """A transport-neutral rejection.

    Carries a ``kind``, never an HTTP status: the boundary maps kind to its own
    status vocabulary (AGENTS.md's command-error dialect rule) without the command
    knowing a transport exists.
    """

    def __init__(self, kind: ErrorKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _detail(source: dict) -> str:
    return f"{source['name']} ({source['kind']} @ {source['host']})"


def _admin_or_refuse(actor: dict | None) -> dict:
    """Admin role plus, for bearer callers, the admin token scope — the rule
    ``run_control_commands._admin`` applies, applied here for the same reason:
    handing out or revoking an inbound-history credential is an operator act."""
    if actor is None:
        raise EventSourceCommandError("unauthorized", "authentication required")
    if not identity.is_admin(actor):
        raise EventSourceCommandError("forbidden", "admin role required")
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise EventSourceCommandError(
            "forbidden", f"token scope required: {tokens.ADMIN_SCOPE}"
        )
    return actor


def register_source(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    name: str,
    kind: str,
    host: str,
) -> dict:
    """Register a source and record its ``registered_event_source`` event atomically.

    Returns the source **with** its one-time secret, which is never written to the
    audit log and never readable again. Raises on a non-admin actor, on an unknown
    dialect (so a request can never arrive for a parser that does not exist), and
    on a duplicate name (so a trail entry naming a source is unambiguous).
    """
    actor_id = _admin_or_refuse(actor)["id"]
    if not event_sources.valid_kind(kind):
        raise EventSourceCommandError("invalid", f"unknown source kind '{kind}'")
    with db.transaction(conn, immediate=True):
        if event_sources.get_source_by_name(conn, name) is not None:
            raise EventSourceCommandError(
                "conflict", "an event source with that name already exists"
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
    conn: sqlite3.Connection, *, actor: dict | None, source_id: int, enabled: bool
) -> dict:
    """Pause or resume a source, recording the change only when it actually flips.

    Pausing does NOT rotate the secret: an operator silencing a noisy forge should
    be able to restore it without re-registering the webhook on the far side.
    """
    actor_id = _admin_or_refuse(actor)["id"]
    with db.transaction(conn, immediate=True):
        before = event_sources.get_source(conn, source_id)
        if before is None:
            raise EventSourceCommandError("not_found", "no such event source")
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


def delete_source(
    conn: sqlite3.Connection, *, actor: dict | None, source_id: int
) -> bool:
    """Revoke a source and record it. The history it already landed stays: those
    events happened and were authentic when recorded, and deleting the credential
    is not a reason to rewrite the trail."""
    actor_id = _admin_or_refuse(actor)["id"]
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
