"""Unified full-text search over issues and pages — the second one-roof payoff.

Because Aegis and Mentor share one database, a single FTS5 index (`search_index`,
migration 0013) can rank issue and page hits together. This module:

  * keeps that derived index current (index_document, called from the issue/page
    data-access layer on every write, exactly like core/links.sync_links), and
  * answers a query, ranked best-first across both kinds (search).

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

# The searchable kinds and the table each lives in. Both tables expose `title`
# and `body`. Values are fixed literals, never caller input, so building a query
# string from them is safe.
_SOURCE = {"issue": "issues", "page": "pages"}


def index_document(conn: sqlite3.Connection, *, kind: str, source_id: int) -> None:
    """Make the search_index entry for one source match its live row exactly.

    Called from the data-access layer after an issue/page is created or edited.
    We REPLACE (delete-then-insert) rather than diff: the source row is the single
    source of truth, so re-deriving from it is simplest and always correct — and,
    crucially, re-reading the WHOLE row means a title-only or body-only edit still
    indexes the full current text, not a stale half. If the row is gone (defensive
    — there is no delete path today), the entry is simply removed. Commits, since
    the data-access callers commit their own write too."""
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


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Best-first hits for `query` across issues and pages.

    Each hit is {kind, source_id, title, snippet}: the snippet is a short body
    excerpt with the matched terms wrapped in [..] for highlighting. Ranking is
    bm25 with the title column weighted above the body, so a title match outranks
    a body-only match. An empty/whitespace query returns [] (a search box with no
    input shows nothing, it does not dump the table). `kind` optionally narrows to
    one side ('issue' | 'page'); an unknown kind simply matches nothing."""
    if not query or not query.strip():
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
    # bm25() column order is (kind, source_id, title, body); weight title 2x body.
    sql += "ORDER BY bm25(search_index, 0.0, 0.0, 2.0, 1.0) LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
