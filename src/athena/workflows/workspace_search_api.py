"""REST for workspace search — the transport for `workflows/workspace_search.py`.

It sits at the workflows layer because the read it adapts does: a route that
answered about issues *and* pages could not live in either module's API without
one of them importing the other.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from athena.core.deps import get_conn
from athena.core.identity import current_actor
from athena.workflows import workspace_search

router = APIRouter(prefix="/search", tags=["workflows"])

# The kind dialect, mapped once at the transport edge.
STATUS_BY_KIND = {"invalid": 422}


def _refusal(exc: workspace_search.WorkspaceSearchError) -> HTTPException:
    """The same 422 body `aegis/api.py::_query_refusal` returns, so a bad query
    reads identically whether it was asked of `/issues` or of here — one error
    contract for one grammar."""
    detail: dict[str, object] = {"error": str(exc), "code": "invalid_query"}
    if exc.atom is not None:
        detail["atom"] = exc.atom
    return HTTPException(status_code=STATUS_BY_KIND[exc.kind], detail=detail)


@router.get("/workspace")
def workspace(
    q: str = Query(
        ...,
        description=(
            "One ask across issues, pages, and comments. Work-query atoms work "
            "here ('is:open label:infra'); free text goes to full-text search."
        ),
    ),
    limit_per_kind: int = Query(
        workspace_search.DEFAULT_LIMIT_PER_KIND,
        ge=workspace_search.MIN_LIMIT_PER_KIND,
        le=workspace_search.MAX_LIMIT_PER_KIND,
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Authenticated, matching `GET /search`: this returns titles and snippets from
    # across the workspace at once, so it is a privileged cross-cutting read. The
    # actor also gates every group, so it can never show more than the module
    # surfaces would.
    try:
        return workspace_search.search_workspace(
            conn, actor=actor, q=q, limit_per_kind=limit_per_kind
        )
    except workspace_search.WorkspaceSearchError as exc:
        raise _refusal(exc) from exc
