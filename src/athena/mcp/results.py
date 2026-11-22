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

They also keep the answer within the call's response budget (A6, set by the
server around each call with ``budget``): a list over budget keeps the rows
that fit, says ``truncated`` and names the ``next`` arguments that resume
after them; ``fit_object`` cuts an object's long text or nested list the same
honest way, recording each cut under ``_truncated`` with the tool call that
reads the rest. Nothing is cut without saying so.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
import copy
from dataclasses import dataclass
from typing import Any, TypedDict

from pydantic import BaseModel, GetJsonSchemaHandler, RootModel
from pydantic.json_schema import JsonSchemaValue
import pydantic_core
from pydantic_core import CoreSchema


class ListResult(TypedDict):
    """A list, as one object: the rows, how many, and how to continue."""

    items: list[Any]
    count: int
    truncated: bool
    next: dict[str, Any] | None


#: The response budget of the tool call in progress, in characters of the JSON
#: text a client receives; None for a tool that has none.
_BUDGET: ContextVar[int | None] = ContextVar("athena_mcp_budget", default=None)

#: Where an object records what the budget cut: ``{path: {total, next}}``.
TRUNCATED_KEY = "_truncated"


@contextmanager
def budget(chars: int | None) -> Iterator[None]:
    """Hold the helpers below to ``chars`` for the duration of one tool call."""
    token = _BUDGET.set(chars)
    try:
        yield
    finally:
        _BUDGET.reset(token)


def serialized_size(value: Any) -> int:
    """Characters of the text block FastMCP sends for ``value``: it serializes a
    tool's answer with ``pydantic_core.to_json(..., indent=2)``."""
    return len(pydantic_core.to_json(value, fallback=str, indent=2).decode())


def _envelope(
    rows: list[Any], truncated: bool, next: dict[str, Any] | None
) -> ListResult:
    return {"items": rows, "count": len(rows), "truncated": truncated, "next": next}


def _largest_fitting(fits: Callable[[int], bool], low: int, high: int) -> int:
    """The largest n in [low, high] with fits(n), given fits is monotone and
    fits(low); low when nothing larger fits."""
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def listing(
    items: Sequence[Any],
    *,
    truncated: bool = False,
    next: dict[str, Any] | None = None,
    resume: Callable[[int], dict[str, Any]] | None = None,
) -> ListResult:
    """The envelope for ``items``. A complete list passes neither ``truncated``
    nor ``next``. ``resume(n)`` names the arguments that continue after the
    first ``n`` rows; with it, a list over the call's budget keeps the rows that
    fit (at least one) and reports itself truncated at that point."""
    if next is not None and not truncated:
        raise ValueError("next continues a truncated list only")
    rows = list(items)
    whole = _envelope(rows, truncated, next)
    limit = _BUDGET.get()
    if (
        resume is None
        or limit is None
        or len(rows) < 2
        or serialized_size(whole) <= limit
    ):
        return whole
    cut = resume  # narrowed to a callable for the closure below

    def fits(kept: int) -> bool:
        return serialized_size(_envelope(rows[:kept], True, cut(kept))) <= limit

    kept = _largest_fitting(fits, 1, len(rows) - 1)
    return _envelope(rows[:kept], True, resume(kept))


def paged(items: Sequence[Any], *, limit: int, offset: int) -> ListResult:
    """A page of an offset route. A full page may have more after it: the route
    cannot say, so the page reports itself truncated and names the next offset."""
    full = len(items) >= limit
    return listing(
        items,
        truncated=full,
        next={"offset": offset + len(items)} if full else None,
        resume=lambda kept: {"offset": offset + kept},
    )


def sliced(rows: Sequence[Any], *, offset: int) -> ListResult:
    """A route that returns everything it has, read from ``offset``: complete
    unless the budget cuts it, and then resumed by offset."""
    if offset < 0:
        raise ValueError("offset must not be negative")
    return listing(rows[offset:], resume=lambda kept: {"offset": offset + kept})


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

    def resume(kept: int) -> dict[str, Any]:
        reached = offset + kept
        continuation: dict[str, Any] = {"offset": reached}
        if reached + limit > cap:
            continuation["limit"] = cap - reached
        return continuation

    if len(rows) < offset + limit:
        return listing(items, resume=resume)
    reached = offset + len(items)
    if reached >= cap:
        # The route cannot page past its cap: more may exist, out of reach.
        return listing(items, truncated=True, resume=resume)
    return listing(items, truncated=True, next=resume(len(items)), resume=resume)


# --- summary rows ----------------------------------------------------------------

#: How much of a comment a summary row quotes.
EXCERPT_CHARS = 200


def summary(row: dict[str, Any]) -> dict[str, Any]:
    """A list row without its body: identifiers, title, status and the other short
    fields. The body is read from the row's own get-one tool."""
    return {key: value for key, value in row.items() if key != "body"}


