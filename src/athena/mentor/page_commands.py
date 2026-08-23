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

These commands OWN their visibility authorization: every entry point takes a
resolved ``actor`` and re-reads the page (or space) inside its own write
transaction, refusing with ``not_found`` when it is missing or hidden — a page in
a private space reads as missing, never as forbidden, so a write path cannot
confirm one exists. The checks used to live at the transport boundary
(``access.can_see_space`` in the routes) and the command trusted a bare actor id,
which meant a caller reaching the command directly — undo, a workflow, a script —
was trusted too, and the check and the write it guarded could straddle a
transaction boundary. Role and token scope stay at the transport (and at undo's
boundary), exactly as they do for the aegis comment commands.
"""

from __future__ import annotations

import sqlite3

from athena.core import access, budgets, db, etag, labels
from athena.mentor import page_activity, page_etags, page_templates, pages, spaces

_ERROR_KINDS = (
    "not_found",
    "invalid",
    "conflict",
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


def _visible_page_or_refuse(
    conn: sqlite3.Connection, actor: dict, page_id: int
) -> dict:
    """The page, if this actor may see it — else ``not_found``.

    Called INSIDE the command's write transaction, which is the point of moving it
    here: a check the transport ran beforehand can go stale between the check and
    the write, and a caller reaching the command directly skipped it entirely. A
    page in a private space reads as missing so its existence never leaks through
    a write path."""
    page = pages.get_page(conn, page_id)
    if page is None or not access.can_see_space(conn, actor, page["space_id"]):
        raise PageCommandError("not_found", "no such page")
    return page


def _visible_space_or_refuse(
    conn: sqlite3.Connection, actor: dict, space_id: int
) -> dict:
    """The space, if this actor may see it — else ``not_found`` (same leak rule)."""
    space = spaces.get_space(conn, space_id)
    if space is None or not access.can_see_space(conn, actor, space_id):
        raise PageCommandError("not_found", "no such space")
    return space


def edit_page(
    conn: sqlite3.Connection,
    *,
    actor: dict,
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
    page is missing or hidden from ``actor``, and the precondition kinds when an
    ``If-Match`` is supplied and malformed, too large, or stale.

    The current representation and the strong-comparison check both run inside this
    ``BEGIN IMMEDIATE`` transaction, so two writers holding the same tag cannot both
    pass the precondition and mutate — the point of an optimistic lock.

    Metered: a budgeted actor spends one action here (see ``core.budgets``).
    """
    with db.transaction(conn, immediate=True):
        before = _visible_page_or_refuse(conn, actor, page_id)
        budgets.charge(conn, actor)
        actor_id = actor["id"]

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
    actor: dict,
    space_id: int,
    title: str,
    body: str = "",
    parent_id: int | None = None,
) -> dict:
    """Create a page atomically with its ``page_created`` event (the creator's
    auto-watch and any mentions included). The space must exist and be visible to
    ``actor``, checked inside this transaction. Transport-shape validation — a
    non-empty title, and a parent that is a real page in the SAME space — is the
    boundary's job (as with the page edit/comment commands); the foreign keys are
    the backstop. Returns the new page.

    Metered: a budgeted actor spends one action here (see ``core.budgets``)."""
    with db.transaction(conn, immediate=True):
        _visible_space_or_refuse(conn, actor, space_id)
        budgets.charge(conn, actor)
        actor_id = actor["id"]
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
    conn: sqlite3.Connection, *, actor: dict, page_id: int, new_parent_id: int | None
) -> dict:
    """Re-parent a page atomically with its ``page_moved`` event, under the write lock
    so validate-then-write can't race a concurrent move into a cycle. Raises
    ``PageCommandError('not_found')`` if the page is missing or hidden from
    ``actor``, or ``('invalid', reason)`` if the move is illegal (another space,
    itself, or its own descendant). A move to the same parent is a no-op that
    records nothing. Returns the moved page."""
    with db.transaction(conn, immediate=True):
        page = _visible_page_or_refuse(conn, actor, page_id)
        actor_id = actor["id"]
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
    conn: sqlite3.Connection, *, actor: dict, page_id: int, version: int
) -> dict:
    """Restore a page's content to a prior revision atomically with its
    ``page_restored`` event. This is a non-destructive edit: the live content is first
    snapshotted into history (via update_page), so nothing is lost. Raises
    ``PageCommandError('not_found')`` if the page is hidden from ``actor`` or the
    page or that version is missing. Restoring content identical to the live row is
    a no-op that records nothing. Returns the page."""
    with db.transaction(conn, immediate=True):
        before = _visible_page_or_refuse(conn, actor, page_id)
        actor_id = actor["id"]
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


