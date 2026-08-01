"""The search REST API.

Search spans issues, pages, their comments, and Room coordination events, so like
users it is core/, not a feature module. The data-access layer in core/search.py
runs the FTS5 query; this boundary just validates params and shapes the response.

Search requires an authenticated actor: it returns titles and body snippets from
multiple modules at once, so it is a privileged cross-cutting read. Source and
Room visibility predicates still filter every hit for the current actor before the
response is projected.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from athena.core import search
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/search", tags=["core"])


class SearchHit(BaseModel):
    kind: str
    source_id: int
    title: str
    snippet: str
    # Per-kind context (null for the other kind): an issue carries its project key
    # (ATH-12, or null for a backlog issue) and status; a page carries its space key.
    # A comment hit ('issue_comment'/'page_comment') borrows its parent's title and
    # context, and names the parent it lives on so the client can link there. A Room
    # event carries the stable coordinates needed to navigate back to its Room.
    key: str | None = None
    status: str | None = None
    space_key: str | None = None
    parent_kind: str | None = None
    parent_id: int | None = None
    room_id: int | None = None
    room_slug: str | None = None
    project_id: int | None = None
    event_kind: str | None = None


@router.get("", response_model=list[SearchHit])
def query(
    q: str = Query(..., description="free text; prefix-matched, terms AND together"),
    kind: str | None = Query(
        None,
        description="narrow to issue, page, comment, or room_event kind",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(
        0,
        ge=0,
        le=search.MAX_OFFSET,
        description="skip this many ranked hits (paging)",
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # An empty/whitespace q legitimately returns [] (the search layer handles it);
    # no need to 422 — a blank search box is "no results", not an error. The actor
    # gates the hits: private project, space, and Room content never surfaces to
    # someone who cannot see it (admins see all).
    return search.search(conn, q, kind=kind, limit=limit, offset=offset, actor=actor)
