"""The users REST API.

Users are core (every module's rows point at a user), so this lives in core/,
not in a feature module. Same shape as aegis/api.py: Pydantic validates the body,
the data-access layer in core/users.py does the SQL.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from athena.core import users
from athena.core.deps import get_conn
from athena.core.identity import admin_actor, current_actor, is_admin, optional_actor, require_admin

router = APIRouter(prefix="/users", tags=["core"])

Role = Literal["admin", "member", "viewer"]


class UserCreate(BaseModel):
    email: str
    name: str
    # Optional: set it to enable browser login. Omit for agent/API-only users.
    password: str | None = None
    # Bootstrap ignores this and always creates the first user as admin.
    role: Role | None = None


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: Role
    created_at: str


@router.post("", response_model=UserOut, status_code=201)
def create(
    payload: UserCreate,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Bootstrap exception: the very first user is created without authentication,
    # because on a fresh install there is nobody to authenticate as yet. The first
    # user is always admin so a deploy cannot accidentally lock itself out. After
    # bootstrap, only admins may add users or assign roles.
    existing = users.count_users(conn)
    if existing == 0:
        role = users.BOOTSTRAP_ROLE
    else:
        if actor is None:
            raise HTTPException(status_code=401, detail="authentication required")
        require_admin(actor)
        role = payload.role or users.DEFAULT_ROLE
    try:
        return users.create_user(
            conn,
            email=payload.email,
            name=payload.name,
            password=payload.password,
            role=role,
        )
    except sqlite3.IntegrityError:
        # email collides with an existing user — reject at the boundary.
        raise HTTPException(status_code=400, detail="email already in use")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[UserOut])
def index(
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Listing users is an authenticated operation — don't let an exposed instance
    # be enumerated anonymously.
    return users.list_users(conn)


@router.get("/{user_id}", response_model=UserOut)
def show(
    user_id: int,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    if not is_admin(actor) and actor["id"] != user_id:
        raise HTTPException(status_code=403, detail="admin role required")
    user = users.get_user(conn, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="no such user")
    return user
