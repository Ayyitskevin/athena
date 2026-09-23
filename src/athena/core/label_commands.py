"""The audited write that grows the shared label vocabulary.

Attaching a label to an issue or a page already goes through those modules'
commands. ``POST /labels`` used to insert the row itself and record nothing,
so the trail could not say who added a name to the vocabulary everyone shares.
This command owns that insert and its ``label_created`` event.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, identity, labels, tokens, users

VERB_LABEL_CREATED = "label_created"


class LabelCommandError(Exception):
    """A transport-neutral refusal to grow the label vocabulary."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _live_writer(conn: sqlite3.Connection, actor: dict) -> dict:
    """Re-read the actor under the write lock and refuse a credential that no
    longer has the issue-write role and scope the vocabulary requires."""
    live = users.get_user(conn, actor["id"])
    if live is None:
        raise LabelCommandError("unauthorized", "authentication required")
    effective = {**actor, **live}
    if (
        effective.get("paused_at")
        or effective.get("removed_at")
        or not identity.can_write(effective)
        or not identity.token_has_scope(effective, tokens.ISSUE_WRITE_SCOPE)
    ):
        raise LabelCommandError(
            "forbidden", f"token scope required: {tokens.ISSUE_WRITE_SCOPE}"
        )
    return effective


def create_label(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    name: str,
    color: str | None = None,
) -> dict:
    """Insert one label and record who added it.

    A duplicate name — including the loser of a create race — is a conflict.
    The case-insensitive UNIQUE constraint is the backstop for that race.
    """
    cleaned = name.strip()
    if not cleaned:
        raise LabelCommandError("invalid", "label name is required")
    try:
        normalized = labels.normalize_color(color) if color is not None else None
    except ValueError as exc:
        raise LabelCommandError("invalid", str(exc)) from exc
    with db.transaction(conn, immediate=True):
        writer = _live_writer(conn, actor)
        if labels.get_label_by_name(conn, cleaned) is not None:
            raise LabelCommandError("conflict", "label already exists")
        try:
            created = labels.create_label(
                conn,
                name=cleaned,
                **({"color": normalized} if normalized is not None else {}),
                commit=False,
            )
        except sqlite3.IntegrityError as exc:
            raise LabelCommandError("conflict", "label already exists") from exc
        activity.record(
            conn,
            actor_id=writer["id"],
            verb=VERB_LABEL_CREATED,
            target_kind="label",
            target_id=int(created["id"]),
            detail=created["name"],
            commit=False,
        )
        return created
