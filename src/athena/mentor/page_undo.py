"""Mentor's inverses — how page events are undone (`core/undo.py`).

The Aegis twin of this module delegates authorization entirely to the command
owner, because `aegis.issue_commands` owns its own role/scope/visibility gates.
**Mentor's page commands deliberately do not**: `page_commands.set_page_archived`
and the label commands take a bare ``actor_id`` and document that the transport
boundary owns visibility and scope (`mentor/api.py` calls `_page_for_read` and
depends on `docs_write_actor`).

Undo is a second boundary onto those same commands, so it must apply the same two
checks itself. Without them, undo would be a privilege escalation: any writer
could archive or re-label a page in a private space they cannot see, by naming its
event id. That asymmetry is the reason this module reads the way it does.
"""

from __future__ import annotations

import sqlite3

from athena.core import access, identity, labels, tokens, undo
from athena.mentor import api as page_api
from athena.mentor import page_commands, pages


def _refusal(exc: page_commands.PageCommandError) -> undo.UndoRefused:
    """Re-raise a page command's own rejection as an undo refusal, reusing the REST
    boundary's status mapping so the two answers cannot drift."""
    return undo.UndoRefused(
        exc.detail,
        code=undo.COMMAND_REFUSED_CODE,
        status_code=page_api.page_command_status(exc),
    )


def _writable_page(conn: sqlite3.Connection, actor: dict | None, page_id: int) -> dict:
    """The transport boundary's gate, re-applied at undo time.

    Role and scope first, then visibility — and a hidden page gives the SAME
    refusal as a missing one, so an event id cannot be used to probe for pages in
    a private space."""
    if actor is None:
        raise undo.UndoRefused(
            "authentication required", code=undo.NOT_REVERSIBLE_CODE, status_code=401
        )
    identity.require_write_role(actor)
    identity.require_token_scope(actor, tokens.DOCS_WRITE_SCOPE)
    page = pages.get_page(conn, page_id)
    if page is None or not access.can_see_space(conn, actor, page["space_id"]):
        raise undo.UndoRefused(
            "no such page", code=undo.NOT_FOUND_CODE, status_code=404
        )
    return page


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
    page = _writable_page(conn, actor, event["target_id"])
    assert actor is not None
    try:
        page_commands.set_page_archived(
            conn, actor_id=actor["id"], page_id=page["id"], archived=archived
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
    page = _writable_page(conn, actor, event["target_id"])
    assert actor is not None
    try:
        page_commands.detach_page_label(
            conn,
            actor_id=actor["id"],
            page_id=page["id"],
            label_id=_label_id(conn, event),
        )
    except page_commands.PageCommandError as exc:
        raise _refusal(exc) from exc


def _attach_page_label(
    conn: sqlite3.Connection, actor: dict | None, event: dict
) -> None:
    page = _writable_page(conn, actor, event["target_id"])
    assert actor is not None
    try:
        page_commands.attach_page_label(
            conn,
            actor_id=actor["id"],
            page_id=page["id"],
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
