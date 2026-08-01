"""Unified full-text search across issues, pages, comments, and Room events.

Because Aegis, Mentor, and Rooms share one database, a single FTS5 index
(`search_index`, migration 0013) can rank work, documentation, discussion, and
coordination hits together. This module:

  * keeps that derived index current (index_document, called from entity/comment
    writers and the Room event command, exactly like core/links.sync_links),
  * answers a query, ranked best-first across every supported kind, paged (search),
    and
  * enriches each hit with the context that makes it scannable — issue key/status,
    page space, comment parent coordinates, or Room/project coordinates and event
    kind — by reading the source tables directly.

It lives in core/ for the same reason links does: it deliberately knows about
multiple source tables while never importing aegis/mentor modules, so there is no
dependency cycle (aegis/mentor import core, never the reverse).

The index is DERIVED, not truth: the mapped source rows and Room event metadata are
the truth, and entries are re-derived from their live sources on every write. If
they drift, the source rows win and a reindex repairs the index.
"""

from __future__ import annotations

import sqlite3
from typing import TypedDict, cast

from athena.core import access

# SQLite binds integer query parameters as signed 64-bit values. Keep the
# pagination contract inside that range so an oversized request is rejected at
# the boundary instead of surfacing as an sqlite3 OverflowError.
MAX_OFFSET = 2**63 - 1


# The searchable kinds and how to read each one's text: the table, its title column
# (None for comments and Room activity, which have no title of their own), and its body
# column. Issues/pages carry title + body; comments and Room events carry only prose
# (their FTS title is indexed empty and _enrich supplies parent/Room context). Values
# are fixed literals, never caller input, so building a query string from them is safe.
class _SourceSpec(TypedDict):
    table: str
    title: str | None
    body: str


_SOURCE: dict[str, _SourceSpec] = {
    "issue": {"table": "issues", "title": "title", "body": "body"},
    "page": {"table": "pages", "title": "title", "body": "body"},
    "issue_comment": {"table": "comments", "title": None, "body": "body"},
    "page_comment": {"table": "page_comments", "title": None, "body": "body"},
    "room_event": {"table": "activity", "title": None, "body": "detail"},
}

# Comment kinds → the parent (kind, table, foreign-key column) the hit resolves to for its
# title, context, and link. A comment has no address of its own in the UI; it is read and
# linked through the issue/page it hangs off.
_COMMENT_PARENT = {
    "issue_comment": {"parent_kind": "issue", "table": "comments", "fk": "issue_id"},
    "page_comment": {"parent_kind": "page", "table": "page_comments", "fk": "page_id"},
}

# Sentinel for search()'s `actor`: "no visibility gating at all" (an internal caller,
# or a test exercising ranking). Distinct from actor=None, which is a real anonymous
# viewer who may see only public projects/spaces.
_UNGATED = object()


def index_document(
    conn: sqlite3.Connection, *, kind: str, source_id: int, commit: bool = True
) -> None:
    """Make the search_index entry for one source match its live row exactly.

    Called after an issue/page/comment is created or edited and after a Room event is
    appended. We REPLACE (delete-then-insert) rather than diff: the source row is the
    single source of truth, so re-deriving from it is simplest and always correct —
    and, crucially, re-reading the WHOLE row means a title-only or body-only edit still
    indexes the full current text, not a stale half. If the row is gone (a deleted
    comment or purged page), the entry is simply removed — so calling this AFTER a
    delete clears the FTS entry. ``commit=False`` lets an application command commit
    the source, projections, and audit together.

    Comments and Room activity have no title column, so their FTS title is indexed
    empty (`_SOURCE[kind]` carries title=None); _enrich supplies the parent or Room
    title and coordinates at read time."""
    spec = _SOURCE[kind]
    cols = spec["body"] if spec["title"] is None else f"{spec['title']}, {spec['body']}"
    row = conn.execute(
        f"SELECT {cols} FROM {spec['table']} WHERE id = ?", (source_id,)
    ).fetchone()
    conn.execute(
        "DELETE FROM search_index WHERE kind = ? AND source_id = ?", (kind, source_id)
    )
    if row is not None:
        title = row[spec["title"]] if spec["title"] is not None else ""
        conn.execute(
            "INSERT INTO search_index (kind, source_id, title, body) "
            "VALUES (?, ?, ?, ?)",
            (kind, source_id, title, row[spec["body"]]),
        )
    if commit:
        conn.commit()


