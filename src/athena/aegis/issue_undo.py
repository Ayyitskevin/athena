"""Aegis's inverses — how issue events are undone (`core/undo.py`).

Registration lives here rather than in ``core`` because the inverse of a domain
write is domain knowledge, and because ``core`` may not import ``aegis`` under the
package's import contract. ``main.py``, the composition root, calls
:func:`register`.

Every compensator delegates to the ordinary command owner, passing the UNDOING
actor. That is what makes "authorization is re-evaluated, not inherited" true
rather than aspirational: the command applies its own role, scope, and visibility
gates, and an actor who could not archive the issue themselves cannot reach it
through undo either.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import api as issue_api
from athena.aegis import issue_activity, issue_commands, issues
from athena.core import labels, undo


def _refusal(exc: issue_commands.IssueCommandError) -> undo.UndoRefused:
    """Re-raise a command's own rejection as an undo refusal.

    ``IssueCommandError`` is transport-neutral and every REST route translates it
    at the boundary — but the undo route lives in ``core`` and cannot import
    ``aegis`` to do the same. So the translation happens here, on the aegis side of
    the wall, reusing the SAME status mapping the issue API uses rather than a
    second one that could drift. The command's message survives intact: an actor
    refused for role, scope, or visibility should read the real reason."""
    return undo.UndoRefused(
        exc.detail,
        code=undo.COMMAND_REFUSED_CODE,
        status_code=issue_api.issue_command_status(exc),
    )


def _label_id(conn: sqlite3.Connection, event: dict) -> int:
    """The label an issue label event names.

    The event stores the label's NAME, and names are unique (0007, COLLATE
    NOCASE), so this is an exact lookup rather than an interpretation of prose. A
    label that has since been deleted, or an event recorded with an empty name
    because the label was already gone, resolves to nothing — refused as
    irreversible instead of guessing which label was meant."""
    label = labels.get_label_by_name(conn, event["detail"]) if event["detail"] else None
    if label is None:
        raise undo.UndoRefused(
            f"the label '{event['detail']}' no longer exists",
            code=undo.NO_EFFECT_CODE,
            status_code=409,
        )
    return int(label["id"])


def _set_archived(
    conn: sqlite3.Connection, actor: dict | None, event: dict, *, archived: bool
) -> None:
    try:
        issue_commands.set_issue_archived(
            conn, actor=actor, issue_id=event["target_id"], archived=archived
        )
    except issue_commands.IssueCommandError as exc:
        raise _refusal(exc) from exc


def _unarchive_issue(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    _set_archived(conn, actor, event, archived=False)


def _archive_issue(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    _set_archived(conn, actor, event, archived=True)


def _restore_status(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    """Put an issue back into the status a ``changed_status`` event moved it out of.

    The prior status is read from ``issue_lifecycle_facts`` (migration 0055), which
    has recorded ``before_status`` structurally, immutably, and in the same
    transaction as the event since long before undo existed. The event's own
    ``detail`` says the same thing as prose — and is never used here, because
    driving a write from a human-readable string is exactly what makes a compensator
    untrustworthy.

    Two gates that the label and archive inverses do not need, because a scalar
    field has no domain idempotency to fall back on:

    * **Still in force.** ``update_issue`` records an event for any real change, so
      if the issue has moved on to a third status the engine's "recorded nothing
      means nothing to undo" net (``core/undo.undo``) would NOT fire — it would
      happily write the old value over the newer one and stamp that as a reversal.
      The trail would then assert it reversed ``open → in_progress`` while actually
      discarding ``done``. So the effect must still be in force: the issue's current
      status has to be the one this event produced.
    * **Same access envelope.** Statuses are per-project (0024) and moving an issue
      between projects REMAPS its status. A ``before_status`` captured under one
      project is not meaningful under another, so a scope change refuses rather than
      writing a name that means something different (or nothing) where it landed.

    Authorization is not checked here at all: ``issue_commands.update_issue`` owns
    role, scope, visibility, budget, and the approval gate, and running through it as
    the undoing actor is what makes those apply to undo too. Re-opening a closed
    issue is an ordinary status write and is metered and gated like one.
    """
    fact = issue_activity.lifecycle_fact_for_event(conn, event["id"])
    if fact is None or fact["event_kind"] != "status_transition":
        raise undo.UndoRefused(
            "no structured prior status was recorded for this event",
            code=undo.NOT_REVERSIBLE_CODE,
            status_code=422,
        )
    issue = issues.get_issue(conn, event["target_id"])
    if issue is None:
        raise undo.UndoRefused(
            "the issue no longer exists",
            code=undo.NO_EFFECT_CODE,
            status_code=409,
        )
    if issue["status"] != fact["after_status"]:
        raise undo.UndoRefused(
            f"the issue has since moved to '{issue['status']}'; "
            f"this event's change to '{fact['after_status']}' is no longer in force",
            code=undo.NO_EFFECT_CODE,
            status_code=409,
        )
    if (
        issue_activity.project_scope_key(conn, issue["project_id"])
        != fact["after_project_scope_key"]
    ):
        raise undo.UndoRefused(
            "the issue has moved to another project since this event, "
            "where its previous status may not exist or may mean something else",
            code=undo.NO_EFFECT_CODE,
            status_code=409,
        )
    try:
        issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=event["target_id"],
            status=fact["before_status"],
        )
    except issue_commands.IssueCommandError as exc:
        raise _refusal(exc) from exc


def _detach_label(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    try:
        issue_commands.detach_label(
            conn,
            actor=actor,
            issue_id=event["target_id"],
            label_id=_label_id(conn, event),
        )
    except issue_commands.IssueCommandError as exc:
        raise _refusal(exc) from exc


def _attach_label(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    try:
        issue_commands.attach_label(
            conn,
            actor=actor,
            issue_id=event["target_id"],
            label_id=_label_id(conn, event),
        )
    except issue_commands.IssueCommandError as exc:
        raise _refusal(exc) from exc


def register() -> None:
    """Wire Aegis's inverses into the undo engine. Idempotent."""
    undo.register("archived", action_class=undo.TWO_WAY, compensator=_unarchive_issue)
    undo.register("unarchived", action_class=undo.TWO_WAY, compensator=_archive_issue)
    undo.register("labeled", action_class=undo.TWO_WAY, compensator=_detach_label)
    undo.register("unlabeled", action_class=undo.TWO_WAY, compensator=_attach_label)
    undo.register(
        "changed_status", action_class=undo.TWO_WAY, compensator=_restore_status
    )

    # Classified, deliberately not reversible. Naming them is the point: a surface
    # can say WHY there is no undo instead of leaving the verb unexplained, and the
    # list is a standing invitation to argue with a specific decision.
    undo.register(
        "commented",
        action_class=undo.ONE_WAY,
        reason="people have read it; delete the comment explicitly instead",
    )
    undo.register(
        "added_attachment",
        action_class=undo.ONE_WAY,
        reason="the blob was published; remove the attachment explicitly instead",
    )
    undo.register(
        "removed_attachment",
        action_class=undo.TRAPDOOR,
        reason="the stored blob is gone; re-upload the file instead",
    )
    undo.register(
        "deleted_project",
        action_class=undo.TRAPDOOR,
        reason="the project row and its access envelope are destroyed",
    )
    undo.register(
        "overrode_blocked_issue_close",
        action_class=undo.ONE_WAY,
        reason="a policy override is a decision on the record, not a state to flip",
    )
