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

import re
import sqlite3
from typing import TypedDict, cast

from athena.core import access, db

# SQLite binds integer query parameters as signed 64-bit values. Keep the
# pagination contract inside that range so an oversized request is rejected at
# the boundary instead of surfacing as an sqlite3 OverflowError.
MAX_OFFSET = 2**63 - 1

#: How many matching entries one search ranks: the newest this many the caller
#: may see, of the kind asked for and not archived (H5). Ranking needs a score for
#: every candidate, so without a bound a search for a common word scored every
#: entry that contains it, and its cost grew with the workspace's history. A word
#: in fewer entries than this is ranked exactly as before; for a commoner word the
#: best hits are found among its newest entries, and paging stops at the window's
#: end. "Newest" is the order entries were last written: an edit re-indexes its
#: source, so an edited issue, page or comment is new again.
RANK_WINDOW = 1_000

# bm25() column order is (kind, source_id, title, body); weight title 2x body.
_RANK = "bm25(search_index, 0.0, 0.0, 2.0, 1.0)"


# The searchable kinds and how to read each one's text: the table, its title column
# (None for a comment, which has no title of its own), and its body column. Issues and
# pages carry title + body; issue/page comments carry only a body (the FTS title is
# indexed empty and the hit borrows its PARENT's title at read time, see _enrich).
# Values are fixed literals, never caller input, so building a query string from them is
# safe. The two comment kinds make discussion searchable alongside the docs it annotates.
class _SourceSpec(TypedDict):
    table: str
    title: str | None
    body: str


_SOURCE: dict[str, _SourceSpec] = {
    "issue": {"table": "issues", "title": "title", "body": "body"},
    "page": {"table": "pages", "title": "title", "body": "body"},
    "issue_comment": {"table": "comments", "title": None, "body": "body"},
    "page_comment": {"table": "page_comments", "title": None, "body": "body"},
}

