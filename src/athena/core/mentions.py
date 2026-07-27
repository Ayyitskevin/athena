"""Unlinked mentions — the graph's missing edges, proposed but never taken.

A wiki's link graph is only as good as the discipline of the people writing it.
Someone types "see the Fleet operating guide" in an issue and moves on; the page
they meant never learns it was referenced. Obsidian's answer is *unlinked
mentions*: text that names a thing without linking to it. This module is
Athena's.

It is deliberately a READ. Finding a mention proposes an edge; it never creates
one. The "link it" affordance edits the source through the ordinary page/issue
command, so the edge arrives attributed, versioned, and on the activity trail
like any other edit. Nothing here writes, and nothing auto-links — an index that
silently rewrites bodies would make the link graph untrustworthy in exactly the
way this feature is supposed to fix.

It lives in core/ for the same reason ``links`` and ``search`` do: it is one of
the few places that deliberately knows about BOTH issues and pages. It reads
those tables directly and never imports aegis/mentor, so there is no dependency
cycle.

Two rules decide what counts as a mention, and both are choices worth stating:

  * **Code is not prose.** An occurrence inside a fenced block or an inline code
    span is NOT a mention. It is a literal — a command, a filename, a sample —
    and linkifying it would corrupt the literal while producing a link that has
    no business inside a code block. Prose in a blockquote, by contrast, IS a
    mention: a quote is still someone talking about the thing.
  * **An attempted link is not a mention.** Text already inside a ``[[...]]``
    token is skipped even when it produced no row in ``links`` (an ambiguous
    title resolves to nothing). The author already tried; offering to "link it"
    would splice ``[[`` inside ``[[`` and produce nonsense.
"""

from __future__ import annotations

import re
import sqlite3

from athena.core import search

# How many candidate hits to pull from FTS before confirming them in Python. The
# confirm pass drops candidates (a prefix-token match is not a phrase match), so
# this is deliberately wider than the returned window.
_CANDIDATE_POOL = 60

# Default and ceiling for the returned suggestions. A mention list is a nudge,
# not an inbox: a bounded, scannable set beats an exhaustive one nobody reads.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

# The kinds a mention can be found IN. Comments are searchable (they are in the
# FTS index) but are not mention sources: a comment has no body the "link it"
# edit could rewrite through a page/issue command, so suggesting one would be an
# offer Athena cannot honor.
_SOURCE_KINDS = ("issue", "page")

# A fenced code block: ``` or ~~~ to the matching fence (or end of text, for an
# unterminated fence — an unclosed fence swallows the rest, exactly as a Markdown
# renderer treats it, so the two agree on what is code).
_FENCE_RE = re.compile(r"(?ms)^(?P<f>```|~~~).*?(?:^(?P=f)[^\S\n]*$|\Z)")

# An inline code span: one or more backticks, the shortest run to a matching
# closer on the same line.
_INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")

# An existing reference token in any of its forms. The inner grammar is broad on
# purpose: this masks the AUTHOR'S INTENT to link, whether or not that intent
# resolved to a row in `links`.
_REF_TOKEN_RE = re.compile(r"\[\[[^\[\]\n]*\]\]")

# A Markdown link's target — `](...)`. A title appearing inside a URL is not
# prose either, and rewriting it would break the link.
_LINK_TARGET_RE = re.compile(r"\]\([^)\n]*\)")


def _mask(text: str) -> str:
    """Blank out every region where an occurrence is not a mention, preserving
    length and line structure so offsets into the masked text address the same
    characters in the original.

    Masking rather than deleting is what lets the caller find an occurrence in
    the masked text and then act on the ORIGINAL string at the same offset —
    the property ``linkify_first`` relies on to rewrite exactly the occurrence
    the suggestion was based on.
    """

    def blank(match: re.Match[str]) -> str:
        # Keep newlines so line numbers and block structure survive; everything
        # else becomes a space that can never match a needle.
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    masked = _FENCE_RE.sub(blank, text)
    masked = _INLINE_CODE_RE.sub(blank, masked)
    masked = _REF_TOKEN_RE.sub(blank, masked)
    return _LINK_TARGET_RE.sub(blank, masked)