def _to_match(query: str) -> str:
    """Turn raw user text into a SAFE FTS5 MATCH expression.

    A search box must never let user text reach the FTS5 query parser as syntax:
    bare input like `set:up(` or `AND` would either error or act as operators. So
    we tokenize on whitespace and rebuild each token as a quoted prefix term —
    `"token"*` — escaping any internal quote by doubling it. Quoting makes every
    token a literal (operators in user input become text), and the trailing `*`
    makes it a prefix match so "desig" finds "design". Joining the quoted terms
    with spaces means FTS5 ANDs them: every term must appear. The empty string
    maps to "" and the caller short-circuits before MATCHing it."""
    terms = query.split()
    return " ".join(f'"{t.replace(chr(34), chr(34) * 2)}"*' for t in terms)


def _enrich(conn: sqlite3.Connection, hits: list[dict]) -> list[dict]:
    """Attach scannable per-kind context: issue key/status, page space, a comment's
    parent coordinates/title, or a Room event's room/project coordinates, event kind,
    and room title. Each kind is resolved with one batch query, not per hit, by reading
    source tables directly; this stays free of aegis/mentor imports. A hit whose source
    row vanished between the FTS read and enrichment gets empty context."""
    issue_ids = [h["source_id"] for h in hits if h["kind"] == "issue"]
    page_ids = [h["source_id"] for h in hits if h["kind"] == "page"]
    issue_ctx: dict[int, dict] = {}
    if issue_ids:
        ph = ",".join("?" for _ in issue_ids)
        for row in conn.execute(
            f"SELECT i.id, i.status, i.project_seq, p.key AS project_key "
            f"FROM issues i LEFT JOIN projects p ON p.id = i.project_id "
            f"WHERE i.id IN ({ph})",
            issue_ids,
        ).fetchall():
            key = (
                f"{row['project_key']}-{row['project_seq']}"
                if row["project_key"] and row["project_seq"] is not None
                else None
            )
            issue_ctx[row["id"]] = {"key": key, "status": row["status"]}
    page_ctx: dict[int, dict] = {}
    if page_ids:
        ph = ",".join("?" for _ in page_ids)
        for row in conn.execute(
            f"SELECT pg.id, s.key AS space_key "
            f"FROM pages pg JOIN spaces s ON s.id = pg.space_id "
            f"WHERE pg.id IN ({ph})",
            page_ids,
        ).fetchall():
            page_ctx[row["id"]] = {"space_key": row["space_key"]}
    # Comment hits borrow their PARENT's identity: a comment has no title/link of its own,
    # so it renders and links through the issue/page it hangs off. Resolve each comment
    # kind's parents in ONE batch query (joining comment → parent → project/space), keyed
    # by comment id.
    ic_ids = [h["source_id"] for h in hits if h["kind"] == "issue_comment"]
    pc_ids = [h["source_id"] for h in hits if h["kind"] == "page_comment"]
    ic_ctx: dict[int, dict] = {}
    if ic_ids:
        ph = ",".join("?" for _ in ic_ids)
        for row in conn.execute(
            f"SELECT c.id AS cid, i.id AS pid, i.title, i.status, i.project_seq, "
            f"p.key AS project_key FROM comments c JOIN issues i ON i.id = c.issue_id "
            f"LEFT JOIN projects p ON p.id = i.project_id WHERE c.id IN ({ph})",
            ic_ids,
        ).fetchall():
            key = (
                f"{row['project_key']}-{row['project_seq']}"
                if row["project_key"] and row["project_seq"] is not None
                else None
            )
            ic_ctx[row["cid"]] = {
                "parent_id": row["pid"],
                "title": row["title"],
                "key": key,
                "status": row["status"],
            }
    pc_ctx: dict[int, dict] = {}
    if pc_ids:
        ph = ",".join("?" for _ in pc_ids)
        for row in conn.execute(
            f"SELECT c.id AS cid, pg.id AS pid, pg.title, s.key AS space_key "
            f"FROM page_comments c JOIN pages pg ON pg.id = c.page_id "
            f"JOIN spaces s ON s.id = pg.space_id WHERE c.id IN ({ph})",
            pc_ids,
        ).fetchall():
            pc_ctx[row["cid"]] = {
                "parent_id": row["pid"],
                "title": row["title"],
                "space_key": row["space_key"],
            }
    # Room-event prose lives on activity; metadata supplies the navigable Room,
    # project, event kind, and display title in one batch.
    room_event_ids = [h["source_id"] for h in hits if h["kind"] == "room_event"]
    room_event_ctx: dict[int, dict] = {}
    if room_event_ids:
        ph = ",".join("?" for _ in room_event_ids)
        for row in conn.execute(
            f"SELECT re.activity_id, re.event_kind, r.id AS room_id, "
            f"r.project_id, r.slug AS room_slug, r.title "
            f"FROM room_events re JOIN rooms r ON r.id = re.room_id "
            f"WHERE re.activity_id IN ({ph})",
            room_event_ids,
        ).fetchall():
            room_event_ctx[row["activity_id"]] = dict(row)
    for h in hits:
        if h["kind"] == "issue":
            ctx = issue_ctx.get(h["source_id"], {})
            h["key"] = ctx.get("key")
            h["status"] = ctx.get("status")
        elif h["kind"] == "page":
            ctx = page_ctx.get(h["source_id"], {})
            h["space_key"] = ctx.get("space_key")
        elif h["kind"] == "issue_comment":
            ctx = ic_ctx.get(h["source_id"], {})
            h["parent_kind"] = "issue"
            h["parent_id"] = ctx.get("parent_id")
            h["title"] = ctx.get("title") or ""  # borrow the parent issue's title
            h["key"] = ctx.get("key")
            h["status"] = ctx.get("status")
        elif h["kind"] == "page_comment":
            ctx = pc_ctx.get(h["source_id"], {})
            h["parent_kind"] = "page"
            h["parent_id"] = ctx.get("parent_id")
            h["title"] = ctx.get("title") or ""  # borrow the parent page's title
            h["space_key"] = ctx.get("space_key")
        elif h["kind"] == "room_event":
            ctx = room_event_ctx.get(h["source_id"], {})
            h["room_id"] = ctx.get("room_id")
            h["room_slug"] = ctx.get("room_slug")
            h["project_id"] = ctx.get("project_id")
            h["event_kind"] = ctx.get("event_kind")
            h["title"] = ctx.get("title") or ""
    return hits


