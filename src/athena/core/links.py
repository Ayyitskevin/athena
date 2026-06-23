"""The cross-link resolver — Athena's one-roof payoff.

An issue in Aegis can reference a page in Mentor and vice-versa, because they
share one database. Authors write a reference token in a body — `[[issue:42]]`
or `[[page:7]]` — and this module:

  * extracts those tokens (extract_refs),
  * keeps a derived index of them in the `links` table (sync_links, called from
    the issue/page data-access layer on every write), and
  * answers "what does this reference?" (outgoing_links) and the marquee
    question "what references THIS?" (backlinks).

It lives in core/ because it is the one place that deliberately knows about BOTH
issues and pages (the whole point of the shared core). It does NOT import the
aegis/mentor modules — it reads the `issues`/`pages` tables directly via a fixed
internal table map — so there is no dependency cycle (aegis/mentor import core,
never the reverse).

References resolve LAZILY: a row may point at a target that doesn't exist (a typo,
or a thing created later). Existence is checked at read time, so a broken ref is
shown as broken rather than silently dropped, and a target created later lights up
its backlinks for free.
"""
from __future__ import annotations

import re
import sqlite3

# The two linkable kinds and the table each lives in. This map is the only place
# the resolver's knowledge of "what is linkable" lives; both tables happen to use
# a `title` column. Values are fixed literals, never caller input, so building a
# query string from them is safe.
_TABLE = {"issue": "issues", "page": "pages"}

# Matches [[issue:42]] / [[page:7]] — a known kind, a colon, a positive integer.
# Anything else (unknown kind, non-numeric id) is just left as literal text.
# Public: the web inline renderer reuses this exact grammar so what gets indexed
# and what gets turned into a link never diverge on "what counts as a reference".
REF_RE = re.compile(r"\[\[(issue|page):(\d+)\]\]")


def extract_refs(text: str | None) -> list[tuple[str, int]]:
    """Every distinct (kind, id) reference in a body, in first-seen order.
    Deduped so writing [[page:7]] twice records one link, not two."""
    if not text:
        return []
    out: list[tuple[str, int]] = []
    for kind, num in REF_RE.findall(text):
        ref = (kind, int(num))
        if ref not in out:
            out.append(ref)
    return out


def sync_links(
    conn: sqlite3.Connection, *, source_kind: str, source_id: int, body: str | None
) -> None:
    """Make the `links` rows for one source match its current body exactly.

    Called from the data-access layer after an issue/page is created or edited.
    We replace (delete-then-insert) rather than diff: a source owns its outgoing
    links, so the body is the single source of truth and re-deriving is simplest
    and always correct. A self-reference (a thing linking to itself) is dropped as
    noise. Commits, since the data-access callers commit their own write too."""
    refs = extract_refs(body)
    conn.execute(
        "DELETE FROM links WHERE source_kind = ? AND source_id = ?",
        (source_kind, source_id),
    )
    for target_kind, target_id in refs:
        if target_kind == source_kind and target_id == source_id:
            continue  # a self-link is noise, not a cross-reference
        conn.execute(
            "INSERT OR IGNORE INTO links "
            "(source_kind, source_id, target_kind, target_id) VALUES (?, ?, ?, ?)",
            (source_kind, source_id, target_kind, target_id),
        )
    conn.commit()


def _resolve(conn: sqlite3.Connection, kind: str, target_id: int) -> dict:
    """A reference resolved for display: its kind, id, current title, and whether
    it still exists. title is None for a broken (non-existent) reference. The
    table name comes from the fixed _TABLE map, never caller input."""
    table = _TABLE[kind]
    row = conn.execute(
        f"SELECT title FROM {table} WHERE id = ?", (target_id,)
    ).fetchone()
    return {
        "kind": kind,
        "id": target_id,
        "title": row["title"] if row else None,
        "exists": row is not None,
    }


def resolve_ref(conn: sqlite3.Connection, kind: str, target_id: int) -> dict:
    """Resolve ONE reference to {kind, id, title, exists} — the inline renderer's
    entry point, so a body's [[issue:N]] token can be turned into a live link (or
    shown broken) using the same existence check the backlink lists use. `kind`
    must be a known kind ('issue' | 'page'); the caller's regex guarantees that."""
    return _resolve(conn, kind, target_id)


def outgoing_links(
    conn: sqlite3.Connection, source_kind: str, source_id: int
) -> list[dict]:
    """What this source references, each resolved to title + existence. Ordered
    stably (kind, then id) so renders are deterministic."""
    rows = conn.execute(
        "SELECT target_kind, target_id FROM links "
        "WHERE source_kind = ? AND source_id = ? "
        "ORDER BY target_kind, target_id",
        (source_kind, source_id),
    ).fetchall()
    return [_resolve(conn, r["target_kind"], r["target_id"]) for r in rows]


def backlinks(
    conn: sqlite3.Connection, target_kind: str, target_id: int
) -> list[dict]:
    """What references this target ("Referenced by"), each resolved to title +
    existence. The marquee query — keyed by target, served by idx_links_target.
    Sources are real rows (they had to exist to be written), so these resolve
    live; the `exists` flag is still honored for robustness."""
    rows = conn.execute(
        "SELECT source_kind, source_id FROM links "
        "WHERE target_kind = ? AND target_id = ? "
        "ORDER BY source_kind, source_id",
        (target_kind, target_id),
    ).fetchall()
    return [_resolve(conn, r["source_kind"], r["source_id"]) for r in rows]
