"""Application commands for audited, atomic page writes.

Today this owns the page-EDIT write: the snapshot-then-overwrite, its derived
link/search re-index, the ``page_edited`` audit event (with the editor's auto-watch
and any mentions), and an optional ``If-Match`` precondition, all folded into one
``db.transaction(immediate=True)``. It is the Mentor twin of the edit path in
aegis/issue_commands.update_issue, and the foundation for page optimistic
concurrency — ETag / If-Match parity with issues.

Previously an edit committed the row change (mentor/pages.update_page) and its
activity event in SEPARATE transactions: a crash between them could rewrite a page's
body with no ``page_edited`` event on the trail. And there was no conditional-write
path at all, so two agents editing shared memory silently clobbered each other
(last-write-wins). This command closes both gaps.

Visibility/existence authorization (``access.can_see_space``) stays at the transport
boundary, as it does for the page-comment commands — the command re-reads the page
under the write lock so the precondition check and the mutation cannot straddle a
concurrent edit.
"""

from __future__ import annotations

import sqlite3

from athena.core import db, etag, labels
from athena.mentor import page_activity, page_etags, pages

_ERROR_KINDS = (
    "not_found",
    "invalid",
    "invalid_precondition",
    "precondition_too_large",
    "precondition_failed",
)


class PageCommandError(Exception):
    """A transport-neutral page-command rejection.

    ``kind`` lets each adapter map to a status (REST: 404/422/400/431/412);
    ``current_etag`` accompanies a ``precondition_failed`` so the boundary can echo
    the live validator in the 412's ``ETag`` header, exactly as issues do.
    """

    def __init__(
        self, kind: str, detail: str, *, current_etag: str | None = None
    ) -> None:
        super().__init__(detail)
        if kind not in _ERROR_KINDS:
            raise ValueError(f"unknown PageCommandError kind: {kind}")
        self.kind = kind
        self.detail = detail
        self.current_etag = current_etag


def edit_page(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    page_id: int,
    title: str | None = None,
    body: str | None = None,
    if_match: list[str] | None = None,
) -> dict:
    """Edit a page's title and/or body atomically with its ``page_edited`` event,
    under an optional ``If-Match`` precondition evaluated inside the write lock.

    ``title``/``body`` follow the data layer's partial-update rule: a field left None
    is untouched (the boundary strips/validates the title and guarantees at least one
    field). Returns the updated page. Raises ``PageCommandError('not_found')`` if the
    page vanished before the write (a race past the boundary's visibility gate), and
    the precondition kinds when an ``If-Match`` is supplied and malformed, too large,
    or stale.

    The current representation and the strong-comparison check both run inside this
    ``BEGIN IMMEDIATE`` transaction, so two writers holding the same tag cannot both
    pass the precondition and mutate — the point of an optimistic lock.
    """
    with db.transaction(conn, immediate=True):
        before = pages.get_page(conn, page_id)
        if before is None:
            raise PageCommandError("not_found", "no such page")

        if if_match is not None:
            current = page_etags.current_etag(conn, before)
            try:
                condition = etag.parse_if_match(if_match)
            except etag.IfMatchTooLarge as exc:
                raise PageCommandError("precondition_too_large", str(exc)) from exc
            except etag.InvalidIfMatch as exc:
                raise PageCommandError("invalid_precondition", str(exc)) from exc
            if not condition.matches(current):
                raise PageCommandError(
                    "precondition_failed",
                    "If-Match precondition failed",
                    current_etag=current,
                )

        after = pages.update_page(
            conn,
            page_id,
            editor_id=actor_id,
            title=title,
            body=body,
            commit=False,
        )
        # after is never None here: the page existed under the same write lock.
        assert after is not None
        page_activity.record_page_edited(
            conn, actor_id=actor_id, before=before, after=after, commit=False
        )
        return after


def create_page(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    title: str,
    body: str = "",
    parent_id: int | None = None,
) -> dict:
    """Create a page atomically with its ``page_created`` event (the creator's
    auto-watch and any mentions included). Transport-shape validation — a non-empty
    title, and a parent that is a real page in the SAME space — is the boundary's job
    (as with the page edit/comment commands); the foreign keys are the backstop.
    Returns the new page."""
    with db.transaction(conn, immediate=True):
        page = pages.create_page(
            conn,
            space_id=space_id,
            title=title,
            body=body,
            parent_id=parent_id,
            created_by=actor_id,
            commit=False,
        )
        page_activity.record_page_created(
            conn,
            actor_id=actor_id,
            page_id=page["id"],
            title=page["title"],
            body=page["body"],
            commit=False,
        )
        return page


def move_page(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, new_parent_id: int | None
) -> dict:
    """Re-parent a page atomically with its ``page_moved`` event, under the write lock
    so validate-then-write can't race a concurrent move into a cycle. Raises
    ``PageCommandError('not_found')`` if the page vanished, or ``('invalid', reason)``
    if the move is illegal (another space, itself, or its own descendant). A move to
    the same parent is a no-op that records nothing. Returns the moved page."""
    with db.transaction(conn, immediate=True):
        page = pages.get_page(conn, page_id)
        if page is None:
            raise PageCommandError("not_found", "no such page")
        moved, reason = pages.move(conn, page, new_parent_id, commit=False)
        if reason is not None:
            raise PageCommandError("invalid", reason)
        assert moved is not None
        page_activity.record_page_moved(
            conn,
            actor_id=actor_id,
            page_id=page_id,
            before_parent_id=page["parent_id"],
            after_parent_id=new_parent_id,
            commit=False,
        )
        return moved


