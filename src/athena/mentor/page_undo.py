"""Mentor's inverses — how page events are undone (`core/undo.py`).

The Aegis twin of this module delegates authorization entirely to the command
owner, because `aegis.issue_commands` owns its own role/scope/visibility gates.
Mentor's page commands now own their VISIBILITY gate the same way — each re-reads
the page inside its own write transaction and refuses a hidden page as missing —
so the compensators here delegate that check rather than duplicating it, and a
hidden page gives the same refusal as a missing one (an event id cannot probe a
private space).

What the page commands deliberately do NOT check is role and token scope: those
stay at each boundary, and undo is a second boundary onto the same commands, so
it applies them itself. Without that, undo would be a privilege escalation — a
viewer, or a write-role actor holding only an issue-scoped token, could archive
or re-label pages by naming event ids.
"""

from __future__ import annotations

import sqlite3

from athena.core import identity, labels, tokens, undo
from athena.mentor import api as page_api
from athena.mentor import page_commands


def _refusal(exc: page_commands.PageCommandError) -> undo.UndoRefused:
    """Re-raise a page command's own rejection as an undo refusal, reusing the REST
    boundary's status mapping so the two answers cannot drift."""
    return undo.UndoRefused(
        exc.detail,
        code=undo.COMMAND_REFUSED_CODE,
        status_code=page_api.page_command_status(exc),
    )


def _page_writer(actor: dict | None) -> dict:
    """The boundary's role/scope gate, applied at undo time.

    Visibility is NOT checked here — the command owns it, inside the write
    transaction (see the module docstring)."""
    if actor is None:
        raise undo.UndoRefused(
            "authentication required", code=undo.NOT_REVERSIBLE_CODE, status_code=401
        )
    identity.require_write_role(actor)
    identity.require_token_scope(actor, tokens.DOCS_WRITE_SCOPE)
    return actor


def _label_id(conn: sqlite3.Connection, event: dict) -> int:
    """The label a page label event names — an exact lookup by unique name, the
    same rule (and the same refusal for a since-deleted label) as Aegis uses."""
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
    writer = _page_writer(actor)
    try:
        page_commands.set_page_archived(
            conn, actor=writer, page_id=event["target_id"], archived=archived
        )
    except page_commands.PageCommandError as exc:
        raise _refusal(exc) from exc


def _unarchive_page(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    _set_archived(conn, actor, event, archived=False)


def _archive_page(conn: sqlite3.Connection, actor: dict | None, event: dict) -> None:
    _set_archived(conn, actor, event, archived=True)


def _detach_page_label(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    writer = _page_writer(actor)
    try:
        page_commands.detach_page_label(
            conn,
            actor=writer,
            page_id=event["target_id"],
            label_id=_label_id(conn, event),
        )
    except page_commands.PageCommandError as exc:
        raise _refusal(exc) from exc


def _attach_page_label(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    writer = _page_writer(actor)
    try:
        page_commands.attach_page_label(
            conn,
            actor=writer,
            page_id=event["target_id"],
            label_id=_label_id(conn, event),
        )
    except page_commands.PageCommandError as exc:
        raise _refusal(exc) from exc


def register() -> None:
    """Wire Mentor's inverses into the undo engine. Idempotent."""
    undo.register(
        "page_archived", action_class=undo.TWO_WAY, compensator=_unarchive_page
    )
    undo.register(
        "page_unarchived", action_class=undo.TWO_WAY, compensator=_archive_page
    )
    undo.register(
        "page_labeled", action_class=undo.TWO_WAY, compensator=_detach_page_label
    )
    undo.register(
        "page_unlabeled", action_class=undo.TWO_WAY, compensator=_attach_page_label
    )

    # Classified, deliberately not reversible.
    undo.register(
        "page_commented",
        action_class=undo.ONE_WAY,
        reason="people have read it; delete the comment explicitly instead",
    )
    undo.register(
        "page_deleted",
        action_class=undo.TRAPDOOR,
        reason="the page, its versions, and its comments are destroyed",
    )
    undo.register(
        "space_deleted",
        action_class=undo.TRAPDOOR,
        reason="the space row and its access envelope are destroyed",
    )
    undo.register(
        "page_edited",
        action_class=undo.ONE_WAY,
        reason="restore the prior version explicitly — page history already keeps it",
    )
