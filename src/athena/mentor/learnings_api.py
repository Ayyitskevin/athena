"""The run-learning REST API — promoting a run outcome into durable memory.

**Why this route lives in Mentor while its path names an issue.** The durable
write is a *page* write, so the command belongs to Mentor. The resource an
operator thinks in is the issue they were working on, so the path says
`/issues/{issue_id}/learnings`. Athena's import contract makes Aegis and Mentor
peers that may not import each other, so a route that writes a page cannot sit in
`aegis/api.py`; it sits here and reaches the issue through `core.links` and
`core.access`, which already own cross-kind resolution and visibility.

Promotion is always explicit. Nothing here fires on completion, on yield, or on
any event: a human or an agent asks for a specific summary to be remembered, and
that is the only way anything reaches the knowledge base.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from athena.core import access
from athena.core.ids import RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import docs_write_actor
from athena.mentor import run_learnings

router = APIRouter(tags=["mentor"])


class LearningIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # What was learned, in the recorder's own words. Stored quoted, never executed,
    # never interpreted as an instruction.
    summary: str = Field(min_length=1, max_length=run_learnings.MAX_SUMMARY_CHARS)
    # The run this came out of. Verified to exist and to be visible to you — an
    # unverifiable provenance claim is refused rather than recorded.
    run_id: str | None = None
    # Required only when this issue has no runbook yet: it says where to start one.
    space_id: int | None = None


class LearningOut(BaseModel):
    page_id: int
    page_title: str
    space_id: int
    # True when this promotion started the issue's runbook rather than appending.
    created: bool


@router.post(
    "/issues/{issue_id}/learnings", response_model=LearningOut, status_code=201
)
def record_learning(
    issue_id: RowIdPath,
    payload: LearningIn,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Append what a run learned to the issue's runbook page.

    The appended block references the issue, so the link index, the issue's
    backlinks, and the next agent's work-context packet all pick it up without any
    further wiring — that is what closes the loop.

    Requires the Mentor write scope (it writes a page) and visibility of the issue.
    201 because the result is a new entry, and possibly a new page."""
    try:
        result = run_learnings.record_learning(
            conn,
            actor=actor,
            issue_id=issue_id,
            summary=payload.summary,
            run_id=payload.run_id,
            space_id=payload.space_id,
        )
    except run_learnings.LearningError as exc:
        detail: object = exc.detail
        if exc.extra:
            detail = {"error": exc.detail, **exc.extra}
        raise HTTPException(
            status_code=run_learnings.STATUS_BY_KIND[exc.kind], detail=detail
        ) from exc
    page = result["page"]
    return {
        "page_id": int(page["id"]),
        "page_title": page["title"],
        "space_id": int(page["space_id"]),
        "created": result["created"],
    }


@router.get("/issues/{issue_id}/runbook", response_model=LearningOut | None)
def read_runbook(
    issue_id: RowIdPath,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | None:
    """The issue's runbook page, or null when it has none yet — so a client can
    tell whether `space_id` is needed before promoting."""
    if not access.can_see_issue(conn, actor, issue_id):
        raise HTTPException(status_code=404, detail="no such issue")
    page = run_learnings.get_runbook(conn, issue_id)
    if page is None or not access.can_see_page(conn, actor, int(page["id"])):
        return None
    return {
        "page_id": int(page["id"]),
        "page_title": page["title"],
        "space_id": int(page["space_id"]),
        "created": False,
    }
