"""Similar open issues for a new title, built on the existing full-text index.

A prompt to link instead of duplicating, not a gate: the caller still creates the issue. The
candidates are the OPEN, unarchived issues of the SAME project (or the backlog, for an issue with
no project), ranked by the search index's title match and then kept only when enough of the new
title's significant words appear in theirs. The index call carries the actor, so an issue the
actor may not see is never named. Nothing here writes, and there is no table of its own.
"""

from __future__ import annotations

import re
import sqlite3

from athena.aegis import issues, statuses
from athena.core import search

MAX_SIMILAR = 3
# Candidate cap: the newest open issues of the project. A bound, not a ranking.
MAX_CANDIDATES = 500
MIN_SHARED_WORDS = 2
MIN_SHARED_FRACTION = 0.5
_STEM = 5  # compare the first five letters, so "crash" meets "crashes"
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "the and for with when not this that from into are was has have but you "
    "can its our out too any all new add get use".split()
)


def _words(title: str) -> list[str]:
    """Significant words of a title: lower case, three or more characters, no filler."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(title.lower()):
        if len(word) >= 3 and word not in _STOPWORDS:
            seen.setdefault(word, None)
    return list(seen)


def similar_open(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    project_id: int | None,
    title: str,
    exclude_id: int | None = None,
) -> list[dict]:
    words = _words(title)
    if len(words) < MIN_SHARED_WORDS:
        return []
    category = statuses.category_sql(
        status_column="i.status", category_column="ps.category"
    )
    candidates = [
        row["id"]
        for row in conn.execute(
            "SELECT i.id FROM issues i "
            "LEFT JOIN project_statuses ps "
            "ON ps.project_id = i.project_id AND ps.name = i.status "
            "WHERE i.archived_at IS NULL AND i.project_id IS ? AND i.id IS NOT ? "
            f"AND {category} != 'done' ORDER BY i.id DESC LIMIT ?",
            (project_id, exclude_id, MAX_CANDIDATES),
        )
    ]
    if not candidates:
        return []
    stems = {word[:_STEM] for word in words}
    hits = search.search(
        conn,
        " ".join(words),
        kind="issue",
        ids=candidates,
        actor=actor,
        limit=50,
        title_any=True,
    )
    similar: list[dict] = []
    for hit in hits:
        shared = stems & {word[:_STEM] for word in _words(hit["title"])}
        if (
            len(shared) < MIN_SHARED_WORDS
            or len(shared) / len(stems) < MIN_SHARED_FRACTION
        ):
            continue
        issue = issues.get_issue(conn, hit["source_id"])
        if issue is None:
            continue
        similar.append(
            {
                "id": issue["id"],
                "key": issue.get("key"),
                "title": issue["title"],
                "status": issue["status"],
            }
        )
        if len(similar) == MAX_SIMILAR:
            break
    return similar
