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
        if actor is not _UNGATED:
            assert actor is None or isinstance(actor, dict)
            if not access.can_see_issue(conn, actor, i):
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


def open_blockers_by_issue(
    conn: sqlite3.Connection,
    issue_ids: list[int],
    *,
    actor: dict | None | object = _UNGATED,
) -> dict[int, list[dict]]:
    """Open blockers for many issues in a handful of queries, not one per chair.

    Same meaning as :func:`open_blockers`: only ``blocks`` edges, only blockers
    that are not done, visibility-gated when ``actor`` is not ``_UNGATED``.
    """
    ids = sorted({int(i) for i in issue_ids})
    empty: dict[int, list[dict]] = {i: [] for i in ids}
    if not ids:
        return empty
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT l.to_id AS blocked_id, l.from_id AS blocker, "
        "i.status, i.project_id FROM issue_links l "
        "JOIN issues i ON i.id = l.from_id "
        "WHERE l.to_id IN (" + placeholders + ") AND l.kind = 'blocks' "
        "ORDER BY l.to_id, l.from_id",
        ids,
    ).fetchall()
    open_ids_by_blocked: dict[int, list[int]] = {i: [] for i in ids}
    blocker_ids: list[int] = []
    for row in rows:
        if statuses.is_done(conn, row["project_id"], row["status"]):
            continue
        blocked_id = int(row["blocked_id"])
        blocker = int(row["blocker"])
        open_ids_by_blocked.setdefault(blocked_id, []).append(blocker)
        blocker_ids.append(blocker)
    visible_blockers: list[int] = []
    for blocker in sorted(set(blocker_ids)):
        if actor is not _UNGATED:
            assert actor is None or isinstance(actor, dict)
            if not access.can_see_issue(conn, actor, blocker):
                continue
        visible_blockers.append(blocker)
    summaries: dict[int, dict] = {}
    if visible_blockers:
        placeholders = ",".join("?" for _ in visible_blockers)
        for row in conn.execute(
            issues.select_sql() + f" WHERE i.id IN ({placeholders})",
            visible_blockers,
        ).fetchall():
            issue = issues.to_issue(row)
            summaries[int(issue["id"])] = _summary(issue)
    out = empty
    for blocked_id, blockers in open_ids_by_blocked.items():
        out[blocked_id] = [summaries[bid] for bid in blockers if bid in summaries]
    return out


def edges_among(conn: sqlite3.Connection, issue_ids: list[int]) -> list[dict]:
    """Every declared dependency whose BOTH ends are in the given set of issues.

    The per-issue reader above is right for one issue's relationship panel and
    wrong for a picture: drawing a project's worth of issues through it costs
    three queries per issue plus a visibility check and a fetch per link. This is
    the set-scoped read a drawn view needs — one query, no N+1.

    **Both ends must be in the set, and that is the visibility gate.** Callers
    pass a set they have already filtered, so an edge to an issue the viewer
    cannot see simply has no second endpoint here and is never selected. It is
    also what keeps an edge from pointing into empty space: an arrow to something
    off the picture reads as a rendering bug rather than a boundary, so a view
    that wants to admit those has to count them itself and say so.

    Returns ``[{"from_id", "to_id", "kind"}]`` ordered deterministically, so a
    layout built from it is reproducible. ``blocks`` is directed (from_id blocks
    to_id); ``relates`` is stored once with the lower id first and is therefore
    already exactly one row per pair — neither needs de-duplication here.
    """
    if len(issue_ids) < 2:
        # One issue cannot have an edge with both ends inside the set, and zero
        # issues would build an empty IN () that SQLite rejects.
        return []
    placeholders = ",".join("?" for _ in issue_ids)
    ids = sorted(issue_ids)
    rows = conn.execute(
        "SELECT from_id, to_id, kind FROM issue_links"
        f" WHERE from_id IN ({placeholders}) AND to_id IN ({placeholders})"
        " ORDER BY kind, from_id, to_id",
        [*ids, *ids],
    ).fetchall()
    return [dict(row) for row in rows]


def count_edges_touching(
    conn: sqlite3.Connection,
    issue_ids: list[int],
    *,
    visible_project_ids: set[int] | None = None,
) -> int:
    """How many declared dependencies have EXACTLY ONE end in the given set, and
    an off-picture end this viewer may see.

    These are the edges a bounded picture cannot draw — the other end is outside
    the view. A view that drops them silently is telling the operator this work
    has no outside dependencies, so the number exists to be said out loud beside
    the drawing.

    The off-picture end is visibility-gated for the same reason the drawn set is:
    an ungated count moves when a hidden issue gains a link to a visible one, and
    a number that reacts to private work is an existence oracle no less than a
    title would be. What the viewer cannot see does not exist here — not as a
    row, and not as an increment.

    An ARCHIVED far end does not count either: the picture this number sits
    beside shows live work only, so "N dependencies reach beyond it" must mean
    edges to work that could be drawn somewhere — a link onto archived history
    is not an outside dependency of the live picture (review finding).
    """
    if not issue_ids:
        return 0
    placeholders = ",".join("?" for _ in issue_ids)
    ids = sorted(issue_ids)
    # The far end of the edge — whichever side is NOT in the drawn set.
    far_end = f"CASE WHEN l.from_id IN ({placeholders}) THEN l.to_id ELSE l.from_id END"
    clauses = [
        f"(l.from_id IN ({placeholders})) != (l.to_id IN ({placeholders}))",
        "o.archived_at IS NULL",
    ]
    params: list = [*ids, *ids, *ids]
    if visible_project_ids is not None:
        if visible_project_ids:
            vis = ",".join("?" for _ in visible_project_ids)
            clauses.append(f"(o.project_id IS NULL OR o.project_id IN ({vis}))")
            params.extend(sorted(visible_project_ids))
        else:
            clauses.append("o.project_id IS NULL")
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM issue_links l"
        f" JOIN issues o ON o.id = ({far_end})"
        f" WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return int(row["n"])
