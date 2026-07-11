"""Data access for pages — a single document inside a space.

All page SQL lives here, mirroring mentor/spaces.py and aegis/issues.py. A page
belongs to one space and may nest under a parent page (parent_id) to form a tree.
This covers create + read + edit; each edit snapshots the prior content into
page_versions (the page's history), so the live `pages` row is always the present
and page_versions is the past. The cross-space tree rule (a parent must share the
page's space) is enforced at the API boundary, not here.
"""
from __future__ import annotations

import sqlite3

from athena import config
from athena.core import attachments, links, notifications, search


def create_page(
    conn: sqlite3.Connection,
    *,
    space_id: int,
    title: str,
    created_by: int,
    body: str = "",
    parent_id: int | None = None,
) -> dict:
    """Insert a page and return it. The foreign keys refuse an orphan: space_id
    must be a real space, parent_id (when given) a real page, created_by a real
    user. A new page is its own current revision, so updated_by/updated_at start
    equal to the creator and creation time — this is what the first edit will
    snapshot as version 1."""
    cur = conn.execute(
        "INSERT INTO pages (id, space_id, parent_id, title, body, created_by, "
        "updated_by, updated_at) "
        "SELECT next_id, ?, ?, ?, ?, ?, ?, datetime('now') "
        "FROM activity_target_id_sequences WHERE target_kind = 'page'",
        (space_id, parent_id, title, body, created_by, created_by),
    )
    conn.commit()
    # Index any [[issue:N]]/[[page:N]] references this page's body makes.
    links.sync_links(conn, source_kind="page", source_id=cur.lastrowid, body=body)
    # Index its title + body for full-text search.
    search.index_document(conn, kind="page", source_id=cur.lastrowid)
    return get_page(conn, cur.lastrowid)


