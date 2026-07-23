"""Filtered issue search — full-text relevance narrowed by the structured filters.

This is the synthesis of the two search-adjacent capabilities: the FTS ranking from
core.search (best-first, prefix-matched, snippeted) and the structured issue filters
from issues.list_issues (status, priority, assignee, label, project — the SAME
dimensions the issue list and saved filters use). A result here both MATCHES the text
AND satisfies every filter.

It composes rather than reimplements: the filter set comes from list_issues (the one
issue-filtering path, so a filtered search and an ad-hoc filter agree on what
qualifies), and the ranking comes from search.search restricted to those ids (so a
filtered search and a plain search agree on ordering). Nothing new about "what
matches" is invented here — only the intersection.

It lives in aegis because it spans aegis concerns (issues, labels) plus core.search;
core may not import aegis, so the composition belongs on this side of the line.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issues
from athena.core import access, labels, search

# Sentinel: "no visibility gating" (internal callers / ranking tests). Distinct from
# actor=None, a real anonymous viewer who may see only public projects.
_UNGATED = object()


def search_issues(
    conn: sqlite3.Connection,
    query: str,
    *,
    status: str | None = None,
    priority: str | None = None,
    assignee_id: int | None = None,
    label: str | None = None,
    project: str | None = None,
    text_filter: str | None = None,
    limit: int = 20,
    offset: int = 0,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """Issues matching `query` (full-text, ranked) AND every supplied filter.

    A blank query returns [] — this is search, not browse (the unfiltered "list all
    issues" need is served by the issue list / saved filters). When no filter is set
    it is a plain issue search; when filters are set, the FTS hits are intersected
    with the issues that satisfy them, preserving FTS rank order. Returns core.search
    hits ({kind, source_id, title, snippet, key, status}).

    `actor` gates by project visibility: an issue in a project the actor can't see
    never surfaces. The default (_UNGATED) applies no gate (internal/ranking callers);
    pass the real actor (a user dict, or None for anonymous) to filter. It is threaded
    to both the structured pre-filter (list_issues) and the FTS query (search), so a
    hidden issue can't slip through either path."""
    if not query or not query.strip():
        return []
    viewer: dict | None = None
    visible_project_ids: set[int] | None = None
    if actor is not _UNGATED:
        assert actor is None or isinstance(actor, dict)
        viewer = actor
        visible_project_ids = access.visible_project_filter(conn, viewer)

    if project is not None and not isinstance(project, str):
        return []
    project_filter = project is not None and project.strip() != ""
    has_filter = (
        status is not None
        or priority is not None
        or assignee_id is not None
        or (label is not None and label.strip() != "")
        or project_filter
        or (text_filter is not None and text_filter.strip() != "")
    )
    if not has_filter:
        # No structured constraint — a plain issue-scoped search.
        if actor is _UNGATED:
            return search.search(conn, query, kind="issue", limit=limit, offset=offset)
        return search.search(
            conn,
            query,
            kind="issue",
            limit=limit,
            offset=offset,
            actor=viewer,
        )

    # Resolve the structured filter to a concrete set of issue ids via the one
    # issue-filtering path, then restrict the ranked FTS hits to that set.
    project_id: int | None = None
    backlog = False
    if project_filter:
        parsed = issues.parse_project_filter(project)
        if parsed is None:
            # HTTP rejects this at its boundary; a direct caller or malformed
            # legacy saved row must fail closed rather than omit the constraint.
            return []
        project_id, backlog = parsed
    label_ids = (
        labels.issue_ids_for_label(conn, label) if (label and label.strip()) else None
    )
    filtered = issues.list_issues(
        conn,
        status=status,
        priority=priority,
        assignee_id=assignee_id,
        project_id=project_id,
        backlog=backlog,
        search=text_filter,
        ids=label_ids,
        visible_project_ids=visible_project_ids,
    )
    allowed = [i["id"] for i in filtered]
    if not allowed:
        return []
    if actor is _UNGATED:
        return search.search(
            conn, query, kind="issue", ids=allowed, limit=limit, offset=offset
        )
    return search.search(
        conn, query, kind="issue", ids=allowed, limit=limit, offset=offset, actor=viewer
    )
