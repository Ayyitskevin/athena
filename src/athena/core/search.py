"""Unified full-text search over issues and pages — the second one-roof payoff.

Because Aegis and Mentor share one database, a single FTS5 index (`search_index`,
migration 0013) can rank issue and page hits together. This module:

  * keeps that derived index current (index_document, called from the issue/page
    data-access layer on every write, exactly like core/links.sync_links),
  * answers a query, ranked best-first across both kinds, paged (search), and
  * enriches each hit with the little context that makes a result scannable — an
    issue's key (ATH-12) and status, a page's space key — by reading the source
    tables directly (the same direct-read contract used everywhere here, so still
    no aegis/mentor import and no dependency cycle).

It lives in core/ for the same reason links does: it is the one place that
deliberately knows about BOTH issues and pages. It reads those tables directly
via a fixed internal map and never imports aegis/mentor, so there is no
dependency cycle (aegis/mentor import core, never the reverse).

The index is DERIVED, not truth: the `issues`/`pages` rows are the truth, and a
row's entry is re-derived from the live row on every write. If the two ever drift,
the source row wins and a reindex repairs the index.
"""
from __future__ import annotations

import sqlite3

from athena.core import access

# SQLite binds integer query parameters as signed 64-bit values. Keep the
# pagination contract inside that range so an oversized request is rejected at
# the boundary instead of surfacing as an sqlite3 OverflowError.
MAX_OFFSET = 2**63 - 1

# The searchable kinds and the table each lives in. Both tables expose `title`
# and `body`. Values are fixed literals, never caller input, so building a query
# string from them is safe.
_SOURCE = {"issue": "issues", "page": "pages"}

# Sentinel for search()'s `actor`: "no visibility gating at all" (an internal caller,
# or a test exercising ranking). Distinct from actor=None, which is a real anonymous
# viewer who may see only public projects/spaces.
_UNGATED = object()