def _needle_re(needle: str) -> re.Pattern[str]:
    """A case-insensitive matcher for `needle` that will not fire mid-word.

    "Fleet" must not match "Fleetwood", but a title may legitimately begin or end
    with punctuation ("Q&A", "v2.0"), so a blanket ``\\b`` is wrong: ``\\b``
    before a non-word character never matches. The boundary is therefore applied
    only on the side where the needle's own edge is a word character.
    """
    escaped = re.escape(needle)
    left = r"(?<![0-9A-Za-z_])" if needle[:1].isalnum() or needle[:1] == "_" else ""
    right = r"(?![0-9A-Za-z_])" if needle[-1:].isalnum() or needle[-1:] == "_" else ""
    return re.compile(f"{left}{escaped}{right}", re.IGNORECASE)


def find_occurrence(body: str | None, needle: str) -> int:
    """Offset of the first real mention of `needle` in `body`, or -1.

    "Real" means: outside code, outside an existing ``[[ref]]``, outside a link
    target, and not glued to surrounding word characters. The offset indexes the
    ORIGINAL body (see ``_mask``), so a caller can slice around it.
    """
    if not body or not needle:
        return -1
    match = _needle_re(needle).search(_mask(body))
    return match.start() if match else -1


def linkify_first(body: str, needle: str, token: str) -> str | None:
    """Replace the first real mention of `needle` with `token`, or None if there
    is none left to replace.

    Returning None is the honest answer to a race: the suggestion the operator
    clicked was computed from a body that has since changed, and the mention it
    named is gone. The caller turns that into a refusal rather than editing
    something the operator did not see.
    """
    at = find_occurrence(body, needle)
    if at < 0:
        return None
    return body[:at] + token + body[at + len(needle) :]


def _linked_sources(
    conn: sqlite3.Connection, target_kind: str, target_id: int
) -> set[tuple[str, int]]:
    """Every (kind, id) that ALREADY links to this target — the set a mention
    must not be in, because a linked mention is just a link."""
    rows = conn.execute(
        "SELECT source_kind, source_id FROM links "
        "WHERE target_kind = ? AND target_id = ?",
        (target_kind, target_id),
    ).fetchall()
    return {(r["source_kind"], r["source_id"]) for r in rows}


def _bodies(
    conn: sqlite3.Connection, kind: str, ids: list[int]
) -> dict[int, str | None]:
    """Bodies for a batch of sources, in ONE query per kind. The table name comes
    from a fixed literal map, never caller input."""
    if not ids:
        return {}
    table = {"issue": "issues", "page": "pages"}[kind]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, body FROM {table} WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {r["id"]: r["body"] for r in rows}


def mention_text(conn: sqlite3.Connection, kind: str, target_id: int) -> str | None:
    """The text that counts as naming this target: a page's title, or an issue's
    key (ATH-12).

    An issue is named by its KEY, not its title — issue titles are sentences
    ("Fix the login redirect") that recur in prose constantly, so title matching
    would bury the operator in false positives. A backlog issue has no key and
    therefore no mention text; it returns None and the surface says so rather
    than showing an empty list that looks like "no mentions".
    """
    if kind == "page":
        row = conn.execute(
            "SELECT title FROM pages WHERE id = ?", (target_id,)
        ).fetchone()
        return row["title"] if row else None
    if kind == "issue":
        row = conn.execute(
            "SELECT i.project_seq, p.key FROM issues i "
            "LEFT JOIN projects p ON p.id = i.project_id WHERE i.id = ?",
            (target_id,),
        ).fetchone()
        if row and row["key"] and row["project_seq"] is not None:
            return f"{row['key']}-{row['project_seq']}"
        return None
    return None


def link_token(conn: sqlite3.Connection, kind: str, target_id: int) -> str:
    """The reference token that links to this target, in the form an author would
    have written by hand: ``[[ATH-12]]`` for a keyed issue, ``[[Title]]`` for a
    page, falling back to the always-valid numeric ``[[kind:id]]``.

    Preferring the human form matters because the token is written into someone's
    prose: an edit that turns "see the Fleet operating guide" into "see the
    [[Fleet operating guide]]" reads as the author's own sentence, while
    ``[[page:41]]`` reads as machinery.
    """
    text = mention_text(conn, kind, target_id)
    if kind == "page" and text:
        # Only safe when the title round-trips: resolve_title_ref refuses an
        # ambiguous title, so a duplicated title must use the numeric form or the
        # "link" would silently record no link at all.
        rows = conn.execute(
            "SELECT id FROM pages WHERE title = ? COLLATE NOCASE "
            "AND archived_at IS NULL",
            (text,),
        ).fetchall()
        if len(rows) == 1:
            return f"[[{text}]]"
    elif kind == "issue" and text:
        return f"[[{text}]]"
    return f"[[{kind}:{target_id}]]"