def delete_page(conn: sqlite3.Connection, *, actor: dict, page_id: int) -> dict:
    """Hard-delete a page atomically with its ``page_deleted`` event, then run the
    post-commit blob-unlink + index side effects. Raises
    ``PageCommandError('not_found')`` if the page is missing or hidden from
    ``actor``, and ``('conflict')`` if it still has child pages — we refuse rather
    than cascade, so a delete can't silently wipe a subtree. Both checks run inside
    the write transaction, so neither can straddle a concurrent write (a child
    created between a boundary-side check and the purge used to be orphanable).
    Returns the page as it was, since the row is gone — the caller's redirect or
    audit trail may still need its space and title.

    The DB deletes and the audit event commit or roll back together (the atomicity the
    migration buys); the filesystem/index side effects — which can't be transactional —
    run only after that commit, so a rolled-back delete leaves nothing orphaned.

    The event is recorded BEFORE the purge, inside the same transaction. Order used to
    be the other way, which meant a deletion notified NOBODY: purge_page drops the
    page's watches, and the fan-out resolves a space watch through the page row, so by
    the time the event existed both routes to a watcher were gone. Losing the loudest
    change a subscriber can care about is not an acceptable reading of "the watch dies
    with the page" — the watch ends, but its last delivery is the news that it ended.
    The ordering is invisible outside the transaction: readers still see the row gone
    and the event present — the title in the audit detail comes from the row as read
    under this same write lock."""
    stored_names: list[str] = []
    with db.transaction(conn, immediate=True):
        page = _visible_page_or_refuse(conn, actor, page_id)
        if pages.count_child_pages(conn, page_id) > 0:
            raise PageCommandError("conflict", "move or delete its child pages first")
        page_activity.record_page_deleted(
            conn,
            actor_id=actor["id"],
            page_id=page_id,
            title=page["title"],
            commit=False,
        )
        stored_names = pages.purge_page(conn, page_id)
    pages.finalize_page_deletion(conn, page_id, stored_names)
    return page


def attach_page_label(
    conn: sqlite3.Connection, *, actor: dict, page_id: int, label_id: int
) -> dict:
    """Attach a label to a page atomically with its ``page_labeled`` event — the
    Mentor twin of aegis attach_label. Previously the join write and the activity
    recorder committed SEPARATELY (labels.add_label_to_page committed, then the
    event committed on its own), so a crash between them could label a page with no
    ``page_labeled`` event on the trail. Idempotent: re-attaching an attached pair
    records nothing. The page must be visible to ``actor``, checked inside this
    transaction. Raises ``PageCommandError('not_found')`` if the page is missing or
    hidden and ``('invalid', 'no such label')`` for an unknown label id (checked
    here so the FK cannot surface as a 500). Returns the page."""
    with db.transaction(conn, immediate=True):
        page = _visible_page_or_refuse(conn, actor, page_id)
        if labels.get_label(conn, label_id) is None:
            raise PageCommandError("invalid", "no such label")
        if labels.add_label_to_page(conn, page_id, label_id, commit=False):
            page_activity.record_page_label_added(
                conn,
                actor_id=actor["id"],
                page_id=page_id,
                label_id=label_id,
                commit=False,
            )
        return page


def attach_page_label_by_name(
    conn: sqlite3.Connection, *, actor: dict, page_id: int, name: str
) -> dict:
    """Attach a label to a page by NAME, find-or-creating the vocabulary entry —
    the Mentor twin of aegis issue_commands.attach_label_by_name.

    The browser route lets a user type a label name without a separate
    vocabulary-management step. The find-or-create lives HERE, not in the
    transport: one command owns each write, and the vocabulary write now shares
    the attach's transaction, so a crash between them cannot plant a label that
    was never attached. The visibility check precedes the find-or-create INSIDE
    the same transaction, so a refused attach — a hidden or nonexistent page —
    never grows the vocabulary either: the refusal rolls the whole write back.
    Raises ``PageCommandError('not_found')`` if the page is missing or hidden
    from ``actor``. Returns the page."""
    with db.transaction(conn, immediate=True):
        page = _visible_page_or_refuse(conn, actor, page_id)
        label = labels.get_or_create_label(conn, name=name, commit=False)
        if labels.add_label_to_page(conn, page_id, label["id"], commit=False):
            page_activity.record_page_label_added(
                conn,
                actor_id=actor["id"],
                page_id=page_id,
                label_id=label["id"],
                commit=False,
            )
        return page


