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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from athena.core import token_commands, tokens
from athena.core.deps import get_conn
from athena.core.identity import current_actor, token_management_actor

router = APIRouter(prefix="/tokens", tags=["core"])

TokenScope = Literal["read", "issue:write", "docs:write", "admin"]


class TokenCreate(BaseModel):
    name: str
    # REQUIRED in effect: omitting scopes is a 422 with a helpful message (the old
    # behavior silently minted ADMIN — a fail-open default that inverted least
    # privilege). Kept Optional in the model so the error is ours, not Pydantic's.
    scopes: list[TokenScope] | None = None


class TokenOut(BaseModel):
    id: int
    user_id: int
    name: str
    scopes: list[TokenScope]
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


class TokenCreatedOut(TokenOut):
    # The raw secret — shown once, on creation, and never again.
    token: str


@router.post("", response_model=TokenCreatedOut, status_code=201)
def create(
    payload: TokenCreate,
    actor: dict = Depends(token_management_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The token belongs to the authenticated actor — not a user id from the body.
    # The command owns the mint AND its atomic 'minted_token' audit event, so a fresh
    # credential is never a silent write.
    try:
        return token_commands.mint_token(
            conn, actor_id=actor["id"], name=payload.name, scopes=payload.scopes
        )
    except token_commands.TokenCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


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
    actor: dict = Depends(token_management_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    # The command owns the revoke AND its atomic 'revoked_token' audit event.
    if not token_commands.revoke_token(conn, actor_id=actor["id"], token_id=token_id):
        raise HTTPException(status_code=404, detail="no such live token")
    return Response(status_code=204)
