"""Workspace search — one ask, everything you may see.

An agent should not have to know which module owns what. `GET /search` already
spans issues, pages, and both comment kinds, but it answers in one globally
ranked list and it does not speak the work query grammar: an agent that knows
`is:open label:infra` has to switch surfaces to use it.

This composes the two searches that already exist and invents no third:

  * **issues** come from the work query grammar when the query contains grammar
    atoms (`is:`, `label:`, `project:`, …), and from full-text search when it is
    plain words. One parser decides which — `core.work_query.parse`, the same one
    the issue list uses — so there is no second notion of "grammar-shaped";
  * **pages** and **comments** come from full-text search over the query's FREE
    TEXT only. `is:open` is a work concept; feeding it to FTS would match the
    literal token and produce confident nonsense;
  * results are **grouped by kind, not globally ranked**. Two engines with two
    orders cannot be interleaved into one honest ranking, so the response says
    they are grouped rather than inventing a cross-source score.

It lives in `workflows/` because it reads Aegis (the grammar) and core (FTS)
together, and neither module may import the other.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from athena.aegis import issue_query
from athena.core import access, search, work_query

#: Response contract marker — bumped when the shape changes, so a client can
#: refuse a payload it does not understand instead of guessing.
SCHEMA = "athena.workspace_search.v1"

MIN_LIMIT_PER_KIND = 1
MAX_LIMIT_PER_KIND = 25
DEFAULT_LIMIT_PER_KIND = 10

#: The two FTS kinds folded into the `comments` group. Discussion is searchable
#: alongside what it annotates, but a comment has no address of its own — each hit
#: names the issue or page it hangs off.
_COMMENT_KINDS = ("issue_comment", "page_comment")


class WorkspaceSearchError(Exception):
    """Transport-neutral refusal. ``kind`` is the dialect ('invalid'); ``atom``
    names the offending grammar piece when the grammar is what refused, so a
    caller can fix the typo instead of guessing which term was wrong."""

    def __init__(self, kind: str, message: str, *, atom: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.atom = atom


def _group(items: list[dict], limit: int) -> dict[str, Any]:
    """Clip to the bound and SAY whether anything was dropped. The caller fetches
    limit+1 rows, so `clipped` is a fact about this read, not a guess."""
    clipped = len(items) > limit
    return {
        "items": items[:limit],
        "returned": min(len(items), limit),
        "clipped": clipped,
    }


def _issue_item(row: dict, *, snippet: str | None) -> dict[str, Any]:
    """One shape for an issue hit whichever engine found it. ``snippet`` is null
    on the grammar path: a structural match has no text excerpt to show, and an
    empty string would read like "matched nothing"."""
    return {
        "id": row["id"],
        "key": row.get("key"),
        "title": row["title"],
        "status": row.get("status"),
        "snippet": snippet,
    }


def search_workspace(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    q: str,
    limit_per_kind: int = DEFAULT_LIMIT_PER_KIND,
) -> dict[str, Any]:
    """Search issues, pages, and comments in one call, with the caller's visibility.

    Raises :class:`WorkspaceSearchError` with kind 'invalid' for an empty query, a
    limit outside 1..25, or a query the grammar refuses — an unknown atom is an
    error, never an empty result set, because silently returning nothing to
    `labl:infra` teaches an agent the label does not exist.
    """
    if not (MIN_LIMIT_PER_KIND <= limit_per_kind <= MAX_LIMIT_PER_KIND):
        raise WorkspaceSearchError(
            "invalid",
            f"limit_per_kind must be between {MIN_LIMIT_PER_KIND} "
            f"and {MAX_LIMIT_PER_KIND}",
        )
    try:
        parsed = work_query.parse(q)
    except work_query.QueryError as exc:
        raise WorkspaceSearchError("invalid", str(exc), atom=exc.atom) from exc
    if not parsed.terms and not parsed.text.strip():
        raise WorkspaceSearchError("invalid", "q must not be empty")

    text = parsed.text.strip()
    over = limit_per_kind + 1  # one extra row is how `clipped` becomes a fact

    if parsed.terms:
        # Grammar-shaped: the issue module answers about issues, including any
        # free text (it applies it as its ordinary substring match).
        try:
            rows = issue_query.run_query(
                conn,
                parsed,
                actor=actor,
                visible_project_ids=access.visible_project_filter(conn, actor),
                limit=over,
            )
        except issue_query.QueryCompileError as exc:
            raise WorkspaceSearchError("invalid", str(exc), atom=exc.atom) from exc
        issues_group = _group(
            [_issue_item(r, snippet=None) for r in rows], limit_per_kind
        )
        issues_source = "query"
    else:
        hits = search.search(conn, text, kind="issue", limit=over, actor=actor)
        issues_group = _group(
            [
                _issue_item(
                    {
                        "id": h["source_id"],
                        "key": h.get("key"),
                        "title": h["title"],
                        "status": h.get("status"),
                    },
                    snippet=h["snippet"],
                )
                for h in hits
            ],
            limit_per_kind,
        )
        issues_source = "text"

    page_hits = (
        search.search(conn, text, kind="page", limit=over, actor=actor) if text else []
    )
    pages_group = _group(
        [
            {
                "id": h["source_id"],
                "title": h["title"],
                "snippet": h["snippet"],
                "space_key": h.get("space_key"),
            }
            for h in page_hits
        ],
        limit_per_kind,
    )

    comment_hits: list[dict] = []
    if text:
        for kind in _COMMENT_KINDS:
            comment_hits.extend(
                search.search(conn, text, kind=kind, limit=over, actor=actor)
            )
    comments_group = _group(
        [
            {
                "id": h["source_id"],
                "title": h["title"],
                "snippet": h["snippet"],
                "parent_kind": h.get("parent_kind"),
                "parent_id": h.get("parent_id"),
            }
            for h in comment_hits
        ],
        limit_per_kind,
    )

    return {
        "schema": SCHEMA,
        "query": {
            "raw": q,
            # Exactly what went to full-text search. Empty for a pure-grammar
            # query, which is why the page/comment groups are empty there — stated
            # rather than left to look like "no matches".
            "text": text,
            "atoms": [
                f"-{t.field}:{t.value}" if t.negated else f"{t.field}:{t.value}"
                for t in parsed.terms
            ],
        },
        "issues": {**issues_group, "source": issues_source},
        "pages": pages_group,
        "comments": comments_group,
        "limit_per_kind": limit_per_kind,
        # Each source keeps its own order; there is no cross-source score.
        "grouped_by_kind": True,
    }