def detach_page_label(
    conn: sqlite3.Connection, *, actor: dict, page_id: int, label_id: int
) -> dict:
    """Detach a label from a page atomically with its ``page_unlabeled`` event —
    the Mentor twin of aegis detach_label, closing the same split-commit gap as
    attach_page_label. Raises ``PageCommandError('not_found')`` with "no such page"
    if the page is missing or hidden from ``actor``, or with "label not on this
    page" when the pair isn't attached — the boundary maps both to its 404 (REST)
    or treats the unattached pair as a double-submit no-op (browser). Returns the
    page."""
    with db.transaction(conn, immediate=True):
        page = _visible_page_or_refuse(conn, actor, page_id)
        if not labels.remove_label_from_page(conn, page_id, label_id, commit=False):
            raise PageCommandError("not_found", "label not on this page")
        page_activity.record_page_label_removed(
            conn,
            actor_id=actor["id"],
            page_id=page_id,
            label_id=label_id,
            commit=False,
        )
        return page


def set_page_archived(
    conn: sqlite3.Connection, *, actor: dict, page_id: int, archived: bool
) -> dict:
    """Archive (soft-delete) or restore a page atomically with its
    ``page_archived``/``page_unarchived`` event. The row — and its versions and
    comments — is preserved; only archived_at flips, so the default tree/nav/search
    hide it while it stays fully restorable. Idempotent: re-archiving an archived
    page (or restoring an active one) re-stamps but records no event.

    The page must be visible to ``actor``, checked inside this transaction. Raises
    ``PageCommandError('not_found')`` if it is missing or hidden."""
    with db.transaction(conn, immediate=True):
        before = _visible_page_or_refuse(conn, actor, page_id)
        after = pages.set_archived(conn, page_id, archived, commit=False)
        assert after is not None
        page_activity.record_page_archive_change(
            conn,
            actor_id=actor["id"],
            page_id=page_id,
            before=before["archived_at"],
            after=after["archived_at"],
            commit=False,
        )
        return after


def create_page_from_template(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    space_id: int,
    template_id: int,
    title: str,
    parent_id: int | None = None,
) -> dict:
    """Create a page whose body starts as a template's, atomically with its
    ``page_created`` event. The space must be visible to ``actor``, checked inside
    this transaction like the template itself.

    The template is re-read and re-checked INSIDE the write lock, not at the
    boundary: a page can lose its ``template`` label between the moment the
    operator sees the menu and the moment they submit, and a stale menu must not
    be able to seed a page from something that is no longer a template.

    The new page deliberately does NOT inherit the template's labels. Copying
    them would carry the ``template`` label across and make every page created
    from a template a template itself — a self-replicating menu. The body is
    copied; the marking is not.

    Raises ``PageCommandError('not_found')`` if the template is gone or lives in
    another space, and ``('invalid')`` if it is not actually a template.

    Metered: a budgeted actor spends one action here (see ``core.budgets``).
    """
    with db.transaction(conn, immediate=True):
        _visible_space_or_refuse(conn, actor, space_id)
        budgets.charge(conn, actor)
        actor_id = actor["id"]
        template = pages.get_page(conn, template_id)
        if (
            template is None
            or template["space_id"] != space_id
            or template["archived_at"] is not None
        ):
            raise PageCommandError("not_found", "no such template in this space")
        if not page_templates.is_template(conn, template_id):
            raise PageCommandError("invalid", "that page is not a template")
        body = page_templates.fill(
            template["body"], title=title, date=page_templates.today()
        )
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


def ensure_daily_page(
    conn: sqlite3.Connection, *, actor: dict, space_id: int, day: str | None = None
) -> tuple[dict, bool]:
    """Find-or-create the space's daily note for ``day`` (default: today, UTC).

    Returns ``(page, created)``. The space must be visible to ``actor``, checked
    inside this transaction — on the find path too, since finding a daily note
    confirms a hidden space has one. This is the one command in Mentor that may
    perform NO write at all: visiting an existing daily note is a read, so the
    already-exists path charges no budget and records no event. Stamping a
    ``page_created`` event on every visit would turn the operator's morning habit
    into audit noise and make the trail lie about when the page came to be.

    Idempotency is structural, not best-effort: the lookup and the insert share
    one ``BEGIN IMMEDIATE`` transaction, so two concurrent first-visits serialize
    and the second finds the first's page instead of racing it. A daily note is
    exactly the surface a double-click or a prefetching browser hits twice.

    The body comes from the space's ``Daily Note Template`` if it has one. A
    space without one still gets a note — empty — because the feature must not
    require setup the operator has not done yet.
    """
    with db.transaction(conn, immediate=True):
        _visible_space_or_refuse(conn, actor, space_id)
        title = day or page_templates.today()
        existing = pages.find_pages_by_title(conn, title, space_id=space_id)
        if existing:
            return existing[0], False
        budgets.charge(conn, actor)
        actor_id = actor["id"]
        template = page_templates.daily_template(conn, space_id)
        body = (
            page_templates.fill(template["body"], title=title, date=title)
            if template is not None
            else ""
        )
        page = pages.create_page(
            conn,
            space_id=space_id,
            title=title,
            body=body,
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
        return page, True