def restore_page_version(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, version: int
) -> dict:
    """Restore a page's content to a prior revision atomically with its
    ``page_restored`` event. This is a non-destructive edit: the live content is first
    snapshotted into history (via update_page), so nothing is lost. Raises
    ``PageCommandError('not_found')`` if the page or that version is missing. Restoring
    content identical to the live row is a no-op that records nothing. Returns the
    page."""
    with db.transaction(conn, immediate=True):
        before = pages.get_page(conn, page_id)
        if before is None:
            raise PageCommandError("not_found", "no such page")
        restored = pages.restore_version(
            conn, page_id, version, editor_id=actor_id, commit=False
        )
        if restored is None:
            raise PageCommandError("not_found", "no such page or version")
        page_activity.record_page_restored(
            conn,
            actor_id=actor_id,
            page_id=page_id,
            version=version,
            before=before,
            after=restored,
            commit=False,
        )
        return restored


def delete_page(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, title: str
) -> bool:
    """Hard-delete a page atomically with its ``page_deleted`` event, then run the
    post-commit blob-unlink + index side effects. Returns True if a page was removed,
    False if it had already vanished (a race past the boundary's checks — no event for a
    delete that didn't happen). The caller enforces the no-children precondition (409),
    visibility, and passes the page's ``title`` (the row is gone by the time the event
    is read, so the title is preserved in the audit detail).

    The DB deletes and the audit event commit or roll back together (the atomicity the
    migration buys); the filesystem/index side effects — which can't be transactional —
    run only after that commit, so a rolled-back delete leaves nothing orphaned."""
    stored_names: list[str] = []
    with db.transaction(conn, immediate=True):
        if pages.get_page(conn, page_id) is None:
            return False
        stored_names = pages.purge_page(conn, page_id)
        page_activity.record_page_deleted(
            conn, actor_id=actor_id, page_id=page_id, title=title, commit=False
        )
    pages.finalize_page_deletion(conn, page_id, stored_names)
    return True


def attach_page_label(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, label_id: int
) -> dict:
    """Attach a label to a page atomically with its ``page_labeled`` event — the
    Mentor twin of aegis attach_label. Previously the join write and the activity
    recorder committed SEPARATELY (labels.add_label_to_page committed, then the
    event committed on its own), so a crash between them could label a page with no
    ``page_labeled`` event on the trail. Idempotent: re-attaching an attached pair
    records nothing. Visibility/existence authorization stays at the transport
    boundary (like the other page commands); the page is re-read under the write
    lock as the race guard. Raises ``PageCommandError('not_found')`` if the page
    vanished and ``('invalid', 'no such label')`` for an unknown label id (checked
    here so the FK cannot surface as a 500). Returns the page."""
    with db.transaction(conn, immediate=True):
        page = pages.get_page(conn, page_id)
        if page is None:
            raise PageCommandError("not_found", "no such page")
        if labels.get_label(conn, label_id) is None:
            raise PageCommandError("invalid", "no such label")
        if labels.add_label_to_page(conn, page_id, label_id, commit=False):
            page_activity.record_page_label_added(
                conn,
                actor_id=actor_id,
                page_id=page_id,
                label_id=label_id,
                commit=False,
            )
        return page


def detach_page_label(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, label_id: int
) -> dict:
    """Detach a label from a page atomically with its ``page_unlabeled`` event —
    the Mentor twin of aegis detach_label, closing the same split-commit gap as
    attach_page_label. Raises ``PageCommandError('not_found')`` with "no such page"
    if the page vanished, or with "label not on this page" when the pair isn't
    attached — the boundary maps both to its 404 (REST) or treats the unattached
    pair as a double-submit no-op (browser). Returns the page."""
    with db.transaction(conn, immediate=True):
        page = pages.get_page(conn, page_id)
        if page is None:
            raise PageCommandError("not_found", "no such page")
        if not labels.remove_label_from_page(conn, page_id, label_id, commit=False):
            raise PageCommandError("not_found", "label not on this page")
        page_activity.record_page_label_removed(
            conn,
            actor_id=actor_id,
            page_id=page_id,
            label_id=label_id,
            commit=False,
        )
        return page


def set_page_archived(
    conn: sqlite3.Connection, *, actor_id: int, page_id: int, archived: bool
) -> dict:
    """Archive (soft-delete) or restore a page atomically with its
    ``page_archived``/``page_unarchived`` event. The row — and its versions and
    comments — is preserved; only archived_at flips, so the default tree/nav/search
    hide it while it stays fully restorable. Idempotent: re-archiving an archived
    page (or restoring an active one) re-stamps but records no event.

    Visibility/existence authorization is the transport boundary's job (like the page
    edit and page-comment commands). Raises ``PageCommandError('not_found')`` if the
    page vanished before the write (a race past the boundary's gate)."""
    with db.transaction(conn, immediate=True):
        before = pages.get_page(conn, page_id)
        if before is None:
            raise PageCommandError("not_found", "no such page")
        after = pages.set_archived(conn, page_id, archived, commit=False)
        assert after is not None
        page_activity.record_page_archive_change(
            conn,
            actor_id=actor_id,
            page_id=page_id,
            before=before["archived_at"],
            after=after["archived_at"],
            commit=False,
        )
        return after