def unlinked_mentions(
    conn: sqlite3.Connection,
    *,
    kind: str,
    target_id: int,
    actor: dict | None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Sources whose text names this target without linking to it.

    Returns ``{"needle", "token", "items", "shown", "total", "truncated"}``.
    ``needle`` is what was searched for (None when the target has no mention
    text — a backlog issue); ``items`` each carry the source's kind, id, title,
    and a short excerpt showing the mention in context.

    Visibility is the VIEWER'S: candidates come from ``search.search`` with the
    actor applied, so a private project's issue mentioning a public page never
    surfaces to someone who cannot see that issue. This matters more here than in
    an ordinary list, because a mention suggestion would otherwise leak the
    existence and wording of private work through the back door of a public
    page's sidebar.

    The FTS pass NARROWS; Python CONFIRMS. ``search`` matches prefix tokens ANDed
    together, so "Fleet operating guide" also hits a doc containing those three
    words scattered across paragraphs. Every candidate is therefore re-checked
    against the real body for a genuine, non-code, non-already-linked occurrence.
    Returning an unconfirmed FTS hit would be inventing a mention.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    needle = mention_text(conn, kind, target_id)
    empty: dict = {
        "needle": needle,
        "token": None,
        "items": [],
        "shown": 0,
        "total": 0,
        "truncated": False,
    }
    if not needle:
        return empty

    already = _linked_sources(conn, kind, target_id)
    hits = search.search(
        conn, needle, limit=_CANDIDATE_POOL, actor=actor, include_archived=False
    )
    # Group surviving candidates by kind so bodies are fetched in one query each
    # rather than one per hit.
    candidates: dict[str, list[dict]] = {k: [] for k in _SOURCE_KINDS}
    for hit in hits:
        hit_kind = hit["kind"]
        if hit_kind not in _SOURCE_KINDS:
            continue  # comments are searchable but are not mention sources
        source = (hit_kind, hit["source_id"])
        if source == (kind, target_id):
            continue  # a thing does not mention itself
        if source in already:
            continue  # a linked mention is a link
        candidates[hit_kind].append(hit)

    bodies = {
        k: _bodies(conn, k, [h["source_id"] for h in candidates[k]])
        for k in _SOURCE_KINDS
    }

    confirmed: list[dict] = []
    for hit in hits:
        hit_kind = hit["kind"]
        if hit_kind not in _SOURCE_KINDS:
            continue
        if hit not in candidates[hit_kind]:
            continue
        body = bodies[hit_kind].get(hit["source_id"])
        at = find_occurrence(body, needle)
        if at < 0:
            continue  # an FTS candidate that is not a real mention
        assert body is not None
        confirmed.append(
            {
                "kind": hit_kind,
                "id": hit["source_id"],
                "title": hit.get("title") or "",
                "key": hit.get("key"),
                "space_key": hit.get("space_key"),
                "excerpt": _excerpt(body, at, len(needle)),
            }
        )

    total = len(confirmed)
    return {
        "needle": needle,
        "token": link_token(conn, kind, target_id),
        "items": confirmed[:limit],
        "shown": min(total, limit),
        "total": total,
        "truncated": total > limit,
    }


def _excerpt(body: str, at: int, length: int, *, window: int = 60) -> str:
    """A one-line excerpt centred on the mention, ellipsized at both ends.

    The excerpt exists so the operator can judge the suggestion without opening
    the source — the whole point of a suggestion list. Newlines are collapsed so
    one mention is one line.
    """
    start = max(0, at - window)
    end = min(len(body), at + length + window)
    piece = " ".join(body[start:end].split())
    if start > 0:
        piece = "…" + piece
    if end < len(body):
        piece = piece + "…"
    return piece
