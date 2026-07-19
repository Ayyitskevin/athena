"""The search REST API.

Search spans both modules (issues and pages), so like users it is core/, not a
feature module. The data-access layer in core/search.py runs the FTS5 query; this
boundary just validates query params and shapes the response.

Search requires an authenticated actor: it returns titles and body snippets from
every issue and page at once, so it is a privileged cross-cutting read — the same
bar as listing users. (Single-org tool: there is no per-owner scoping to apply;
auth is the gate, not row filtering.)
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
    # context, and names the parent it lives on so the client can link there.
    key: str | None = None
    status: str | None = None
    space_key: str | None = None
    parent_kind: str | None = None
    parent_id: int | None = None


@router.get("", response_model=list[SearchHit])
def query(
    q: str = Query(..., description="free text; prefix-matched, terms AND together"),
    kind: str | None = Query(None, description="narrow to 'issue' or 'page'"),
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
    # gates the hits: a private project's issues / a private space's pages never
    # surface to someone who can't see them (admins see all).
    return search.search(conn, q, kind=kind, limit=limit, offset=offset, actor=actor)
