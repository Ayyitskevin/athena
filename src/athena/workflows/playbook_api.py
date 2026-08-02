"""The playbook REST surface.

The route lives here rather than in `mentor/api.py` for the same reason the
command does: it adapts a WORKFLOW that spans both modules, and `mentor` may
not import `workflows`. It keeps the `/pages/{page_id}/...` path because the
resource an operator names is still the page — the URL follows the noun, not
the package.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from athena.core.deps import get_conn
from athena.core.identity import current_actor
from athena.core.ids import MAX_SQLITE_INTEGER, RowIdPath
from athena.workflows import playbook_commands

router = APIRouter(prefix="/pages", tags=["workflows"])


class StartPlaybookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Where the created issues land. Omitted means the backlog, matching plain
    # issue creation rather than inventing a different default.
    project_id: int | None = Field(default=None, ge=1, le=MAX_SQLITE_INTEGER)
    # Overrides the parent's title; children always take their step text.
    title: str | None = None


@router.post("/{page_id}/start-playbook", status_code=201)
def start_playbook(
    page_id: RowIdPath,
    payload: StartPlaybookIn | None = None,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Turn a playbook page's checklist into a parent issue and its children.

    Retry-safe through the ordinary `Idempotency-Key` contract this API root
    already honors — there is no playbook-specific replay mechanism, because a
    second one would be a second thing to keep in sync with the first.
    """
    body = payload or StartPlaybookIn()
    try:
        return playbook_commands.start_playbook(
            conn,
            actor=actor,
            page_id=page_id,
            project_id=body.project_id,
            title=body.title,
        )
    except playbook_commands.PlaybookCommandError as exc:
        raise HTTPException(
            status_code=playbook_commands.STATUS_BY_KIND.get(exc.kind, 400),
            detail=exc.detail,
        ) from exc
