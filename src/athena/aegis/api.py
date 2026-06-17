"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from athena.aegis import issues
from athena.core.deps import get_conn

router = APIRouter(prefix="/issues", tags=["aegis"])


class IssueCreate(BaseModel):
    title: str
    body: str = ""
    created_by: int


class IssueOut(BaseModel):
    id: int
    title: str
    body: str
    status: str
    created_by: int
    created_at: str


@router.post("", response_model=IssueOut, status_code=201)
def create(payload: IssueCreate, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return issues.create_issue(
            conn, title=payload.title, body=payload.body, created_by=payload.created_by
        )
    except sqlite3.IntegrityError:
        # created_by points at no real user — reject at the boundary.
        raise HTTPException(status_code=400, detail="created_by must be an existing user id")


@router.get("", response_model=list[IssueOut])
def index(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    return issues.list_issues(conn)
