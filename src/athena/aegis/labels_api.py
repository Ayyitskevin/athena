"""REST routes for the shared label vocabulary.

Split out of aegis/api.py. Attaches to the labels router defined there.
Creating a label goes through ``label_commands`` so the insert and its
``label_created`` event commit together.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from athena.aegis.api import LabelOut, labels_router
from athena.core import label_commands, labels
from athena.core.deps import get_conn
from athena.core.identity import issue_write_actor, optional_actor

# Adapter-owned translation of command-error kinds (the transport decides).
STATUS_BY_KIND: dict[str, int] = {
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "forbidden": 403,
    "unauthorized": 401,
}


class LabelCreate(BaseModel):
    name: str
    color: str = "#6b7280"


@labels_router.get("", response_model=list[LabelOut])
def list_all_labels(
    _actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading the vocabulary is open, like listing issues.
    return labels.list_labels(conn)


@labels_router.post("", response_model=LabelOut, status_code=201)
def create_label(
    payload: LabelCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Add one name to the shared vocabulary and record who added it."""
    try:
        return label_commands.create_label(
            conn, actor=actor, name=payload.name, color=payload.color
        )
    except label_commands.LabelCommandError as exc:
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=exc.detail
        ) from exc
