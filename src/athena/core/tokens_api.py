"""The API-tokens REST API.

Tokens are how a user (human or agent) authenticates, so this is core/. Every
endpoint here is itself authenticated via `current_actor`: you must already be
identified to manage your tokens. On a fresh deploy the X-Athena-Actor fallback
(see core/identity.py) bootstraps the very first token; after that, bearer
tokens can do everything.

A token's raw secret is returned exactly once, by POST. It is never stored or
retrievable again — only its hash lives in the database.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from athena.core import tokens
from athena.core.deps import get_conn
from athena.core.identity import current_actor, write_actor

router = APIRouter(prefix="/tokens", tags=["core"])


class TokenCreate(BaseModel):
    name: str


class TokenOut(BaseModel):
    id: int
    user_id: int
    name: str
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


class TokenCreatedOut(TokenOut):
    # The raw secret — shown once, on creation, and never again.
    token: str


@router.post("", response_model=TokenCreatedOut, status_code=201)
def create(
    payload: TokenCreate,
    actor: dict = Depends(write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The token belongs to the authenticated actor — not a user id from the body.
    return tokens.create_token(conn, user_id=actor["id"], name=payload.name)


@router.get("", response_model=list[TokenOut])
def index(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # You only ever see your own tokens.
    return tokens.list_tokens(conn, actor["id"])


@router.delete("/{token_id}", status_code=204)
def revoke(
    token_id: int,
    actor: dict = Depends(write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    if not tokens.revoke_token(conn, user_id=actor["id"], token_id=token_id):
        raise HTTPException(status_code=404, detail="no such live token")
    return Response(status_code=204)
