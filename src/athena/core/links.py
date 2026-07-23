"""The cross-link resolver — Athena's one-roof payoff.

An issue in Aegis can reference a page in Mentor and vice-versa, because they
share one database. Authors write a reference token in a body — `[[issue:42]]`,
`[[page:7]]`, a Jira-style `[[ATH-12]]`, or a bare `[[Page Title]]` wiki-link —
and this module:

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

from athena.core import access

# Sentinel for the link reads' `actor`: "no visibility gating" (internal callers /
# tests). Distinct from actor=None, a real anonymous viewer who sees only public refs.
_UNGATED = object()

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

# Matches the Jira-style issue key form [[ATH-12]] — a project key prefix (leading
# letter, then letters/digits) a dash, and the per-project number. Disjoint from
# REF_RE (colon vs dash), so the two grammars never overlap. A key ref is sugar
# for "the issue with this number in this project"; it resolves to a concrete
# issue id at index time and is then stored as an ordinary ('issue', id) link, so
# the links table and backlinks stay numeric and stable.
KEY_REF_RE = re.compile(r"\[\[([A-Za-z][A-Za-z0-9]*)-(\d+)\]\]")

# Matches a bare [[Page Title]] wiki-link (Obsidian/Notion style): double brackets
# around free text with no brackets, newline, or ANGLE BRACKETS inside. This grammar is
# DELIBERATELY broad on the rest — it also matches the typed forms above — so the reserved
# forms are excluded in code (_is_reserved_ref), never by the regex. A title ref addresses
# a page by the thing an agent actually remembers (its title) instead of a numeric id;
# like a key ref it resolves to a concrete page id at index time and is stored as an
# ordinary ('page', id) link, so backlinks light up the same way [[page:N]] does.
#
# Angle brackets are excluded so the SAME regex is safe over both the raw body (indexing)
# and the Markdown-rendered, HTML-escaped body (rendering): a token like [[**bold**]]
# becomes [[<strong>bold</strong>]] once rendered, and excluding <> keeps this pass from
# swallowing that emphasis markup (it stays bold-in-brackets, its pre-title-links look).
# The one entity that still slips through is `&` (a real title like "Q&A" renders as
# "Q&amp;A"); the render pass html-unescapes the inner before lookup so it matches the
# raw title the index recorded.
TITLE_REF_RE = re.compile(r"\[\[([^\[\]\n<>]+)\]\]")

# The inner-text forms that are NOT titles — the typed/key/mention grammars, anchored
# with fullmatch so ONLY an exact match is reserved (a title that merely CONTAINS a
# colon, e.g. "Roadmap: 2026", is still a title). These are exactly the tokens the
# other resolvers own, so [[Title]] resolution never steals one of them.
_RESERVED_INNER_RE = re.compile(r"(?:issue|page|user):\d+|[A-Za-z][A-Za-z0-9]*-\d+")


def _is_reserved_ref(inner: str) -> bool:
    """Whether a [[...]] token's inner text is a typed/key/mention ref (owned by another
    resolver) rather than a page title. Matched WITHOUT stripping, so it reserves
    precisely the strings [[issue:N]]/[[page:N]]/[[user:N]]/[[KEY-N]] the other regexes
    consume — a spaced variant like `[[ page:7 ]]` (which those regexes do NOT match) is
    left to resolve as an ordinary title."""
    return _RESERVED_INNER_RE.fullmatch(inner) is not None


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


def resolve_key_ref(conn: sqlite3.Connection, key: str, seq: int) -> dict:
    """Resolve a [[KEY-N]] issue reference to {kind:'issue', id, title, exists}.

    Looks up the issue holding number N in the project whose prefix is KEY. id and
    title are None when nothing matches — an unknown project key, or a number that
    was never allocated or has since been retired (the issue moved away/was
    deleted). Reads the issues/projects tables directly (projects.key is COLLATE
    NOCASE, so the match is case-insensitive) to stay cycle-free — links.py never
    imports the aegis module."""
    row = conn.execute(
        "SELECT i.id, i.title FROM issues i "
        "JOIN projects p ON p.id = i.project_id "
        "WHERE p.key = ? AND i.project_seq = ?",
        (key, seq),
    ).fetchone()
    return {
        "kind": "issue",
        "id": row["id"] if row else None,
        "title": row["title"] if row else None,
        "exists": row is not None,
    }


def resolve_title_ref(
    conn: sqlite3.Connection, title: str, *, space_id: int | None = None
) -> dict:
    """Resolve a [[Title]] wiki-link to {kind:'page', id, title, exists}.

    A title reference names a page by its (case-insensitive) title instead of a numeric
    id — the address agents are actually good at. Resolution is deliberately UNAMBIGUOUS:
    titles are not unique, so it resolves ONLY when exactly one live page answers, and it
    prefers the SOURCE page's own space to break a tie the way a wiki would (a [[Title]]
    on a page means "the one in this space" first).

      * archived pages are excluded — a soft-deleted page is not a live target;
      * when `space_id` is given (the source is a page) AND any candidate is in that
        space, candidates are narrowed to it;
      * exactly one surviving candidate resolves; zero or several leave it UNRESOLVED
        (id/title None, exists False) so it renders broken and records no link — the
        same "don't guess" contract a broken numeric/key ref gets.

    Reads the pages table directly (like resolve_key_ref) to keep links.py free of a
    mentor import, so the core resolver never depends on a feature module. The COLLATE
    NOCASE match rides idx_pages_title (a NOCASE index) so this stays an index probe even
    though sync_links calls it on every write."""
    name = (title or "").strip()
    if not name:
        return {"kind": "page", "id": None, "title": None, "exists": False}
    rows = conn.execute(
        "SELECT id, space_id, title FROM pages "
        "WHERE title = ? COLLATE NOCASE AND archived_at IS NULL",
        (name,),
    ).fetchall()
    if space_id is not None:
        in_space = [r for r in rows if r["space_id"] == space_id]
        if (
            in_space
        ):  # prefer the source's own space; fall back to a global unique match
            rows = in_space
    if len(rows) == 1:
        row = rows[0]
        return {"kind": "page", "id": row["id"], "title": row["title"], "exists": True}
    return {"kind": "page", "id": None, "title": None, "exists": False}


def sync_links(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: int,
    body: str | None,
    commit: bool = True,
) -> None:
    """Make the `links` rows for one source match its current body exactly.

    Called from the data-access layer after an issue/page is created or edited.
    We replace (delete-then-insert) rather than diff: a source owns its outgoing
    links, so the body is the single source of truth and re-deriving is simplest
    and always correct. A self-reference (a thing linking to itself) is dropped as
    noise. ``commit=False`` lets an application command include this projection in
    the same transaction as its source row and audit event."""
    targets = extract_refs(body)  # typed [[issue:N]]/[[page:N]] refs
    # Key refs [[ATH-12]] resolve to a concrete issue id NOW and are stored as
    # ordinary ('issue', id) links. An unresolvable key ref is simply not stored —
    # it renders broken but, by design, leaves no backlink (the one documented
    # asymmetry vs [[issue:N]], which records the broken numeric id regardless).
    for key, num in KEY_REF_RE.findall(body or ""):
        resolved = resolve_key_ref(conn, key, int(num))
        if resolved["exists"] and ("issue", resolved["id"]) not in targets:
            targets.append(("issue", resolved["id"]))
    # [[Page Title]] wiki-links resolve to a concrete page id NOW (like key refs) and
    # are stored as ordinary ('page', id) links, so a title reference lights up its
    # target's backlinks exactly as [[page:N]] does. A page source disambiguates within
    # its OWN space first; an issue source has no space, so it resolves globally.
    # Reserved tokens ([[issue:N]]/[[page:N]]/[[user:N]]/[[KEY-N]]) are skipped — they
    # are owned by the resolvers above. An unresolvable or ambiguous title is simply not
    # stored (it renders broken and leaves no backlink — the key-ref asymmetry).
    source_space_id = None
    if source_kind == "page":
        row = conn.execute(
            "SELECT space_id FROM pages WHERE id = ?", (source_id,)
        ).fetchone()
        source_space_id = row["space_id"] if row else None
    for inner in TITLE_REF_RE.findall(body or ""):
        if _is_reserved_ref(inner):
            continue
        resolved = resolve_title_ref(conn, inner, space_id=source_space_id)
        if resolved["exists"] and ("page", resolved["id"]) not in targets:
            targets.append(("page", resolved["id"]))
    conn.execute(
        "DELETE FROM links WHERE source_kind = ? AND source_id = ?",
        (source_kind, source_id),
    )
    for target_kind, target_id in targets:
        if target_kind == source_kind and target_id == source_id:
            continue  # a self-link is noise, not a cross-reference
        conn.execute(
            "INSERT OR IGNORE INTO links "
            "(source_kind, source_id, target_kind, target_id) VALUES (?, ?, ?, ?)",
            (source_kind, source_id, target_kind, target_id),
        )
    if commit:
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


def _visible_ref(conn: sqlite3.Connection, actor, kind: str, ref_id: int) -> bool:
    """Whether the actor may see this (kind, id) reference — the per-ref visibility
    gate the link lists apply. _UNGATED waves everything through (internal callers)."""
    if actor is _UNGATED:
        return True
    if kind == "issue":
        return access.can_see_issue(conn, actor, ref_id)
    if kind == "page":
        return access.can_see_page(conn, actor, ref_id)
    return True


def outgoing_links(
    conn: sqlite3.Connection,
    source_kind: str,
    source_id: int,
    *,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """What this source references, each resolved to title + existence. Ordered
    stably (kind, then id) so renders are deterministic. `actor` drops references to
    targets the viewer can't see (a private project's issue / a private space's
    page); _UNGATED is no gate."""
    rows = conn.execute(
        "SELECT target_kind, target_id FROM links "
        "WHERE source_kind = ? AND source_id = ? "
        "ORDER BY target_kind, target_id",
        (source_kind, source_id),
    ).fetchall()
    return [
        _resolve(conn, r["target_kind"], r["target_id"])
        for r in rows
        if _visible_ref(conn, actor, r["target_kind"], r["target_id"])
    ]


def backlinks(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: int,
    *,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """What references this target ("Referenced by"), each resolved to title +
    existence. The marquee query — keyed by target, served by idx_links_target.
    Sources are real rows (they had to exist to be written), so these resolve
    live; the `exists` flag is still honored for robustness. `actor` drops sources
    the viewer can't see — so a private project's issue linking to a public page
    never reveals itself in that page's "Referenced by"; _UNGATED is no gate."""
    rows = conn.execute(
        "SELECT source_kind, source_id FROM links "
        "WHERE target_kind = ? AND target_id = ? "
        "ORDER BY source_kind, source_id",
        (target_kind, target_id),
    ).fetchall()
    return [
        _resolve(conn, r["source_kind"], r["source_id"])
        for r in rows
        if _visible_ref(conn, actor, r["source_kind"], r["source_id"])
    ]