def get_page(conn: sqlite3.Connection, page_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    return dict(row) if row else None


def list_pages_in_space(conn: sqlite3.Connection, space_id: int) -> list[dict]:
    """Every page in a space, alphabetical by title. Each row carries its
    parent_id so a caller can assemble the tree; this returns the flat set."""
    rows = conn.execute(
        "SELECT * FROM pages WHERE space_id = ? ORDER BY title COLLATE NOCASE",
        (space_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def update_page(
    conn: sqlite3.Connection,
    page_id: int,
    *,
    editor_id: int,
    title: str | None = None,
    body: str | None = None,
) -> dict | None:
    """Edit a page's title and/or body, snapshotting the prior content into its
    history first. Partial: only the fields passed as non-None change. Returns the
    updated page, or None if no page has that id (so the boundary can 404). With no
    changing fields the page is returned untouched and no version is cut.

    The snapshot-then-overwrite happens in one transaction so history can never
    diverge from the live page. We open it with BEGIN IMMEDIATE so the page is
    re-read, the next version is computed, and both writes land while this
    connection holds SQLite's write lock — without it two concurrent editors can
    both read COUNT()+1 == the same number and collide on UNIQUE(page_id, version).
    The version number is dense per page — one more than the count already stored —
    so versions read 1, 2, 3… The snapshot carries the SUPERSEDED revision's own
    author and time (the page's current updated_by/updated_at), not the editor
    doing the replacing; the new editor stamps the fresh live row. Column names in
    the SET clause are hardcoded literals, never caller input, so the f-string is
    safe; values stay parameterized."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        page = get_page(conn, page_id)
        if page is None:
            conn.rollback()
            return None

        fields = {
            col: val
            for col, val in (("title", title), ("body", body))
            if val is not None
        }
        if not fields:
            conn.rollback()  # nothing to change — no new revision, release the lock
            return page

        next_version = conn.execute(
            "SELECT COUNT(*) AS n FROM page_versions WHERE page_id = ?", (page_id,)
        ).fetchone()["n"] + 1
        conn.execute(
            "INSERT INTO page_versions "
            "(page_id, version, title, body, edited_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                page_id,
                next_version,
                page["title"],
                page["body"],
                page["updated_by"],
                page["updated_at"],
            ),
        )

        assignments = ", ".join(f"{col} = ?" for col in fields)
        conn.execute(
            f"UPDATE pages SET {assignments}, updated_by = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (*fields.values(), editor_id, page_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # Re-index references only when the body actually changed (the only field
    # that carries [[...]] tokens); a title-only edit leaves them be. Runs after
    # the snapshot transaction commits, so links never reflect a rolled-back edit.
    if "body" in fields:
        links.sync_links(conn, source_kind="page", source_id=page_id, body=fields["body"])
    # Re-index for search whenever an indexed field (title or body) changed.
    if "title" in fields or "body" in fields:
        search.index_document(conn, kind="page", source_id=page_id)
    return get_page(conn, page_id)


def count_pages_in_space(conn: sqlite3.Connection, space_id: int) -> int:
    """How many pages live in this space (at any depth). The space-delete path uses
    it to refuse deleting a space that still holds pages (the caller would orphan a
    whole tree); the web UI uses it to disable the Delete button with an explanation.
    Lives here, not in spaces.py, because pages.py owns the space_id column — the
    same split as issues.count_issues_in_project owning issues.project_id."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM pages WHERE space_id = ?", (space_id,)
    ).fetchone()["n"]


def count_child_pages(conn: sqlite3.Connection, page_id: int) -> int:
    """How many pages nest directly under this one. The delete path uses it to
    refuse deleting a page that still has children (the caller would orphan them);
    the web UI uses it to disable the Delete button with an explanation. Counts
    only DIRECT children — that's all the block rule needs, since each child is
    itself undeletable until ITS children are gone."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM pages WHERE parent_id = ?", (page_id,)
    ).fetchone()["n"]


def delete_page(conn: sqlite3.Connection, page_id: int) -> bool:
    """Delete a page and everything derived from it. Returns True if a page was
    deleted, False if no page had that id (so the boundary can 404).

    PRECONDITION: the page has no child pages. The caller checks count_child_pages
    first and returns a clean 409 — we do NOT cascade, because silently deleting a
    subtree is the kind of irreversible bulk loss a wiki must not do by accident.
    (If a stray child somehow remained, the parent_id foreign key would refuse the
    delete and raise, rather than orphan it.)

    page_versions has a foreign key to this page with no ON DELETE, so its history
    rows would block the delete — we clear them first, in the same transaction as
    the page delete so the two can't half-happen. Then, exactly like create/update,
    we maintain the two DERIVED indexes through their owners: an empty body clears
    this page's outgoing links, and re-indexing a now-missing row removes its search
    entry. (Inbound links from OTHER pages are left dangling on purpose — they
    resolve as 'broken', the same lazy-resolve contract as a not-yet-created
    target.)"""
    if get_page(conn, page_id) is None:
        return False
    # page_versions AND page_comments both REFERENCE pages(id) with no ON DELETE, so
    # their rows MUST be cleared before the page row (deleting the parent first would
    # trip the FK — and a page that was ever commented on would be permanently
    # undeletable, surfacing as a 500). page_labels has ON DELETE CASCADE, so it needs
    # no explicit clear. The danger is a half-delete: dependents gone, page still here,
    # if the page delete then fails (e.g. a stray child's parent_id FK restricts it).
    # BEGIN IMMEDIATE + rollback-on-failure makes the whole set atomic — exactly the
    # pattern update_page uses — so a failed page delete restores its dependents too.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # attachments and watches key this page POLYMORPHICALLY (target_kind='page',
        # target_id) with NO foreign key, so the DB clears neither. Purge them in THIS
        # transaction so they die WITH the page rather than remaining as ghost
        # subscriptions/files. Reserved activity-target ids also prevent later
        # rebinding, but do not make dangling resources legitimate. purge_target returns
        # blob names to unlink post-commit (a filesystem side effect can't be atomic).
        # Notifications are left alone on purpose: they point at the append-only
        # activity log, which outlives the page, so they never dangle (see notifications.py).
        stored_names = attachments.purge_target(conn, "page", page_id)
        notifications.delete_watches_for(conn, "page", page_id)
        conn.execute("DELETE FROM page_versions WHERE page_id = ?", (page_id,))
        conn.execute("DELETE FROM page_comments WHERE page_id = ?", (page_id,))
        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # Post-commit, non-transactional side effects: unlink the now-orphaned blob files
    # (best-effort), then maintain the derived link/search indexes as before.
    attachments.unlink_blobs(config.ATTACH_DIR, stored_names)
    links.sync_links(conn, source_kind="page", source_id=page_id, body="")
    search.index_document(conn, kind="page", source_id=page_id)
    return True


def validate_move(
    conn: sqlite3.Connection, page: dict, new_parent_id: int | None
) -> str | None:
    """Is it legal to re-parent `page` under `new_parent_id`? Returns None if the
    move is allowed, else a human-readable reason the boundary turns into an error.
    A predicate the API and web surfaces share so the rule has ONE home (like
    issues.can_modify). `page` is the already-fetched row (the caller 404s a
    missing page before calling this).

    Moving to the top level (new_parent_id is None) is always allowed. Otherwise
    the new parent must (1) not be the page itself, (2) be a real page IN THE SAME
    SPACE — a tree can't span spaces, the same rule create enforces — and (3) not
    live inside this page's OWN subtree, which would detach a loop from the root.
    We test the cycle by walking UP from the proposed parent: if we reach the page
    being moved, the parent is one of its descendants."""
    if new_parent_id is None:
        return None
    if new_parent_id == page["id"]:
        return "A page cannot be its own parent."
    parent = get_page(conn, new_parent_id)
    if parent is None or parent["space_id"] != page["space_id"]:
        return "Parent must be a page in this space."
    node = parent
    seen: set[int] = set()
    while node is not None:
        if node["id"] == page["id"]:
            return "A page cannot be moved under one of its own descendants."
        if node["id"] in seen:
            # A pre-existing cycle in the stored tree that does NOT pass through the moved
            # page (e.g. from a concurrent-move race or a crafted import): stop rather than
            # walk it forever. This bounds the walk so one bad row can't spin a worker at
            # 100% CPU and exhaust the request thread-pool.
            break
        seen.add(node["id"])
        node = get_page(conn, node["parent_id"]) if node["parent_id"] else None
    return None


def move(
    conn: sqlite3.Connection, page: dict, new_parent_id: int | None
) -> tuple[dict | None, str | None]:
    """Re-parent a page ATOMICALLY: run validate_move and the UPDATE inside one
    BEGIN IMMEDIATE. validate_move alone can't stop a check-then-write race — two
    concurrent moves (move A under B / move B under A) each validate against the
    pre-move tree, both pass, then both write, forming a cycle. Holding the write lock
    across validate+write serializes them, so the second move re-validates against the
    first's committed result and is rejected. Returns (updated_page, None) on success,
    or (None, reason) when the move is illegal — the caller turns `reason` into its
    422/400. `page` is the already-fetched row; only its id/space_id are read (a move
    changes neither), while validate_move re-reads the live tree under the lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        reason = validate_move(conn, page, new_parent_id)
        if reason is not None:
            conn.rollback()
            return None, reason
        conn.execute(
            "UPDATE pages SET parent_id = ? WHERE id = ?", (new_parent_id, page["id"])
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_page(conn, page["id"]), None


def list_page_versions(conn: sqlite3.Connection, page_id: int) -> list[dict]:
    """A page's superseded revisions, newest first (highest version first). Empty
    for a page never edited. Does NOT include the live current revision — that is
    the `pages` row itself."""
    rows = conn.execute(
        "SELECT * FROM page_versions WHERE page_id = ? ORDER BY version DESC",
        (page_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_page_version(
    conn: sqlite3.Connection, page_id: int, version: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM page_versions WHERE page_id = ? AND version = ?",
        (page_id, version),
    ).fetchone()
    return dict(row) if row else None


def restore_version(
    conn: sqlite3.Connection, page_id: int, version: int, *, editor_id: int
) -> dict | None:
    """Restore a page's content to a prior revision. Returns the updated page, or
    None if no such page/version exists (so the boundary can 404).

    NON-DESTRUCTIVE: this is an ordinary edit, not a rewind. We read the snapshot
    and hand its title+body to update_page, which FIRST snapshots the page's current
    content into history, THEN overwrites the live row. So restoring v1 onto a page
    last saved as v4 turns the v4 content into the newest version and makes v1's
    content live — nothing is lost and you can always restore forward again. Reusing
    update_page also means restore inherits its history + link + search re-indexing
    for free, so a restore can never leave a derived index out of sync.

    Restoring content identical to the live row is a no-op: we guard it here because
    update_page only skips when NO fields are passed (and restore always passes
    both), so without this check an identical restore would file a redundant
    duplicate version. We return the page untouched instead."""
    snapshot = get_page_version(conn, page_id, version)
    if snapshot is None:
        return None
    current = get_page(conn, page_id)
    if current is None:  # page vanished between the version lookup and now -> 404
        return None
    if snapshot["title"] == current["title"] and snapshot["body"] == current["body"]:
        return current
    return update_page(
        conn,
        page_id,
        editor_id=editor_id,
        title=snapshot["title"],
        body=snapshot["body"],
    )