def index_document(
    conn: sqlite3.Connection, *, kind: str, source_id: int, commit: bool = True
) -> None:
    """Make the search_index entry for one source match its live row exactly.

    Called from the data-access layer after an issue/page is created or edited.
    We REPLACE (delete-then-insert) rather than diff: the source row is the single
    source of truth, so re-deriving from it is simplest and always correct — and,
    crucially, re-reading the WHOLE row means a title-only or body-only edit still
    indexes the full current text, not a stale half. If the row is gone (defensive
    — there is no delete path today), the entry is simply removed. ``commit=False``
    lets an application command commit the source, projections, and audit together."""
    table = _SOURCE[kind]
    row = conn.execute(
        f"SELECT title, body FROM {table} WHERE id = ?", (source_id,)
    ).fetchone()
    conn.execute(
        "DELETE FROM search_index WHERE kind = ? AND source_id = ?", (kind, source_id)
    )
    if row is not None:
        conn.execute(
            "INSERT INTO search_index (kind, source_id, title, body) "
            "VALUES (?, ?, ?, ?)",
            (kind, source_id, row["title"], row["body"]),
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
    """Attach per-kind context to each hit so a result is scannable at a glance: an
    issue gets its key (ATH-12, or None for a backlog issue) and status; a page gets
    its space key. Resolved with ONE batch query per kind — not per hit — reading the
    source tables directly, the same contract index_document uses, so this stays free
    of any aegis/mentor import. A hit whose source row vanished between the FTS read
    and this read just gets no context and still renders by title."""
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
    for h in hits:
        if h["kind"] == "issue":
            ctx = issue_ctx.get(h["source_id"], {})
            h["key"] = ctx.get("key")
            h["status"] = ctx.get("status")
        elif h["kind"] == "page":
            ctx = page_ctx.get(h["source_id"], {})
            h["space_key"] = ctx.get("space_key")
    return hits


def _visibility_clause(
    conn: sqlite3.Connection, actor: dict | None
) -> tuple[str, list]:
    """Build the SQL fragment (with a leading AND) + params that keep only hits the
    actor may see: an issue hit whose project is visible (or backlog), a page hit whose
    space is visible. Returns ('', []) for an admin (who sees all — no gate needed).
    The subqueries resolve the visible source ids from the visible project/space sets,
    so we never enumerate them in Python. An actor with no visible spaces matches no
    pages; backlog issues (no project) always pass."""
    vis_projects = access.visible_project_filter(conn, actor)
    if vis_projects is None:  # admin → unrestricted
        return "", []
    vis_spaces = access.visible_space_filter(conn, actor)

    params: list = []
    if vis_projects:
        ph = ",".join("?" for _ in vis_projects)
        issue_src = f"SELECT id FROM issues WHERE project_id IS NULL OR project_id IN ({ph})"
        params.extend(vis_projects)
    else:
        issue_src = "SELECT id FROM issues WHERE project_id IS NULL"
    if vis_spaces:
        ph = ",".join("?" for _ in vis_spaces)
        page_src = f"SELECT id FROM pages WHERE space_id IN ({ph})"
        params.extend(vis_spaces)
    else:
        page_src = "SELECT id FROM pages WHERE 0"  # no visible spaces → no page hits
    clause = (
        f"AND ((kind = 'issue' AND source_id IN ({issue_src})) "
        f"OR (kind = 'page' AND source_id IN ({page_src}))) "
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
    """Best-first hits for `query` across issues and pages.

    Each hit is {kind, source_id, title, snippet} plus the per-kind context _enrich
    adds (an issue's key/status, a page's space_key). The snippet is a short body
    excerpt with the matched terms wrapped in [..] for highlighting. Ranking is bm25
    with the title column weighted above the body, so a title match outranks a
    body-only match. An empty/whitespace query returns [] (a search box with no input
    shows nothing, it does not dump the table). `kind` optionally narrows to one side
    ('issue' | 'page'); an unknown kind simply matches nothing. `limit`/`offset` page
    the ranked result set, so a caller can fetch one window at a time.

    `ids` optionally restricts the hits to these source ids — the generic hook that
    lets a caller intersect full-text relevance with a structured pre-filter (e.g.
    aegis narrows an issue search to the issues that match status/label/project).
    source_ids are per-kind, so a caller passing `ids` must also fix `kind`; an empty
    list matches nothing (an "IN ()" is both invalid SQL and the right answer).

    `actor` gates the hits by visibility: an issue hit survives only if its project is
    visible to the actor (or it is a backlog issue), and a page hit only if its space
    is. The default (_UNGATED) applies no gate — for internal callers and ranking
    tests; pass the real actor (a user dict, or None for anonymous) to filter. Gating
    happens in SQL, before LIMIT/OFFSET, so paging stays correct. An admin sees
    everything, so no predicate is added for them.

    `include_archived` defaults False: archived (soft-deleted) issues drop out of every
    list, so search excludes them too — the derived FTS index doesn't carry the archived
    flag, so the exclusion is applied here by source. Pages have no archival, so this
    only ever removes archived issue hits. Pass True to search the archive as well."""
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
        clause, vis_params = _visibility_clause(conn, actor)
        if clause:
            sql += clause
            params.extend(vis_params)
    if not include_archived:
        # Exclude archived issues by source (the FTS index has no archived flag). Pages
        # have no archival, so only archived issue hits are removed. In SQL, before
        # LIMIT/OFFSET, so paging stays correct.
        sql += (
            "AND NOT (kind = 'issue' AND source_id IN "
            "(SELECT id FROM issues WHERE archived_at IS NOT NULL)) "
        )
    # bm25() column order is (kind, source_id, title, body); weight title 2x body.
    sql += "ORDER BY bm25(search_index, 0.0, 0.0, 2.0, 1.0) LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return _enrich(conn, [dict(r) for r in rows])
