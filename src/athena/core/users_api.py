"""The users REST API.

Users are core (every module's rows point at a user), so this lives in core/,
not in a feature module. Same shape as aegis/api.py: Pydantic validates the body,
the data-access layer in core/users.py does the SQL.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from athena.core import users
from athena.core.deps import get_conn

router = APIRouter(prefix="/users", tags=["core"])


class UserCreate(BaseModel):
    email: str
    name: str


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    created_at: str


@router.post("", response_model=UserOut, status_code=201)
def create(payload: UserCreate, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return users.create_user(conn, email=payload.email, name=payload.name)
    except sqlite3.IntegrityError:
        # email collides with an existing user — reject at the boundary.
        raise HTTPException(status_code=400, detail="email already in use")


@router.get("", response_model=list[UserOut])
def index(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    return users.list_users(conn)


@router.get("/{user_id}", response_model=UserOut)
def show(user_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    user = users.get_user(conn, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="no such user")
    return user