def from_offset(owner: dict[str, Any], field: str, offset: int) -> dict[str, Any]:
    """``owner`` with its text ``field`` read from ``offset`` on. A window that
    does not start at the beginning says so under ``_truncated`` (with no
    ``next`` until the budget cuts its end too)."""
    if offset < 0:
        raise ValueError("body_offset must not be negative")
    if offset == 0 or not isinstance(owner.get(field), str):
        return owner
    whole = owner[field]
    marks = dict(owner.get(TRUNCATED_KEY) or {})
    marks[field] = {"total_chars": len(whole), "offset": offset, "next": None}
    return {**owner, field: whole[offset:], TRUNCATED_KEY: marks}


# --- cutting an object to its budget -------------------------------------------


@dataclass(frozen=True)
class Cut:
    """One place an object may be shortened: the text or the list at ``path``,
    or, with ``each``, the text at ``each`` inside every item of that list.

    ``reader(kept)`` is the tool call (``{"tool", "arguments"}``) that reads
    what follows the first ``kept`` characters or rows. ``adjust`` updates the
    fields beside a cut (a REST ``has_more`` or ``truncated``, say). A cut
    keeps at least ``floor`` characters or rows."""

    path: tuple[str, ...]
    reader: Callable[[int], dict[str, Any]]
    adjust: Callable[[dict[str, Any], int], None] | None = None
    each: tuple[str, ...] | None = None
    floor: int = 1


def _at(value: Any, path: tuple[str, ...]) -> Any:
    for step in path:
        if not isinstance(value, dict):
            return None
        value = value.get(step)
    return value


def _put(value: Any, path: tuple[str, ...], new: Any) -> None:
    for step in path[:-1]:
        value = value[step]
    value[path[-1]] = new


def _name(cut: Cut) -> str:
    name = ".".join(str(step) for step in cut.path)
    if cut.each is not None:
        name += "[*]." + ".".join(str(step) for step in cut.each)
    return name


def _span(result: dict[str, Any], cut: Cut) -> int:
    """How many characters or rows the place holds uncut (0: nothing to cut)."""
    found = _at(result, cut.path)
    if cut.each is None:
        return len(found) if isinstance(found, (str, list)) else 0
    if not isinstance(found, list):
        return 0
    texts = [_at(item, cut.each) for item in found]
    return max((len(text) for text in texts if isinstance(text, str)), default=0)


def _render(
    result: dict[str, Any], cuts: Sequence[Cut], kept: dict[int, int]
) -> dict[str, Any]:
    """``result`` with each cut in ``kept`` applied, and recorded."""
    fitted = copy.deepcopy(result)
    marks: dict[str, Any] = dict(fitted.get(TRUNCATED_KEY) or {})
    for index, amount in kept.items():
        cut = cuts[index]
        whole = _at(result, cut.path)
        name = _name(cut)
        if cut.each is None:
            _put(fitted, cut.path, whole[:amount])
            mark = dict(marks.get(name) or {})
            unit = "total_chars" if isinstance(whole, str) else "total_items"
            mark.setdefault(unit, len(whole))
        else:
            for item in _at(fitted, cut.path):
                text = _at(item, cut.each)
                if isinstance(text, str) and len(text) > amount:
                    _put(item, cut.each, text[:amount])
            mark = {"max_chars": amount}
        mark["next"] = cut.reader(amount)
        marks[name] = mark
        if cut.adjust is not None:
            cut.adjust(fitted, amount)
    if marks:
        fitted[TRUNCATED_KEY] = marks
    return fitted


def fit_object(result: dict[str, Any], cuts: Sequence[Cut]) -> dict[str, Any]:
    """``result`` within the call's budget, cutting only the places in ``cuts``.

    The places are listed least valuable first. Each is cut in turn, down to its
    floor, until the object fits; the place that makes it fit keeps as much as
    still fits. Every cut is recorded under ``_truncated[path]`` with the size
    the answer carried and the call that reads on. An object the listed cuts
    cannot bring within budget is returned cut to every floor, over budget, and
    never cut anywhere unlisted."""
    limit = _BUDGET.get()
    if limit is None or serialized_size(result) <= limit:
        return result
    kept: dict[int, int] = {}

    def fits(index: int, amount: int) -> bool:
        trial = {**kept, index: amount}
        return serialized_size(_render(result, cuts, trial)) <= limit

    for index, cut in enumerate(cuts):
        span = _span(result, cut)
        if span <= cut.floor:
            continue
        if not fits(index, cut.floor):
            kept[index] = cut.floor
            continue
        settling = index
        kept[index] = _largest_fitting(
            lambda amount: fits(settling, amount), cut.floor, span - 1
        )
        break
    return _render(result, cuts, kept)


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
