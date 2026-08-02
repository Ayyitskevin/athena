"""Application commands for audited webhook lifecycle changes.

Registering a webhook makes the server push every event it sees to an outbound URL;
pausing, resuming, or deleting one changes that outbound exposure. All four were bare
writes with NO activity event — an admin (human or agent) could stand up an
exfiltration endpoint, or quietly tear one down, with zero trace in the append-only
log. These commands own that write: the row change and its audit event run in one
db.transaction, so a webhook can never appear, flip, or vanish without its record.

Same shape as core/token_commands.py. Authorization (admin-only) and the SSRF URL
guard (is_safe_url) stay at the transport boundary, as they do today; the command
owns the write and the audit emission. The one-time signing secret returned by a
registration is NEVER written to the log — the detail records the URL (and event
filter) only.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, webhooks

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_REGISTERED = "registered_webhook"
VERB_PAUSED = "paused_webhook"
VERB_RESUMED = "resumed_webhook"
VERB_DELETED = "deleted_webhook"


class WebhookCommandError(Exception):
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND``, so the
    command layer states what went wrong and the transport decides how to say
    it — the same shape every command module written since uses."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _detail(url: str, event_kind: str | None) -> str:
    return url if not event_kind else f"{url} (event_kind={event_kind})"


def register_webhook(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    url: str,
    event_kind: str | None = None,
) -> dict:
    """Register a webhook and record a 'registered_webhook' event atomically.

    The one-time signing secret rides back in the returned dict but is NEVER written
    to the audit log — the detail records the outbound URL and its event filter only,
    so the trail shows WHERE events will be pushed, not the credential that signs them.
    URL safety (SSRF) is the caller's guard, applied before this runs.

    The command owns the "start at tip" contract: because recording the registration
    itself advances the activity tip, the cursor is finalized to that new tip, so the
    fresh endpoint replays no prior history AND is never notified of its own creation
    (its own registration event sits at or below the cursor). The whole sequence —
    row, audit event, cursor — lands in one transaction."""
    with db.transaction(conn, immediate=True):
        created = webhooks.create_webhook(
            conn,
            url=url,
            event_kind=event_kind,
            created_by=actor_id,
            start_cursor=webhooks.current_tip(conn),
            commit=False,
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_REGISTERED,
            target_kind="webhook",
            target_id=created["id"],
            detail=_detail(created["url"], created["event_kind"]),
            commit=False,
        )
        # The registration event just moved the tip past the webhook's start_cursor;
        # re-point the cursor at it so the endpoint starts strictly AFTER its own
        # registration (no history replay, no self-notification). The one-time secret
        # from the create call is preserved onto the refreshed row.
        finalized = webhooks.reset_cursor(
            conn, created["id"], webhooks.current_tip(conn), commit=False
        )
        assert finalized is not None
        return {**finalized, "secret": created["secret"]}


def set_webhook_active(
    conn: sqlite3.Connection, *, actor_id: int, webhook_id: int, active: bool
) -> dict:
    """Pause or resume a webhook and record the change atomically. Records a
    'paused_webhook' / 'resumed_webhook' event only when the active state actually
    flips (re-pausing an already-paused endpoint is not a lifecycle moment). Raises
    WebhookCommandError(404) for an unknown webhook."""
    with db.transaction(conn, immediate=True):
        before = webhooks.get_webhook(conn, webhook_id)
        if before is None:
            raise WebhookCommandError("not_found", "no such webhook")
        updated = webhooks.set_webhook_active(conn, webhook_id, active, commit=False)
        if updated is None:
            raise WebhookCommandError("not_found", "no such webhook")
        if bool(before["active"]) != bool(updated["active"]):
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_RESUMED if updated["active"] else VERB_PAUSED,
                target_kind="webhook",
                target_id=webhook_id,
                detail=_detail(updated["url"], updated["event_kind"]),
                commit=False,
            )
        return updated


def delete_webhook(conn: sqlite3.Connection, *, actor_id: int, webhook_id: int) -> bool:
    """Delete a webhook and record a 'deleted_webhook' event atomically. Returns True
    if one was removed, False if there was no such webhook (so the caller can 404). No
    event is recorded when nothing was deleted. The webhook is read first so the audit
    detail can name the URL that is going away."""
    with db.transaction(conn, immediate=True):
        target = webhooks.get_webhook(conn, webhook_id)
        removed = webhooks.delete_webhook(conn, webhook_id, commit=False)
        if removed and target is not None:
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_DELETED,
                target_kind="webhook",
                target_id=webhook_id,
                detail=_detail(target["url"], target["event_kind"]),
                commit=False,
            )
        return removed
