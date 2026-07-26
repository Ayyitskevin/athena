"""Resolving embed directives over REST — the structured half of live embeds.

The browser gets embeds as HTML, inline in a rendered page. An agent must not:
markup is a presentation detail, and an agent asking "what work does this runbook
point at" wants rows, not `<div>`s. This endpoint returns exactly what
`embed_data.resolve` produces — the same resolver the HTML renderer uses, so the
two surfaces cannot disagree about what a page's embeds say.

**Why this takes text rather than a page id.** The obvious route would be
`GET /pages/{id}/embeds`, but the import contract makes `aegis` and `mentor`
peers: a Mentor route cannot import this resolver, and this module cannot read a
Mentor page. Rather than route around the contract (a composition-root injection,
as Stage D needed for undo compensators — justified there, heavy here), the
endpoint resolves directives in *any* text the caller can already read. Reading
the page is the caller's separate, already-authorized call, and the MCP client
composes the two so an agent still makes one tool call.

That has an independent benefit: an agent can resolve a body it is *about* to
save and see what the embeds will show before writing the page.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from athena.aegis import embed_data
from athena.core import embeds
from athena.core.deps import get_conn
from athena.core.identity import optional_actor

router = APIRouter(prefix="/embeds", tags=["aegis"])

#: A page body's practical ceiling. Bounded so this endpoint cannot be used to
#: hand the server an arbitrarily large string to scan.
MAX_TEXT_CHARS = 200_000


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_TEXT_CHARS)


class EmbedOut(BaseModel):
    # Deliberately loose: each kind carries a different payload, and a strict
    # per-kind union would make adding a kind a breaking schema change for every
    # client. `kind` and `error` are the two fields every result has, and they
    # are what a caller branches on.
    model_config = ConfigDict(extra="allow")

    kind: str | None = None
    error: str | None = None


@router.post("/resolve", response_model=list[EmbedOut])
def resolve(
    payload: ResolveIn,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """Resolve every embed directive in a body, as the calling actor.

    Results are in document order, one per directive, each either carrying data
    or an ``error`` explaining why it did not render. Visibility is the caller's:
    the same text resolved by two people can legitimately return different rows,
    which is the point.
    """
    return embed_data.resolve_body(conn, payload.text, actor=actor)


@router.get("/help")
def embed_help() -> dict:
    """The embed vocabulary as data — kinds, keys, and limits.

    Emitted from `embeds.describe()` rather than restated, so this endpoint, the
    MCP docstring, and docs/EMBEDS.md cannot drift from the parser.
    """
    return embeds.describe()
