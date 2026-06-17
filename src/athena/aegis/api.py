"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from athena.aegis import issues
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/issues", tags=["aegis"])


class IssueCreate(BaseModel):
    title: str
    body: str = ""


class IssueOut(BaseModel):
    id: int
    title: str
    body: str
    status: str
    created_by: int
    created_at: str


@router.post("", response_model=IssueOut, status_code=201)
def create(
    payload: IssueCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # created_by is the authenticated actor, never a value the caller supplied.
    return issues.create_issue(
        conn, title=payload.title, body=payload.body, created_by=actor["id"]
    )


@router.get("", response_model=list[IssueOut])
def index(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    return issues.list_issues(conn)
