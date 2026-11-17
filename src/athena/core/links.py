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

from collections.abc import Iterable, Mapping
import json
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

# How many digits an id reference may carry. A SQLite rowid tops out at
# 9223372036854775807 — 19 digits — so this admits every id that can exist and
# refuses only runs that never addressed anything.
#
# The bound is load-bearing, not tidiness. Python caps int(str) at 4300 digits and
# raises ValueError past it, so an UNBOUNDED `\d+` here made a body containing
# `[[issue:<4301 digits>]]` crash extract_refs — and extract_refs runs inside
# sync_links, on every issue and page write. A 500 on a write path, from text any
# authenticated author can type. Bounding the grammar fixes it at the source: an
# over-long run no longer matches at all, so it stays literal text like any other
# non-reference, and no caller has to defend against a number it was handed.
ID_DIGITS = r"\d{1,19}"

# Matches [[issue:42]] / [[page:7]] — a known kind, a colon, a positive integer.
# Anything else (unknown kind, non-numeric id) is just left as literal text.
# Public: the web inline renderer reuses this exact grammar so what gets indexed
# and what gets turned into a link never diverge on "what counts as a reference".
REF_RE = re.compile(rf"\[\[(issue|page):({ID_DIGITS})\]\]")

# Matches the Jira-style issue key form [[ATH-12]] — a project key prefix (leading
# letter, then letters/digits) a dash, and the per-project number. Disjoint from
# REF_RE (colon vs dash), so the two grammars never overlap. A key ref is sugar
# for "the issue with this number in this project"; it resolves to a concrete
# issue id at index time and is then stored as an ordinary ('issue', id) link, so
# the links table and backlinks stay numeric and stable.
#
# `*+` (possessive, Python ≥3.11) on purpose, here and in _RESERVED_INNER_RE:
# `-` is not in [A-Za-z0-9], so handing characters back can never turn a failed
# match into a success — the possessive form matches the IDENTICAL language with
# zero backtracking. The greedy form was an ambiguous quantified term (its first
# char class overlaps the starred one), the shape CodeQL's polynomial-ReDoS
# analysis flags on body-derived text regardless of anchoring; on "[[AAAA…"
# it re-walked the run once per failed attempt.
KEY_REF_RE = re.compile(rf"\[\[([A-Za-z][A-Za-z0-9]*+)-({ID_DIGITS})\]\]")

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
# Possessive `*+` for the same reason as KEY_REF_RE above: identical language,
# no backtracking term for a scanner to flag — fullmatch already made this call
# linear in practice, but CodeQL's query does not model fullmatch anchoring, and
# a regex that is unambiguous BY CONSTRUCTION beats one that is fast by usage.
_RESERVED_INNER_RE = re.compile(
    rf"(?:issue|page|user):{ID_DIGITS}|[A-Za-z][A-Za-z0-9]*+-{ID_DIGITS}"
)


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


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def title_key(inner: str) -> str:
    """The existing title resolver trims whitespace and uses SQLite ASCII NOCASE."""
    return inner.strip().translate(_ASCII_LOWER)


def title_keys(body: str | None) -> set[str]:
    """Distinct authored bare titles, excluding tokens handled by another resolver."""
    return {
        title_key(inner)
        for inner in TITLE_REF_RE.findall(body or "")
        if not _is_reserved_ref(inner) and inner.strip()
    }


def legacy_title_bindings(
    body: str | None,
    page_ids: Iterable[int],
    *,
    titles_by_id: Mapping[int, Iterable[str]] | None = None,
) -> dict[str, int | None]:
    """Recover only associations forced by the old graph and authored tokens.

    A page edge already explained by a numeric token proves nothing about bare
    titles. Every remaining edge, however, needed a bare title to create it.
    If exactly one authored spelling could have produced an edge, that spelling
    identifies its target. Missing name history only permits a singleton proof.
    """
    keys = title_keys(body)
    result: dict[str, int | None] = dict.fromkeys(keys)
    explicit = {target for kind, target in extract_refs(body) if kind == "page"}
    unexplained = set(page_ids) - explicit
    if not keys or not unexplained:
        return result
    if len(keys) == len(unexplained) == 1:
        return dict.fromkeys(keys, next(iter(unexplained)))
    if len(unexplained) > len(keys):
        return result
    claims: dict[str, list[int]] = {}
    for target in unexplained:
        names = (titles_by_id or {}).get(target, ())
        candidates = keys.intersection(title_key(name) for name in names)
        if len(candidates) == 1:
            claims.setdefault(candidates.pop(), []).append(target)
    for key, candidates in claims.items():
        if len(candidates) == 1:
            result[key] = candidates[0]
    return result


