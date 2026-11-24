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

from athena.aegis import rest_support
from athena.aegis import issue_activity, issue_commands, issue_etags, issues, statuses
from athena.core import approvals, labels, undo


def _refusal(exc: issue_commands.IssueCommandError) -> undo.UndoRefused:
    """Re-raise a command's own rejection as an undo refusal.

    ``IssueCommandError`` is transport-neutral and every REST route translates it
    at the boundary — but the undo route lives in ``core`` and cannot import
    ``aegis`` to do the same. So the translation happens here, on the aegis side of
    the wall, reusing the SAME status mapping the issue routes use rather than a
    second one that could drift. The command's message survives intact: an actor
    refused for role, scope, or visibility should read the real reason."""
    return undo.UndoRefused(
        exc.detail,
        code=undo.COMMAND_REFUSED_CODE,
        status_code=rest_support.issue_command_status(exc),
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


# --- Prechecks (U9): what each compensator would refuse, asked without writing.
#
# ``undo.offer`` runs these so the issue page offers undo only where the engine
# would accept it. Each is the compensating command's own write gate
# (``get_writable_issue``: visible, writer role and scope, creator/assignee/
# contributor/admin) plus the still-in-force condition the compensator or the
# command's idempotency enforces. The status and assignee compensators call the
# same in-force helpers, so the offer and the act cannot drift. Not covered: a
# durable budget or an approval gate on the inverse write (see undo.offer).


def _writable(conn: sqlite3.Connection, actor: dict | None, event: dict) -> dict:
    try:
        return issue_commands.get_writable_issue(
            conn, actor=actor, issue_id=event["target_id"]
        )
    except issue_commands.IssueCommandError as exc:
        raise _refusal(exc) from exc


def _no_longer_in_force() -> undo.UndoRefused:
    return undo.UndoRefused(
        undo.NO_EFFECT_DETAIL, code=undo.NO_EFFECT_CODE, status_code=409
    )


def _archived_in_force(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    if _writable(conn, actor, event)["archived_at"] is None:
        raise _no_longer_in_force()


def _unarchived_in_force(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    if _writable(conn, actor, event)["archived_at"] is not None:
        raise _no_longer_in_force()


def _attached(conn: sqlite3.Connection, issue_id: int, label_id: int) -> bool:
    return any(
        int(label["id"]) == label_id
        for label in labels.labels_for_issue(conn, issue_id)
    )


def _labeled_in_force(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    issue = _writable(conn, actor, event)
    if not _attached(conn, issue["id"], _label_id(conn, event)):
        raise _no_longer_in_force()


def _unlabeled_in_force(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    issue = _writable(conn, actor, event)
    if _attached(conn, issue["id"], _label_id(conn, event)):
        raise _no_longer_in_force()


def _status_in_force(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    fact = _status_fact_in_force(conn, event)
    issue = _writable(conn, actor, event)
    # Undoing a reopen closes the issue, and a close is the approval-gated act
    # (``update_issue`` calls ``approvals.require`` when closing). A gated actor
    # without an approved request would only raise an ask, so it is not offered.
    closing = not statuses.is_done(
        conn, issue["project_id"], issue["status"]
    ) and statuses.is_done(conn, issue["project_id"], fact["before_status"])
    if closing and not approvals.would_pass(
        conn,
        actor,
        action_kind=approvals.ACTION_ISSUE_CLOSE,
        target_kind="issue",
        target_id=issue["id"],
    ):
        raise undo.UndoRefused(
            "undoing this closes the issue, which needs an approved request first",
            code=undo.COMMAND_REFUSED_CODE,
            status_code=202,
        )


def _assignee_in_force(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    _assignee_fact_in_force(conn, event)
    _writable(conn, actor, event)


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
    fact = _status_fact_in_force(conn, event)
    try:
        issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=event["target_id"],
            status=fact["before_status"],
        )
    except issue_commands.IssueCommandError as exc:
        raise _refusal(exc) from exc


def _status_fact_in_force(conn: sqlite3.Connection, event: dict) -> dict:
    """The structured prior status of a ``changed_status`` event whose effect is
    still in force, or the refusal :func:`_restore_status` documents."""
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
    return fact


def _restore_assignee(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    """Put an issue back in the hands an ``assigned``/``unassigned`` event took it
    from.

    The prior assignee is read from ``issue_assignee_facts`` (migration 0068) —
    the event itself records only the NEW assignee's display name, which is not
    unique and not a prior value, so before 0068 there was nothing trustworthy to
    restore. A pre-0068 event has no fact and refuses; the prose detail is never
    interpreted.

    Like status (and unlike the toggles), an assignee is a scalar with no domain
    idempotency, so the engine's no-effect net cannot catch a stale undo: without
    the still-in-force gate, undoing an old assignment would overwrite a NEWER
    assignee and stamp the result as a reversal. No scope gate, though — statuses
    are per-project and remapped by a move; an assignee is not.

    Authorization, budget, and any approval gate belong to ``update_issue``, run
    as the undoing actor. Passing ``assignee_id=None`` is an explicit unassign
    (the command distinguishes None from "not provided").
    """
    fact = _assignee_fact_in_force(conn, event)
    try:
        issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=event["target_id"],
            assignee_id=fact["before_assignee_id"],
        )
    except issue_commands.IssueCommandError as exc:
        raise _refusal(exc) from exc


def _assignee_fact_in_force(conn: sqlite3.Connection, event: dict) -> dict:
    """The structured prior assignee of an ``assigned``/``unassigned`` event
    whose effect is still in force, or the refusal :func:`_restore_assignee`
    documents."""
    fact = issue_activity.assignee_fact_for_event(conn, event["id"])
    if fact is None:
        raise undo.UndoRefused(
            "no structured prior assignee was recorded for this event",
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
    if issue["assignee_id"] != fact["after_assignee_id"]:
        raise undo.UndoRefused(
            "the issue has since been reassigned; "
            "this event's assignment is no longer in force",
            code=undo.NO_EFFECT_CODE,
            status_code=409,
        )
    return fact


def _text_fact_in_force(conn: sqlite3.Connection, event: dict) -> dict:
    """The stored prior text of an ``issue_edited`` event whose effect is still
    in force, or the refusal :func:`_restore_text` documents."""
    fact = issue_activity.prior_text_for_event(conn, event["id"])
    if fact is None:
        raise undo.UndoRefused(
            "no prior title or body was recorded for this edit",
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
    replaced = [
        name
        for name in fact["changed"]
        if issue_activity.text_digest(issue[name]) != fact[f"after_{name}_sha256"]
    ]
    if replaced:
        raise undo.UndoRefused(
            f"the issue's {' and '.join(replaced)} has been edited since this "
            "event; its text is no longer in force, and undo will not overwrite "
            "the newer text",
            code=undo.NO_EFFECT_CODE,
            status_code=409,
        )
    return fact


def _text_in_force(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    """The precheck the issue page's offer asks (U9): the edit's text is still
    the issue's own and the viewer may write the issue."""
    _text_fact_in_force(conn, event)
    _writable(conn, actor, event)


def _restore_text(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    """Put back the title and/or body an ``issue_edited`` event replaced.

    The prior text is read from ``issue_prior_texts``, written by the edit's own
    recorder in its transaction (F10); the event's ``detail`` (the new title) is
    never used. An edit with no row (recorded before the table, or imported) is
    ``undo_not_reversible``.

    Text is a scalar with no domain idempotency, so the scalar discipline of
    status and assignee applies: each field the edit changed must still hold the
    text the edit left (its stored digest), or the undo refuses with
    ``undo_no_effect`` and writes nothing, instead of overwriting a later edit
    with older text. A later edit of the other field, or of any other field,
    does not block it. The restore runs through ``restore_issue_text`` as the
    undoing actor, which re-checks the digests under the write lock; that check
    is its precondition, so a bearer-token caller sends no If-Match to undo.

    The reversal records an ordinary ``issue_edited`` with its own prior text,
    so undoing the undo is a redo.
    """
    fact = _text_fact_in_force(conn, event)
    unset = issue_commands.UNSET
    try:
        issue_commands.restore_issue_text(
            conn,
            actor=actor,
            issue_id=event["target_id"],
            title=fact["prior_title"] if "title" in fact["changed"] else unset,
            body=fact["prior_body"] if "body" in fact["changed"] else unset,
            expect_title_sha256=fact["after_title_sha256"],
            expect_body_sha256=fact["after_body_sha256"],
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


def _issue_etag(conn: sqlite3.Connection, issue_id: int) -> str | None:
    issue = issues.get_issue(conn, issue_id)
    return None if issue is None else issue_etags.current_etag(conn, issue)


def register() -> None:
    """Wire Aegis's inverses into the undo engine. Idempotent."""
    undo.register_validator("issue", _issue_etag)
    undo.register(
        "archived",
        action_class=undo.TWO_WAY,
        compensator=_unarchive_issue,
        precheck=_archived_in_force,
    )
    undo.register(
        "unarchived",
        action_class=undo.TWO_WAY,
        compensator=_archive_issue,
        precheck=_unarchived_in_force,
    )
    undo.register(
        "labeled",
        action_class=undo.TWO_WAY,
        compensator=_detach_label,
        precheck=_labeled_in_force,
    )
    undo.register(
        "unlabeled",
        action_class=undo.TWO_WAY,
        compensator=_attach_label,
        precheck=_unlabeled_in_force,
    )
    undo.register(
        "changed_status",
        action_class=undo.TWO_WAY,
        compensator=_restore_status,
        precheck=_status_in_force,
    )
    # One compensator for both directions: the fact's before/after carries the
    # direction, so undoing an assign restores the previous holder (or clears it)
    # and undoing an unassign re-assigns them.
    undo.register(
        "assigned",
        action_class=undo.TWO_WAY,
        compensator=_restore_assignee,
        precheck=_assignee_in_force,
    )
    undo.register(
        "unassigned",
        action_class=undo.TWO_WAY,
        compensator=_restore_assignee,
        precheck=_assignee_in_force,
    )
    # The edit's recorder keeps the text it replaced (F10), so an edit is
    # reversible, and the reversal, an edit itself, is too.
    undo.register(
        "issue_edited",
        action_class=undo.TWO_WAY,
        compensator=_restore_text,
        precheck=_text_in_force,
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
    undo.register(
        "comment_edited",
        action_class=undo.ONE_WAY,
        reason="the previous wording is not stored as a fact; edit the comment again",
    )
    undo.register(
        "comment_deleted",
        action_class=undo.TRAPDOOR,
        reason="the comment body is gone; it cannot be restored from the trail",
    )
    undo.register(
        "changed_priority",
        action_class=undo.ONE_WAY,
        reason="priority changes are recorded as prose, with no fact table to restore",
    )
    undo.register(
        "lease_released",
        action_class=undo.ONE_WAY,
        reason="the holder's possession ended; it claims again for a new one",
    )
    undo.register(
        "holder_budget_exhausted",
        action_class=undo.ONE_WAY,
        reason="it records a refusal; nothing was applied, so nothing is undone",
    )
