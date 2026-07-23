"""Typed dependencies between issues: blocks / blocked-by / relates-to.

This is NOT core/links — that module indexes [[issue:N]] mentions parsed out of
body text and re-derives them on every write. These are explicit relationships a
user creates by choosing one ("ATH-12 blocks ATH-15"). They live in issue_links.

The storage has two canonical kinds; the boundary speaks three user-facing
relations and this module maps between them:

    relation       stored row                       meaning
    ----------     ------------------------------   --------------------------
    "blocks"       (from, to,   'blocks')           from blocks to
    "blocked_by"   (to,   from, 'blocks')           the SAME edge, other end
    "relates"      (min,  max,  'relates')          symmetric, one row

So "blocked_by" is never its own stored row — it is a "blocks" row read from the
target's side. "relates" is symmetric, so we normalize the pair order (smaller id
first) to keep it a single row no matter which side asked.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issues, statuses
from athena.core import access

# What a form / API may ask for. The stored kinds are just {'blocks','relates'}.
RELATIONS = ("blocks", "blocked_by", "relates")

# Sentinel for "no visibility gating" — distinct from a real actor dict or None
# (anonymous). Internal/test callers default to _UNGATED; the boundary passes the real
# viewer so a relationship to an issue in a private project the viewer can't see is
# dropped from the rendered summary (it would otherwise leak that issue's key/title).
_UNGATED = object()


def _summary(issue: dict) -> dict:
    """The slice of an issue a relationship row needs to render: enough to link to
    it and show its state, no more. key is the display key (ATH-12) or None."""
    return {
        "id": issue["id"],
        "key": issue["key"],
        "title": issue["title"],
        "status": issue["status"],
    }


def add_link(
    conn: sqlite3.Connection,
    *,
    from_id: int,
    to_id: int,
    relation: str,
    created_by: int,
    commit: bool = True,
) -> tuple[str | None, bool]:
    """Create a relationship declared FROM from_id. Returns ``(reason, inserted)``:
    ``reason`` is None on success, else a human-readable string the boundary turns
    into an error; ``inserted`` is True only when a NEW edge row was written, so a
    caller can record an audit event only for a real change and stay silent on an
    idempotent re-add (INSERT OR IGNORE makes re-adding an identical edge a no-op).

    from_id is the issue whose page the user is acting on; to_id is the other
    issue (already resolved from a ref by the boundary). Permission is the
    boundary's job — this layer only enforces shape and integrity. ``commit=False``
    lets an audited command bundle the edge and its activity event in one
    transaction."""
    if relation not in RELATIONS:
        return "Unknown relationship type.", False
    if from_id == to_id:
        return "An issue can't depend on itself.", False
    if issues.get_issue(conn, from_id) is None or issues.get_issue(conn, to_id) is None:
        return "No such issue.", False

    if relation == "blocks":
        a, b, kind = from_id, to_id, "blocks"
    elif relation == "blocked_by":
        a, b, kind = to_id, from_id, "blocks"
    else:  # relates — symmetric, normalize so the pair is one row either way
        a, b = (from_id, to_id) if from_id < to_id else (to_id, from_id)
        kind = "relates"

    # Reject the nonsensical direct contradiction: A blocks B AND B blocks A. (We
    # don't chase longer transitive cycles here — that's deferred; the direct
    # 2-cycle is the one a user trips on and it's cheap to catch.)
    if kind == "blocks":
        contradiction = conn.execute(
            "SELECT 1 FROM issue_links WHERE from_id = ? AND to_id = ? AND kind = 'blocks'",
            (b, a),
        ).fetchone()
        if contradiction is not None:
            return "Those two issues already block each other the other way.", False

    cur = conn.execute(
        "INSERT OR IGNORE INTO issue_links (from_id, to_id, kind, created_by) "
        "VALUES (?, ?, ?, ?)",
        (a, b, kind, created_by),
    )
    if commit:
        conn.commit()
    return None, cur.rowcount > 0


def remove_link(
    conn: sqlite3.Connection,
    *,
    from_id: int,
    to_id: int,
    relation: str,
    commit: bool = True,
) -> bool:
    """Remove a relationship, addressed by the same user-facing relation used to
    create it. Returns True if a row was deleted, False if there was nothing to
    delete (the boundary 404s that). Normalizes exactly like add_link so a
    "blocked_by" removal targets the underlying "blocks" row, and a "relates"
    removal finds the single normalized pair."""
    if relation not in RELATIONS:
        return False
    if relation == "blocks":
        a, b, kind = from_id, to_id, "blocks"
    elif relation == "blocked_by":
        a, b, kind = to_id, from_id, "blocks"
    else:
        a, b = (from_id, to_id) if from_id < to_id else (to_id, from_id)
        kind = "relates"
    cur = conn.execute(
        "DELETE FROM issue_links WHERE from_id = ? AND to_id = ? AND kind = ?",
        (a, b, kind),
    )
    conn.commit()
    return cur.rowcount > 0


def _others(
    conn: sqlite3.Connection, ids: list[int], actor: dict | None | object = _UNGATED
) -> list[dict]:
    """Summaries for a list of issue ids, in the given order, skipping any that
    vanished (a CASCADE should make that impossible, but a read shouldn't crash on
    a race) AND any the actor may not see. Small N — these are one issue's
    relationships — so a fetch per id is fine and reuses issues.get_issue's key
    computation. `actor` drops summaries for issues in a private project the viewer
    can't see (their key/title/status would otherwise leak through the relationship);
    _UNGATED keeps everything (internal callers)."""
    out = []
    for i in ids:
        if actor is not _UNGATED and not access.can_see_issue(conn, actor, i):
            continue
        issue = issues.get_issue(conn, i)
        if issue is not None:
            out.append(_summary(issue))
    return out


def list_links(
    conn: sqlite3.Connection, issue_id: int, *, actor: dict | None | object = _UNGATED
) -> dict:
    """All of one issue's relationships, grouped by user-facing relation:

      {"blocks": [...], "blocked_by": [...], "relates": [...]}

    - blocks:     issues THIS one blocks    (its 'blocks' rows, other end = to_id)
    - blocked_by: issues that block THIS one ('blocks' rows pointing at it)
    - relates:    symmetric peers           ('relates' rows on either end)

    Each entry is a _summary (id, key, title, status) so the template can link to
    it and show its state. `actor` gates each linked target by visibility — a
    relationship to an issue in a private project the viewer can't see is omitted, so
    its key/title/status never leaks through the relationship list. _UNGATED (the
    default) keeps every relationship (internal/test callers)."""
    blocks = [
        r["to_id"]
        for r in conn.execute(
            "SELECT to_id FROM issue_links WHERE from_id = ? AND kind = 'blocks' "
            "ORDER BY to_id",
            (issue_id,),
        )
    ]
    blocked_by = [
        r["from_id"]
        for r in conn.execute(
            "SELECT from_id FROM issue_links WHERE to_id = ? AND kind = 'blocks' "
            "ORDER BY from_id",
            (issue_id,),
        )
    ]
    relates = [
        (r["to_id"] if r["from_id"] == issue_id else r["from_id"])
        for r in conn.execute(
            "SELECT from_id, to_id FROM issue_links "
            "WHERE kind = 'relates' AND (from_id = ? OR to_id = ?) "
            "ORDER BY from_id, to_id",
            (issue_id, issue_id),
        )
    ]
    return {
        "blocks": _others(conn, blocks, actor),
        "blocked_by": _others(conn, blocked_by, actor),
        "relates": _others(conn, relates, actor),
    }


def open_blockers(
    conn: sqlite3.Connection, issue_id: int, *, actor: dict | None | object = _UNGATED
) -> list[dict]:
    """Issues that block this one AND are not yet closed — the reason a close should
    warn. Returns summaries (possibly empty). "Closed" is category-based now
    (statuses.is_done), so a blocker in any project's done-category status counts as
    resolved, and anything else is still an open blocker. Each blocker's done-ness
    depends on its OWN project's status set, so we resolve it per row rather than in
    one SQL comparison. `actor` gates each blocker by visibility, so the close-warning
    never reveals a blocker in a private project the closer can't see; _UNGATED keeps
    all (internal callers)."""
    rows = conn.execute(
        "SELECT l.from_id AS blocker, i.status, i.project_id FROM issue_links l "
        "JOIN issues i ON i.id = l.from_id "
        "WHERE l.to_id = ? AND l.kind = 'blocks' "
        "ORDER BY l.from_id",
        (issue_id,),
    ).fetchall()
    open_ids = [
        r["blocker"]
        for r in rows
        if not statuses.is_done(conn, r["project_id"], r["status"])
    ]
    return _others(conn, open_ids, actor)