#: Every kind a hit can be, and so every value ``kind=`` may narrow to. A closed
#: set: the search route refuses any other value rather than matching nothing.
KINDS: tuple[str, ...] = tuple(_SOURCE)

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
    conn: sqlite3.Connection, *, kind: str, source_id: int, commit: bool
) -> None:
    """Make the search_index entry for one source match its live row exactly.

    Called from the data-access layer after an issue/page/comment is created or edited.
    We REPLACE (delete-then-insert) rather than diff: the source row is the single
    source of truth, so re-deriving from it is simplest and always correct — and,
    crucially, re-reading the WHOLE row means a title-only or body-only edit still
    indexes the full current text, not a stale half. If the row is gone (a deleted
    comment, or a purged page), the entry is simply removed — so calling this AFTER a
    delete clears the FTS entry. ``commit=False`` lets an application command commit the
    source, projections, and audit together.

    A comment has no title column, so its FTS title is indexed empty (`_SOURCE[kind]`
    carries title=None); the hit borrows its parent's title at read time (_enrich).

    This is the ONE writer of `search_index` and of its key map `search_entries`
    (the search_entry_keys migration and tests/test_search_entry_keys.py hold
    that). The FTS table's
    kind and source_id columns are UNINDEXED, so the old entry is found through the
    map and deleted by rowid: the write's cost does not grow with the index. The
    rowid delete also checks the kind and source id, so a map row that went stale
    (an index written around this function) can never delete another source's
    entry. The map write replaces on either key: a stale row that names the
    rowid FTS hands the new entry is dropped, where an upsert on the source key
    alone refused the write, and every later one, on the rowid's uniqueness. A
    write elsewhere would leave an entry the map does not know, which the next
    write of that source would then fail to replace."""
    spec = _SOURCE[kind]
    cols = spec["body"] if spec["title"] is None else f"{spec['title']}, {spec['body']}"
    row = conn.execute(
        f"SELECT {cols} FROM {spec['table']} WHERE id = ?", (source_id,)
    ).fetchone()
    entry = conn.execute(
        "SELECT fts_rowid FROM search_entries WHERE kind = ? AND source_id = ?",
        (kind, source_id),
    ).fetchone()
    if entry is not None:
        conn.execute(
            "DELETE FROM search_index WHERE rowid = ? AND kind = ? AND source_id = ?",
            (entry["fts_rowid"], kind, source_id),
        )
    if row is not None:
        title = row[spec["title"]] if spec["title"] is not None else ""
        inserted = conn.execute(
            "INSERT INTO search_index (kind, source_id, title, body) "
            "VALUES (?, ?, ?, ?)",
            (kind, source_id, title, row[spec["body"]]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO search_entries (kind, source_id, fts_rowid) "
            "VALUES (?, ?, ?)",
            (kind, source_id, inserted.lastrowid),
        )
    elif entry is not None:
        conn.execute(
            "DELETE FROM search_entries WHERE kind = ? AND source_id = ?",
            (kind, source_id),
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


def _to_title_any_match(query: str) -> str:
    """A SAFE FTS5 expression matching any word, as a prefix, in the title column only.

    Words are reduced to letters and digits, so nothing user-typed can reach the FTS5
    parser as syntax; an input with no word left matches nothing."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", query)]
    if not words:
        return ""
    return "title : (" + " OR ".join(f'"{w}"*' for w in words) + ")"


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
    return hits


def _visibility_clause(
    conn: sqlite3.Connection, actor: dict | None
) -> tuple[str, list]:
    """Build the SQL fragment (with a leading AND) + params that keep only hits the
    actor may see: an issue hit whose project is visible (or backlog), a page hit whose
    space is visible, and a COMMENT hit whose parent issue/page is visible on the same
    rule (a comment inherits its parent's audience). Returns ('', []) for an admin (who
    sees all — no gate needed). The visible project/space sets are bound as
    parameters, so we never enumerate source ids in Python. An actor with no
    visible spaces matches no pages (nor page comments); backlog issues (no project)
    always pass, so do comments on them.

    Each arm asks about the one hit in hand, by its source row's primary key (H5):
    a hit costs a lookup or two, and no search builds the list of every visible
    issue, comment or page first, which made every search read all of them.

    A comment kind NOT listed in the disjunction would be dropped entirely (no
    arm can pass it), so every searchable kind must appear here — the reason this
    gate and the _SOURCE map are edited together."""
    vis_projects = access.visible_project_filter(conn, actor)
    if vis_projects is None:  # admin → unrestricted
        return "", []
    vis_spaces = access.visible_space_filter(conn, actor)

    params: list = []
    if vis_projects:
        ph = ",".join("?" for _ in vis_projects)
        issue_where = f"(gi.project_id IS NULL OR gi.project_id IN ({ph}))"
    else:
        issue_where = "gi.project_id IS NULL"
    if vis_spaces:
        ph2 = ",".join("?" for _ in vis_spaces)
        page_where = f"gp.space_id IN ({ph2})"
    else:
        page_where = ""  # no visible spaces → no page (or page-comment) hits

    # One correlated arm per kind, each reading the hit's own source row (and a
    # comment's parent) by primary key. Params are appended in clause order: issue
    # set, issue-comment set (issue params again), page set, page-comment set
    # (space again).
    issue_src = (
        "SELECT 1 FROM issues gi WHERE gi.id = search_index.source_id "
        f"AND {issue_where}"
    )
    ic_src = (
        "SELECT 1 FROM comments gc JOIN issues gi ON gi.id = gc.issue_id "
        f"WHERE gc.id = search_index.source_id AND {issue_where}"
    )
    page_src = (
        f"SELECT 1 FROM pages gp WHERE gp.id = search_index.source_id AND {page_where}"
    )
    pc_src = (
        "SELECT 1 FROM page_comments gc JOIN pages gp ON gp.id = gc.page_id "
        f"WHERE gc.id = search_index.source_id AND {page_where}"
    )
    if vis_projects:
        params.extend(vis_projects)  # issue_src
        params.extend(vis_projects)  # ic_src
    if vis_spaces:
        params.extend(vis_spaces)  # page_src
        params.extend(vis_spaces)  # pc_src
    # No visible space: no page arm at all, so no page or page comment can pass.
    page_arms = (
        f"OR (kind = 'page' AND EXISTS ({page_src})) "
        f"OR (kind = 'page_comment' AND EXISTS ({pc_src}))"
        if page_where
        else ""
    )
    clause = (
        f"AND ((kind = 'issue' AND EXISTS ({issue_src})) "
        f"OR (kind = 'issue_comment' AND EXISTS ({ic_src})) "
        f"{page_arms}) "
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
    title_any: bool = False,
) -> list[dict]:
    """Best-first hits for `query` across issues and pages.

    `title_any` (default False) loosens the match for a "is there something like this"
    lookup: ANY of the words may match, as prefixes, and only in the title column, instead
    of every word anywhere. Ranking, visibility and archive handling are unchanged.

    Each hit is {kind, source_id, title, snippet} plus the per-kind context _enrich
    adds (an issue's key/status, a page's space_key). The snippet is a short body
    excerpt with the matched terms wrapped in [..] for highlighting. Ranking is bm25
    with the title column weighted above the body, so a title match outranks a
    body-only match. An empty/whitespace query returns [] (a search box with no input
    shows nothing, it does not dump the table). `kind` optionally narrows to one side
    (one of KINDS); an unknown kind matches nothing here, and the REST route refuses it
    first. `limit`/`offset` page
    the ranked result set, so a caller can fetch one window at a time.

    Only the newest RANK_WINDOW matching entries that pass every filter below are
    ranked (H5): a search's cost is bounded by that window, not by how many
    entries in the whole history contain the word. Past the window's end a page
    is empty.

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

    `include_archived` defaults False: archived (soft-deleted) issues and pages drop out
    of every list, so search excludes them too — the derived FTS index doesn't carry the
    archived flag, so the exclusion is applied here by source for both kinds. Pass True to
    search the archive as well."""
    if offset < 0 or offset > MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")
    if not query or not query.strip():
        return []
    if ids is not None and not ids:
        return []
    match = _to_title_any_match(query) if title_any else _to_match(query)
    if not match:
        return []
    # The window: the newest RANK_WINDOW matching entries this search may return,
    # walked down the index newest first, each filtered as it is reached.
    sql = (
        f"SELECT rowid AS entry, {_RANK} AS score "
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
        # Exclude archived issues AND archived pages by source (the FTS index carries no
        # archived flag). Both kinds soft-delete via archived_at, so a hit is dropped
        # when its own row is archived — and a COMMENT is dropped when its PARENT is
        # archived, matching how the parent itself drops out (an archived doc hides its
        # discussion too). Applied in SQL before LIMIT/OFFSET, so paging stays correct.
        # Asked of the hit's own row by primary key, never as a list of every
        # archived source (which read every comment on every search).
        sql += (
            "AND NOT (kind = 'issue' AND EXISTS (SELECT 1 FROM issues ai "
            "WHERE ai.id = search_index.source_id AND ai.archived_at IS NOT NULL)) "
            "AND NOT (kind = 'page' AND EXISTS (SELECT 1 FROM pages ap "
            "WHERE ap.id = search_index.source_id AND ap.archived_at IS NOT NULL)) "
            "AND NOT (kind = 'issue_comment' AND EXISTS (SELECT 1 FROM comments ac "
            "JOIN issues ai ON ai.id = ac.issue_id "
            "WHERE ac.id = search_index.source_id AND ai.archived_at IS NOT NULL)) "
            "AND NOT (kind = 'page_comment' AND EXISTS (SELECT 1 FROM page_comments ac "
            "JOIN pages ap ON ap.id = ac.page_id "
            "WHERE ac.id = search_index.source_id AND ap.archived_at IS NOT NULL)) "
        )
    sql += "ORDER BY rowid DESC LIMIT ?"
    params.append(RANK_WINDOW)
    # The page: best first within the window. Equal scores keep index order,
    # oldest entry first, the order the single ranked read gave them.
    page = f"SELECT entry FROM ({sql}) ORDER BY score, entry LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db.transaction(conn):  # one snapshot for the page and its text
        entries = [row["entry"] for row in conn.execute(page, params)]
        if not entries:
            return []
        # Only the page's entries are read again for their text and snippet: one
        # walk of the matches between the page's lowest and highest entry, inside
        # the window. Naming the entries as a key list instead (`rowid IN (...)`)
        # makes FTS5 search the whole match list once per entry.
        placeholders = ",".join("?" for _ in entries)
        rows = conn.execute(
            "SELECT rowid AS entry, kind, source_id, title, "
            "snippet(search_index, 3, '[', ']', '…', 10) AS snippet "
            "FROM search_index WHERE search_index MATCH ? "
            f"AND rowid >= ? AND rowid <= ? AND +rowid IN ({placeholders})",
            [match, min(entries), max(entries), *entries],
        ).fetchall()
    found = {row["entry"]: row for row in rows}
    hits = [
        {
            "kind": found[entry]["kind"],
            "source_id": found[entry]["source_id"],
            "title": found[entry]["title"],
            "snippet": found[entry]["snippet"],
        }
        for entry in entries
    ]
    return _enrich(conn, hits)