def stored_title_bindings(
    conn: sqlite3.Connection, source_kind: str, source_id: int
) -> dict[str, int | None]:
    """Read assigned or deliberately withheld identities without writing."""
    source = (source_kind, source_id)
    snapshot = conn.execute(
        "SELECT body, page_edges FROM legacy_title_link_sources "
        "WHERE source_kind = ? AND source_id = ?",
        source,
    ).fetchone()
    if snapshot is not None:
        history = {
            edge["id"]: edge["titles"] for edge in json.loads(snapshot["page_edges"])
        }
        return legacy_title_bindings(snapshot["body"], history, titles_by_id=history)
    return dict(
        conn.execute(
            "SELECT title_key, target_id FROM title_link_bindings "
            "WHERE source_kind = ? AND source_id = ?",
            source,
        )
    )


def source_title_targets(
    conn: sqlite3.Connection, source_kind: str, source_id: int
) -> dict[str, int | None]:
    """Every saved token, including repeatably unresolved or withheld titles."""
    source = conn.execute(
        f"SELECT body FROM {_TABLE[source_kind]} WHERE id = ?",
        (source_id,),
    ).fetchone()
    keys = title_keys(source["body"]) if source else set()
    if not keys:
        return {}
    bindings = stored_title_bindings(conn, source_kind, source_id)
    return {key: bindings.get(key) for key in keys}


def restore_title_bindings(
    conn: sqlite3.Connection,
    source_kind: str,
    source_id: int,
    bindings: Mapping[str, int | None],
) -> None:
    """Replace one source's proven and withheld bindings in the caller's transaction."""
    source = (source_kind, source_id)
    conn.execute(
        "DELETE FROM legacy_title_link_sources WHERE source_kind = ? AND source_id = ?",
        source,
    )
    conn.execute(
        "DELETE FROM title_link_bindings WHERE source_kind = ? AND source_id = ?",
        source,
    )
    conn.executemany(
        "INSERT INTO title_link_bindings (source_kind, source_id, title_key, target_id) "
        "VALUES (?, ?, ?, ?)",
        [(source_kind, source_id, key, target) for key, target in bindings.items()],
    )


def sync_links(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: int,
    body: str | None,
    commit: bool = True,
    new_source: bool = False,
) -> None:
    """Make the `links` rows for one source match its current body exactly.

    Called from the data-access layer after an issue/page is created or edited.
    The body supplies the tokens and each retained title binding supplies its
    original page ID. Both the bindings and outgoing index are reconciled
    together. A self-reference (a thing linking to itself) is dropped as
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
    keys = title_keys(body)
    if keys or not new_source:
        existing = (
            {} if new_source else stored_title_bindings(conn, source_kind, source_id)
        )
        retained = {key: existing[key] for key in keys.intersection(existing)}
        fresh = keys.difference(existing)
        space_id = None
        if fresh and source_kind == "page":
            row = conn.execute(
                "SELECT space_id FROM pages WHERE id = ?", (source_id,)
            ).fetchone()
            if row is not None:
                space_id = row["space_id"]
        for key in fresh:
            resolved = resolve_title_ref(conn, key, space_id=space_id)
            if resolved["exists"]:
                retained[key] = resolved["id"]
        restore_title_bindings(conn, source_kind, source_id, retained)
        targets.extend(
            ("page", page_id) for page_id in retained.values() if page_id is not None
        )
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
