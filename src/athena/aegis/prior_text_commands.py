"""Removing the text one issue edit replaced (decision 6, 2026-11-26).

``issue_activity.record_edited`` keeps the title and body each edit replaced
(F10, D20), so editing a secret out of an issue no longer removes it from those
who can read the edit's prior text. This is the one way to take it back: an
administrator removes one edit's kept text, and the trail records that they
did. The command takes the resolved actor and owns its gate inside its own
transaction (docs/COMMAND_MIGRATION.md, slice rule 1).

- Only a live, unpaused administrator may remove. Anyone else is ``forbidden``.
- A missing issue, an event of another issue, an imported edit, an edit
  recorded before prior texts were kept and a text already removed all read
  the same: ``not_found``.
- The ``issue_prior_text_removed`` event's detail is ``edit <event id>``: it names
  the edit and the fields removed live nowhere but in this answer. Migration
  0096's trigger refuses the delete unless that event is already on the trail.

The issue's current title and body are untouched, and the edit's own event
stays as it was (the hash chain covers it, not the kept text). Undo of that
edit then refuses, as it does for an edit that never had a kept text.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issue_activity, issues
from athena.core import activity, db, identity, users

VERB_PRIOR_TEXT_REMOVED = "issue_prior_text_removed"


class PriorTextCommandError(Exception):
    """A transport-neutral refusal carrying a kind, never a status code."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def removal_detail(event_id: int) -> str:
    """The removal event's detail: which edit, and nothing of its text."""
    return f"edit {event_id}"


def remove_prior_text(
    conn: sqlite3.Connection, *, actor: dict, issue_id: int, event_id: int
) -> dict:
    """Remove the text one ``issue_edited`` event of this issue replaced, with
    its ``issue_prior_text_removed`` event, atomically. Returns
    ``{"event_id", "removed"}``, ``removed`` naming the fields whose prior text
    is gone."""
    with db.transaction(conn, immediate=True):
        live = users.get_user(conn, int(actor["id"]))
        if (
            live is None
            or live.get("removed_at")
            or live.get("paused_at")
            or not identity.is_admin(live)
        ):
            raise PriorTextCommandError(
                "forbidden", "only an administrator may remove a kept prior text"
            )
        fact = issue_activity.prior_text_for_event(conn, event_id)
        if (
            fact is None
            or int(fact["issue_id"]) != issue_id
            or issues.get_issue(conn, issue_id) is None
        ):
            raise PriorTextCommandError(
                "not_found", "no prior text for that edit of this issue"
            )
        activity.record(
            conn,
            actor_id=int(live["id"]),
            verb=VERB_PRIOR_TEXT_REMOVED,
            target_kind="issue",
            target_id=issue_id,
            detail=removal_detail(event_id),
            commit=False,
        )
        issue_activity.delete_prior_text(conn, event_id)
        return {"event_id": event_id, "removed": list(fact["changed"])}
