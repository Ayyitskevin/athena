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
from athena.mentor import pages, spaces

spaces_router = APIRouter(prefix="/spaces", tags=["mentor"])
# A page belongs to a space, so create/list live under /spaces/{id}/pages
# (a sub-resource, like comments under an issue). A single page is addressable on
# its own canonical URL via this top-level router, like GET /issues/{id}.
pages_router = APIRouter(prefix="/pages", tags=["mentor"])


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


class PageCreate(BaseModel):
    title: str
    body: str = ""
    parent_id: int | None = None


class PageOut(BaseModel):
    id: int
    space_id: int
    parent_id: int | None = None
    title: str
    body: str
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


# --- Pages: documents within a space --------------------------------------


@spaces_router.post("/{space_id}/pages", response_model=PageOut, status_code=201)
def create_page(
    space_id: int,
    payload: PageCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may create a page (like creating an issue). The
    # space is a path resource: 404 if it doesn't exist.
    if spaces.get_space(conn, space_id) is None:
        raise HTTPException(status_code=404, detail="no such space")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="page title is required")
    # A parent (if given) must be a real page IN THIS SAME SPACE — a tree can't
    # span spaces. Checked here because the FK can express "is a page" but not
    # "is a page in this space".
    if payload.parent_id is not None:
        parent = pages.get_page(conn, payload.parent_id)
        if parent is None or parent["space_id"] != space_id:
            raise HTTPException(
                status_code=422, detail="parent must be a page in this space"
            )
    return pages.create_page(
        conn,
        space_id=space_id,
        title=title,
        body=payload.body,
        parent_id=payload.parent_id,
        created_by=actor["id"],
    )


@spaces_router.get("/{space_id}/pages", response_model=list[PageOut])
def list_pages(
    space_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    # Reads are open. 404 if the space itself is missing (distinct from a real
    # space that simply has no pages yet, which returns []).
    if spaces.get_space(conn, space_id) is None:
        raise HTTPException(status_code=404, detail="no such space")
    return pages.list_pages_in_space(conn, space_id)


@pages_router.get("/{page_id}", response_model=PageOut)
def show_page(
    page_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    page = pages.get_page(conn, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="no such page")
    return page
