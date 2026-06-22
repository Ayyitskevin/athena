"""The Mentor REST API: space endpoints.

The first slice of the docs module. Spaces are top-level containers; pages (a
later slice) will live inside them. Mirrors the shape of the Aegis projects
router. Mounted by main.py.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from athena.core.deps import get_conn
from athena.core.identity import current_actor
from athena.mentor import spaces

spaces_router = APIRouter(prefix="/spaces", tags=["mentor"])


class SpaceCreate(BaseModel):
    key: str
    name: str
    description: str = ""


class SpaceOut(BaseModel):
    id: int
    key: str
    name: str
    description: str
    created_by: int
    created_at: str


@spaces_router.get("", response_model=list[SpaceOut])
def list_all_spaces(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    # Reading the space list is open, like listing issues or projects.
    return spaces.list_spaces(conn)


@spaces_router.post("", response_model=SpaceOut, status_code=201)
def create_space(
    payload: SpaceCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may create a space (like creating a project).
    # The key is the canonical identity, normalized to uppercase so "eng" and
    # "ENG" are one space and URLs built from it are predictable.
    key = payload.key.strip().upper()
    name = payload.name.strip()
    if not key:
        raise HTTPException(status_code=422, detail="space key is required")
    if not name:
        raise HTTPException(status_code=422, detail="space name is required")
    if spaces.get_space_by_key(conn, key) is not None:
        raise HTTPException(status_code=409, detail="space key already exists")
    return spaces.create_space(
        conn,
        key=key,
        name=name,
        description=payload.description,
        created_by=actor["id"],
    )


@spaces_router.get("/{space_id}", response_model=SpaceOut)
def show_space(
    space_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    space = spaces.get_space(conn, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="no such space")
    return space