def _visibility_clause(
    conn: sqlite3.Connection, actor: dict | None
) -> tuple[str, list]:
    """Build the leading-AND SQL fragment and params that retain only visible hits.

    Issues inherit project visibility (with backlog remaining visible), pages inherit
    space visibility, and comments inherit their parent issue/page audience. Room
    events require native Room metadata, current Room access, and their immutable
    activity visibility envelope. Admins bypass audience filtering, but Room hits must
    still resolve to live native metadata. Subqueries resolve visible source ids in SQL, so
    Python never enumerates them.

    A kind omitted from the disjunction would be dropped entirely, so every searchable
    kind must appear here — the reason this gate and _SOURCE are edited together."""
    vis_projects = access.visible_project_filter(conn, actor)
    if vis_projects is None:  # admin → unrestricted except native Room provenance
        return (
            "AND (kind <> 'room_event' OR source_id IN ("
            "SELECT re.activity_id FROM room_events re "
            "JOIN activity room_activity ON room_activity.id = re.activity_id "
            "JOIN rooms r ON r.id = re.room_id "
            "JOIN projects p ON p.id = r.project_id "
            "AND p.activity_scope_key = r.project_scope_key "
            "WHERE room_activity.imported_at IS NULL)) ",
            [],
        )
    vis_spaces = access.visible_space_filter(conn, actor)

    params: list = []
    if vis_projects:
        ph = ",".join("?" for _ in vis_projects)
        issue_where = f"project_id IS NULL OR project_id IN ({ph})"
        room_project_where = f"r.project_id IN ({ph})"
    else:
        issue_where = "project_id IS NULL"
        room_project_where = "0"
    if vis_spaces:
        ph2 = ",".join("?" for _ in vis_spaces)
        page_where = f"space_id IN ({ph2})"
    else:
        page_where = "0"  # no visible spaces means no page/comment hit

    issue_src = f"SELECT id FROM issues WHERE {issue_where}"
    ic_src = (
        f"SELECT c.id FROM comments c JOIN issues i ON i.id = c.issue_id "
        f"WHERE {issue_where.replace('project_id', 'i.project_id')}"
    )
    page_src = f"SELECT id FROM pages WHERE {page_where}"
    pc_src = (
        f"SELECT c.id FROM page_comments c JOIN pages pg ON pg.id = c.page_id "
        f"WHERE {page_where.replace('space_id', 'pg.space_id')}"
    )
    if actor is None:
        room_access = f"{room_project_where} AND r.visibility = 'project'"
        room_params: list[object] = list(vis_projects)
    else:
        room_access = (
            f"{room_project_where} AND (r.visibility = 'project' OR "
            "(r.visibility = 'members' AND (p.created_by = ? OR EXISTS ("
            "SELECT 1 FROM project_members pm WHERE pm.project_id = r.project_id "
            "AND pm.user_id = ?))))"
        )
        room_params = [*vis_projects, actor["id"], actor["id"]]
    event_gate, event_gate_params = access.event_visibility_clause(
        conn, actor, alias="room_activity"
    )
    room_src = (
        "SELECT re.activity_id FROM room_events re "
        "JOIN activity room_activity ON room_activity.id = re.activity_id "
        "JOIN rooms r ON r.id = re.room_id "
        "JOIN projects p ON p.id = r.project_id "
        "AND p.activity_scope_key = r.project_scope_key "
        f"WHERE room_activity.imported_at IS NULL AND {room_access}"
        f"{f' AND ({event_gate})' if event_gate else ''}"
    )
    room_params.extend(event_gate_params)
    if vis_projects:
        params.extend(vis_projects)
        params.extend(vis_projects)
    if vis_spaces:
        params.extend(vis_spaces)
        params.extend(vis_spaces)
    params.extend(room_params)
    clause = (
        f"AND ((kind = 'issue' AND source_id IN ({issue_src})) "
        f"OR (kind = 'issue_comment' AND source_id IN ({ic_src})) "
        f"OR (kind = 'page' AND source_id IN ({page_src})) "
        f"OR (kind = 'page_comment' AND source_id IN ({pc_src})) "
        f"OR (kind = 'room_event' AND source_id IN ({room_src}))) "
    )
    return clause, params


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 20,
    offset: int = 0,
    ids: list[int] | None = None,
    include_archived: bool = False,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """Best-first hits for `query` across every supported source kind.

    Each hit starts with {kind, source_id, title, snippet}; _enrich adds issue
    key/status, page space, comment parent coordinates, or Room/project coordinates
    and event kind. The snippet is a short body excerpt with matched terms wrapped in
    [..] for highlighting. Ranking is bm25 with title weighted above body. An empty or
    whitespace query returns [] rather than dumping the table. `kind` may narrow to
    issue, page, issue_comment, page_comment, or room_event; an unknown kind matches
    nothing. `limit`/`offset` page the ranked result set.

    `ids` optionally restricts the hits to these source ids — the generic hook that
    lets a caller intersect full-text relevance with a structured pre-filter (e.g.
    aegis narrows an issue search to the issues that match status/label/project).
    source_ids are per-kind, so a caller passing `ids` must also fix `kind`; an empty
    list matches nothing (an "IN ()" is both invalid SQL and the right answer).

    `actor` gates hits by visibility: issues use their project (or backlog), pages use
    their space, comments use their parent, and Room events use current Room access plus
    the immutable activity envelope. The default (_UNGATED) applies no gate for
    internal callers and ranking tests; pass a user dict or None for anonymous access.
    Gating happens in SQL before LIMIT/OFFSET. Admins bypass audience restrictions, but
    Room-event hits still require native metadata.

    `include_archived` defaults False: archived issues/pages, comments whose parent is
    archived, and events in archived Rooms are excluded by source before paging. Pass
    True to include them."""
    if offset < 0 or offset > MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")
    if not query or not query.strip():
        return []
    if ids is not None and not ids:
        return []
    match = _to_match(query)
    sql = (
        "SELECT kind, source_id, title, "
        "snippet(search_index, 3, '[', ']', '…', 10) AS snippet "
        "FROM search_index WHERE search_index MATCH ? "
    )
    params: list = [match]
    if kind is not None:
        sql += "AND kind = ? "
        params.append(kind)
    if ids is not None:
        placeholders = ",".join("?" for _ in ids)
        sql += f"AND source_id IN ({placeholders}) "
        params.extend(ids)
    if actor is not _UNGATED:
        clause, vis_params = _visibility_clause(conn, cast(dict | None, actor))
        if clause:
            sql += clause
            params.extend(vis_params)
    if not include_archived:
        # The FTS index carries no archive flag. Drop archived issues/pages, comments
        # whose parent is archived, and events whose Room is archived. Apply every
        # exclusion before LIMIT/OFFSET so paging stays correct.
        sql += (
            "AND NOT (kind = 'issue' AND source_id IN "
            "(SELECT id FROM issues WHERE archived_at IS NOT NULL)) "
            "AND NOT (kind = 'page' AND source_id IN "
            "(SELECT id FROM pages WHERE archived_at IS NOT NULL)) "
            "AND NOT (kind = 'issue_comment' AND source_id IN "
            "(SELECT c.id FROM comments c JOIN issues i ON i.id = c.issue_id "
            "WHERE i.archived_at IS NOT NULL)) "
            "AND NOT (kind = 'page_comment' AND source_id IN "
            "(SELECT c.id FROM page_comments c JOIN pages pg ON pg.id = c.page_id "
            "WHERE pg.archived_at IS NOT NULL)) "
            "AND NOT (kind = 'room_event' AND source_id IN "
            "(SELECT re.activity_id FROM room_events re JOIN rooms r "
            "ON r.id = re.room_id WHERE r.archived_at IS NOT NULL)) "
        )
    # bm25() column order is (kind, source_id, title, body); weight title 2x body.
    sql += "ORDER BY bm25(search_index, 0.0, 0.0, 2.0, 1.0) LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return _enrich(conn, [dict(r) for r in rows])
