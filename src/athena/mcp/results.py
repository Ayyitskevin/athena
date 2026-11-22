"""The one result shape every MCP tool answers with (A4).

Every tool call returns exactly one JSON object. A tool that reads a list
returns a ``ListResult``: ``{items, count, truncated, next}``. ``truncated`` is
true when a limit was reached or content was cut, and then ``next`` holds the
arguments that continue (merge them into the same call's arguments); otherwise
``next`` is null. An empty list is the same object with no items, never an
absent answer.

The helpers here build that envelope from whatever paging the REST route
already has, so a tool states its paging once: ``paged`` for an offset route,
``window`` for a route that only takes a limit, ``listing`` for a route that
returns everything it has.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from pydantic import BaseModel, GetJsonSchemaHandler, RootModel
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema


class ListResult(TypedDict):
    """A list, as one object: the rows, how many, and how to continue."""

    items: list[Any]
    count: int
    truncated: bool
    next: dict[str, Any] | None


def listing(
    items: Sequence[Any],
    *,
    truncated: bool = False,
    next: dict[str, Any] | None = None,
) -> ListResult:
    """The envelope for ``items``. A complete list passes neither keyword."""
    if next is not None and not truncated:
        raise ValueError("next continues a truncated list only")
    rows = list(items)
    return {"items": rows, "count": len(rows), "truncated": truncated, "next": next}


def paged(items: Sequence[Any], *, limit: int, offset: int) -> ListResult:
    """A page of an offset route. A full page may have more after it: the route
    cannot say, so the page reports itself truncated and names the next offset."""
    full = len(items) >= limit
    return listing(
        items,
        truncated=full,
        next={"offset": offset + len(items)} if full else None,
    )


def window(
    fetch: Callable[[int], Sequence[Any]],
    *,
    limit: int,
    offset: int,
    cap: int,
) -> ListResult:
    """A page of a route that takes only a limit (at most ``cap``): ask for
    ``offset + limit`` rows and keep the last ``limit``. A page that reaches
    ``cap`` stays truncated, with a ``next`` that names the rows still in reach,
    or null when the route can give no more."""
    if offset < 0:
        raise ValueError("offset must not be negative")
    if offset + limit > cap:
        raise ValueError(f"offset + limit must be at most {cap} for this list")
    rows = list(fetch(offset + limit))
    items = rows[offset:]
    if len(rows) < offset + limit:
        return listing(items)
    reached = offset + len(items)
    if reached >= cap:
        # The route cannot page past its cap: more may exist, out of reach.
        return {"items": items, "count": len(items), "truncated": True, "next": None}
    continuation: dict[str, Any] = {"offset": reached}
    if reached + limit > cap:
        continuation["limit"] = cap - reached
    return listing(items, truncated=True, next=continuation)


# --- the declared shapes -------------------------------------------------------
#
# FastMCP turns a tool's return annotation into its output schema and validates
# the tool's answer against the same model. These two models are what
# ``server._one_object`` declares: they validate exactly what the TypedDict
# above and a plain dict describe, and they publish the smallest schema that
# says so, because every schema is carried in every session's tool list.


class ObjectShape(RootModel[dict[str, Any]]):
    """One JSON object, its keys the REST body's."""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {"type": "object"}


class ListShape(BaseModel):
    """The list envelope, by its four keys."""

    items: list[Any]
    count: int
    truncated: bool
    next: dict[str, Any] | None

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {
            "type": "object",
            "properties": {
                "items": {"type": "array"},
                "count": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "next": {"type": ["object", "null"]},
            },
            "required": ["items", "count", "truncated", "next"],
        }
