"""Issue read routes for the Aegis REST API.

Attaches to the issue router in ``aegis.api``. Imported first so ``GET /search``
and the other static paths register before ``GET /{ref}``.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException, Query, Response

from athena.aegis import (
    issue_history,
    issue_narrative,
    issue_query,
    issue_search,
    issues,
)
from athena.aegis.api import (
    IssueOut,
    IssueSearchHit,
    IssueStateOut,
    LinkOut,
    QueryCountOut,
    router,
)
from athena.aegis.rest_support import issue_for_read, tagged_issue, with_labels_many
from athena.core import access, graph, labels, links, mentions, work_query
from athena.core.deps import get_conn
from athena.core.ids import RowIdPath
from athena.core.identity import optional_actor


def _parse_project_filter(project: str | None) -> tuple[int | None, bool]:
    """HTTP wrapper over the shared issues.parse_project_filter: the parsing rules
    live in one place (so the web list can't drift from us), and here we turn the
    "invalid" signal (None) into the API's 422."""
    parsed = issues.parse_project_filter(project)
    if parsed is None:
        raise HTTPException(status_code=422, detail="invalid project filter")
    return parsed


def _query_refusal(exc: Exception, atom: str | None) -> HTTPException:
    """One 422 shape for a bad query, whether the grammar or the domain refused it.

    ``atom`` names the offending piece so a caller can fix the typo instead of
    guessing which of eight terms was wrong — the whole reason an unknown atom is
    an error rather than an empty result set."""
    detail: dict[str, object] = {"error": str(exc), "code": "invalid_query"}
    if atom is not None:
        detail["atom"] = atom
    return HTTPException(status_code=422, detail=detail)


def _parsed_query(raw: str) -> work_query.Query:
    try:
        return work_query.parse(raw)
    except work_query.QueryError as exc:
        raise _query_refusal(exc, exc.atom) from exc


@router.get("", response_model=list[IssueOut])
def index(
    q: str | None = Query(
        None,
        description=(
            "Work query, e.g. 'is:open label:infra project:ATH sort:priority-desc'. "
            "Mutually exclusive with the structured filters below."
        ),
    ),
    status: str | None = None,
    priority: str | None = None,
    assignee: int | None = Query(None, ge=0, le=issues.MAX_SQLITE_INTEGER),
    label: str | None = None,
    search: str | None = None,
    project: str | None = None,
    sprint: int | None = Query(None, ge=0, le=issues.MAX_SQLITE_INTEGER),
    include_archived: bool = False,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=issues.MAX_OFFSET),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # `q` and the structured filters are two spellings of the same intent, so
    # combining them is refused rather than merged: silently AND-ing them would
    # make `?q=is:open&status=done` return nothing and look like a data problem,
    # and silently preferring one would ignore what the caller asked for.
    if q is not None:
        conflicting = [
            name
            for name, value in (
                ("status", status),
                ("priority", priority),
                ("assignee", assignee),
                ("label", label),
                ("search", search),
                ("project", project),
                ("sprint", sprint),
            )
            if value is not None
        ]
        if include_archived:
            conflicting.append("include_archived")
        if conflicting:
            raise HTTPException(
                status_code=422,
                detail=(
                    "q cannot be combined with the structured filters "
                    f"({', '.join(sorted(conflicting))}); express them in the query"
                ),
            )
        try:
            rows = issue_query.run_query(
                conn,
                _parsed_query(q),
                actor=actor,
                visible_project_ids=access.visible_project_filter(conn, actor),
                limit=limit,
                offset=offset,
            )
        except issue_query.QueryCompileError as exc:
            raise _query_refusal(exc, exc.atom) from exc
        return with_labels_many(conn, rows)
    # Optional filters, same semantics the web list uses (one shared path in
    # issues.list_issues). A label name is resolved to issue ids by labels.py so
    # issues.py stays decoupled from the join; an unknown label matches nothing.
    # project is a direct column on the issue: an id restricts to that project,
    # "none" restricts to the backlog (no project). priority/assignee are direct
    # columns too — the same dimensions a saved filter persists. sprint is a direct
    # column too: an id restricts to that sprint (an unknown id matches nothing).
    # The read is open (optional_actor → None for anonymous), but it only ever
    # returns issues in projects the caller may see (public + their private ones);
    # admins see all, the backlog is always in.
    # limit/offset are bounded (page ≤ 100, default 50), mirroring GET /issues/search
    # — an anonymous caller can't pull the whole issues table in one request. The web
    # board reaches issues.list_issues directly (uncapped) and is unaffected.
    project_id, backlog = _parse_project_filter(project)
    ids = labels.issue_ids_for_label(conn, label) if label else None
    rows = issues.list_issues(
        conn,
        status=status,
        priority=priority,
        assignee_id=assignee,
        search=search,
        project_id=project_id,
        backlog=backlog,
        sprint_id=sprint,
        include_archived=include_archived,
        ids=ids,
        visible_project_ids=access.visible_project_filter(conn, actor),
        limit=limit,
        offset=offset,
    )
    return with_labels_many(conn, rows)


@router.get("/query/count", response_model=QueryCountOut)
def query_count(
    q: str,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """How many issues this query matches, within the caller's visibility."""
    parsed = _parsed_query(q)
    try:
        matched = issue_query.count_query(
            conn,
            parsed,
            actor=actor,
            visible_project_ids=access.visible_project_filter(conn, actor),
        )
    except issue_query.QueryCompileError as exc:
        raise _query_refusal(exc, exc.atom) from exc
    return {"q": parsed.raw, "matched": matched}


@router.get("/query/help")
def query_help(_actor: dict | None = Depends(optional_actor)) -> dict:
    """The query vocabulary, as data.

    Emitted from `work_query.describe()` rather than restated, so this endpoint,
    the MCP tool's docstring, and docs/QUERY.md cannot drift from the parser.
    """
    return work_query.describe()


@router.get("/search", response_model=list[IssueSearchHit])
def search_issues_endpoint(
    q: str,
    status: str | None = None,
    priority: str | None = None,
    assignee: int | None = Query(None, ge=0, le=issues.MAX_SQLITE_INTEGER),
    label: str | None = None,
    project: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=issues.MAX_OFFSET),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Full-text issue search narrowed by the structured filters — the ranked twin of
    # GET /issues. Open like the issue list (optional_actor → None for anonymous), but
    # gated by it: an issue in a project the caller can't see never surfaces. project
    # is validated the same way the list does (422 on garbage); the rest are passed
    # through (an unknown status/label/assignee simply matches none). A blank q
    # legitimately returns [] — the issue_search layer handles it. Declared BEFORE GET
    # /{ref} so the literal path wins over the issue-ref parameter.
    if project is not None:
        _parse_project_filter(project)  # raises 422 on an unparseable filter
    return issue_search.search_issues(
        conn,
        q,
        status=status,
        priority=priority,
        assignee_id=assignee,
        label=label,
        project=project,
        limit=limit,
        offset=offset,
        actor=actor,
    )


@router.get("/{ref}", response_model=IssueOut)
def show(
    ref: str,
    response: Response,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Reads are open to everyone (optional_actor → None for anonymous), same as the
    # list endpoint — only writes pass through the creator-or-assignee gate. ref is
    # addressable two ways: the numeric id ("12") or the project key ("ATH-12"); both
    # resolve to the same issue. An issue in a private project the caller can't see is
    # a 404, indistinguishable from a missing one, so visibility never leaks via
    # existence. Backlog issues (no project) read like a public one.
    issue = issues.get_by_ref(conn, ref)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise HTTPException(status_code=404, detail="no such issue")
    return tagged_issue(conn, issue, response)


@router.get("/{issue_id}/backlinks", response_model=list[LinkOut])
def backlinks(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # "What references this issue?" — open like other reads, but gated by visibility:
    # a hidden issue 404s identically to a missing one (the lone sub-resource read that
    # used a bare existence check — a 200-vs-404 existence oracle), and the sources are
    # gated by the viewer so a hidden project's/space's reference never reveals itself.
    issue_for_read(conn, issue_id, actor)
    return links.backlinks(conn, target_kind="issue", target_id=issue_id, actor=actor)


@router.get("/{issue_id}/graph")
def issue_graph(
    issue_id: RowIdPath,
    depth: int = graph.DEFAULT_DEPTH,
    max_nodes: int = graph.DEFAULT_MAX_NODES,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The bounded neighbourhood around this issue, as positioned data rather than
    # markup — the Aegis twin of the page graph.
    issue_for_read(conn, issue_id, actor)
    return graph.ego_graph(
        conn,
        kind="issue",
        node_id=issue_id,
        actor=actor,
        depth=depth,
        max_nodes=max_nodes,
    )


@router.get("/{issue_id}/related")
def issue_related_items(
    issue_id: RowIdPath,
    limit: int = graph.DEFAULT_RELATED_LIMIT,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # "What cites what this issue cites, but is not linked to it yet?" — the
    # Aegis twin of the page route: co-citation over the same links the graph
    # walks, direct neighbours deliberately absent (they are /backlinks), the
    # bound disclosed. 404 for missing and hidden alike.
    issue_for_read(conn, issue_id, actor)
    return graph.related_items(
        conn, kind="issue", node_id=issue_id, actor=actor, limit=limit
    )


@router.get("/{issue_id}/unlinked-mentions")
def issue_unlinked_mentions(
    issue_id: RowIdPath,
    limit: int = mentions.DEFAULT_LIMIT,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Text naming this issue's key without linking to it. A read: it proposes
    # edges, never creates them.
    issue_for_read(conn, issue_id, actor)
    return mentions.unlinked_mentions(
        conn, kind="issue", target_id=issue_id, actor=actor, limit=limit
    )


@router.get("/{issue_id}/state", response_model=IssueStateOut)
def issue_state(
    issue_id: RowIdPath,
    as_of: int | None = Query(
        None,
        description="reconstruct state as of this activity event id (default: now)",
    ),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Time-travel: the issue's lifecycle state folded from its activity log as of a past
    # event. Gated like other reads — a hidden/missing issue is a 404; within a visible
    # issue its own history reads openly, like the detail page.
    issue_for_read(conn, issue_id, actor)
    try:
        state = issue_history.project_issue_state(
            conn, issue_id, as_of_event_id=as_of, actor=actor
        )
    except issue_history.IncompleteIssueHistory as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except issue_history.IssueHistoryTooLarge as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if state is None:  # issue_for_read already 404s; this is belt-and-suspenders
        raise HTTPException(status_code=404, detail="no such issue")
    return state


@router.get("/{issue_id}/history")
def issue_history_narrative(
    issue_id: RowIdPath,
    limit: int = Query(50, ge=1, le=200),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The operator narrative for one issue, as JSON. Gated like other reads — a
    # hidden/missing issue is a 404. The narrative itself preserves each owning
    # surface's visibility rules (admin sees check-ins, agents see their own
    # controls, etc.) and never infers write authority from the evidence it shows.
    issue_for_read(conn, issue_id, actor)
    narrative = issue_narrative.build_issue_narrative(
        conn, issue_id, actor=actor, limit=limit
    )
    if narrative is None:  # issue_for_read already 404s; this is belt-and-suspenders
        raise HTTPException(status_code=404, detail="no such issue")
    return narrative


@router.get("/{issue_id}/children", response_model=list[IssueOut])
def list_children(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like backlinks/comments. 404 if the issue is missing OR in a private
    # project the caller can't see. The children themselves are visibility-gated too:
    # a child can sit in a private project the caller can't see (parenting spans
    # projects), so gate the list the same way the issue list is gated — else the
    # parent's children would leak a hidden child's content.
    issue_for_read(conn, issue_id, actor)
    children = issues.list_children(
        conn, issue_id, visible_project_ids=access.visible_project_filter(conn, actor)
    )
    return with_labels_many(conn, children)
