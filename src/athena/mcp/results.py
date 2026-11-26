"""The one result shape every MCP tool answers with (A4).

Every tool call returns exactly one JSON object. A tool that reads a list
returns a ``ListResult``: ``{items, count, truncated, next}``. ``truncated`` is
true when a limit was reached or content was cut, and then ``next`` holds the
arguments that continue (merge them into the same call's arguments); otherwise
``next`` is null. An empty list is the same object with no items, never an
absent answer.

The helpers here build that envelope from whatever paging the REST route
already has, so a tool states its paging once: ``cursor_page`` for a route
that pages in D2's vocabulary (``cursor`` in, ``next_cursor`` and ``has_more``
out), ``paged`` for an offset route, ``window`` for a route that only takes a
limit, ``listing`` for a route that returns everything it has.

They also keep the answer within the call's response budget (A6, set by the
server around each call with ``budget``): a list over budget keeps the rows
that fit, says ``truncated`` and names the ``next`` arguments that resume
after them; ``fit_object`` cuts an object's long text or nested list the same
honest way, recording each cut under ``_truncated`` with the tool call that
reads the rest. An object still over budget after those declared cuts is cut by
its shape (``shrink``), which always ends within the budget. Nothing is cut
without saying so.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
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


def answer_text(value: Any) -> str:
    """The text block a client receives for ``value``: compact JSON, no space
    between tokens, keys in their order, non-ASCII text as itself (F12).

    The server's protocol handler (``server._answer_refusals_as_one_object``)
    sends every answer, success or failure, as this text. FastMCP's own
    rendering of a tool's answer is indented JSON
    (``pydantic_core.to_json(..., indent=2)``); that was about a sixth of what a
    seat's answers sent, and a budgeted answer was cut that much earlier."""
    return pydantic_core.to_json(value, fallback=str).decode()


def serialized_size(value: Any) -> int:
    """Characters of the text block a client receives for ``value``: the
    budget measures what is sent, and nothing else."""
    return len(answer_text(value))


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


#: One page of a D2 route as a tool sees it: the rows (already shaped), the
#: route's ``next_cursor`` and its ``has_more``.
CursorPage = tuple[Sequence[Any], str | None, bool]


def cursor_page(fetch: Callable[[int], CursorPage], *, limit: int) -> ListResult:
    """A page of a route that pages in D2's vocabulary, continued by the
    route's own cursor: ``fetch(n)`` reads ``n`` rows from the call's cursor.

    The list is truncated exactly when the route says ``has_more``, and ``next``
    is ``{"cursor": next_cursor}``. Over the call's budget, the route is asked
    again for the rows that fit, so the cursor a caller resumes from is always
    one the route handed out (a tool never makes one up, and never re-reads the
    whole list to find its place); ``next`` then also names that smaller
    ``limit``, so the following call asks for what fits from the start."""
    asked = limit
    rows, next_cursor, has_more = fetch(asked)
    while True:
        items = list(rows)
        continuation: dict[str, Any] | None = None
        if has_more:
            continuation = {"cursor": next_cursor}
            if asked < limit:
                continuation["limit"] = asked
        answer = _envelope(items, has_more, continuation)
        cap = _BUDGET.get()
        if cap is None or len(items) < 2 or serialized_size(answer) <= cap:
            return answer
        asked = _rows_within(items, next_cursor, cap)
        rows, next_cursor, has_more = fetch(asked)


def _rows_within(items: list[Any], next_cursor: str | None, cap: int) -> int:
    """How many of ``items`` a truncated envelope holds within ``cap`` (at least
    one). The shorter page's cursor is not known until it is read; this page's
    stands in for its length, and a longer one is caught on the next turn."""
    stand_in = {"cursor": next_cursor or "", "limit": len(items)}
    return _largest_fitting(
        lambda kept: serialized_size(_envelope(items[:kept], True, stand_in)) <= cap,
        1,
        len(items) - 1,
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


def fit_object(
    result: dict[str, Any],
    cuts: Sequence[Cut],
    *,
    rest: Reader | None = None,
    keep: Mapping[Path, int] | None = None,
) -> dict[str, Any]:
    """``result`` within the call's budget: the declared ``cuts`` first, then,
    if it is still over, the structural cut (:func:`shrink`).

    The declared places are listed least valuable first. Each is cut in turn,
    down to its floor, until the object fits; the place that makes it fit keeps
    as much as still fits. Every cut is recorded under ``_truncated[path]`` with
    the size the answer carried and the call that reads on. An object the
    declared cuts cannot bring within budget is handed to :func:`shrink`, which
    always ends within it: ``rest`` names the call that reads what it cuts and
    ``keep`` the lists that keep more than one row."""
    limit = _BUDGET.get()
    if limit is None or serialized_size(result) <= limit:
        return result
    fitted = _fit_declared(result, cuts, limit)
    if serialized_size(fitted) <= limit:
        return fitted
    return shrink(fitted, limit, rest=rest, keep=keep)


def _fit_declared(
    result: dict[str, Any], cuts: Sequence[Cut], limit: int
) -> dict[str, Any]:
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


# --- the structural cut ---------------------------------------------------------
#
# The declared cuts know where an answer's bulk usually is. They cannot know
# every place: a desk lane at its maximum, a title no write path bounds, a
# notebook line. What is still over budget after them is cut here, by shape,
# and the cut always ends within the budget.

#: A place in an answer: dict keys, and list indexes below a list.
Path = tuple[str | int, ...]
#: ``rest(path, kept)``: the call that reads what follows the first ``kept``
#: rows (a list) or characters (a text) at ``path``, or None when no tool does.
Reader = Callable[[Path, int], dict[str, Any] | None]

#: A text reached through objects alone is cut only when it is longer than
#: twice this, and then no shorter than this: the short texts an answer explains
#: itself with (a lane's ``meaning``) stay whole.
TEXT_FLOOR = 1_000


#: A ``keep`` floor that keeps a list whole.
WHOLE = 2**31


@dataclass
class _Plan:
    lists: dict[Path, int]  # rows kept, per list
    texts: dict[Path, int]  # characters kept, per long text outside a row
    cap: int | None = None  # characters kept by every text inside a row
    # Lists kept WHOLE: their rows' texts are never capped either, so a list
    # declared whole (the desk's identity scopes) reads exactly as sent.
    whole: frozenset[Path] = frozenset()


def _places(
    value: Any, path: Path = (), in_row: bool = False
) -> Iterator[tuple[Path, Any]]:
    """Every list, at any depth (a list inside a row is a place of its own,
    named with the row's index), and every text longer than twice
    :data:`TEXT_FLOOR` reached through objects alone, outside any
    ``_truncated`` record."""
    if isinstance(value, list):
        for index, row in enumerate(value):
            yield from _places(row, (*path, index), True)
        return
    if not isinstance(value, dict):
        return
    for key, inner in value.items():
        if key == TRUNCATED_KEY:
            continue
        here = (*path, key)
        if isinstance(inner, list):
            yield here, inner
            yield from _places(inner, here, True)
        elif isinstance(inner, str) and not in_row and len(inner) > 2 * TEXT_FLOOR:
            yield here, inner
        elif isinstance(inner, dict):
            yield from _places(inner, here, in_row)


def _get(value: Any, path: Path) -> Any:
    """The value at ``path`` (keys and row indexes), or None where it is gone."""
    for step in path:
        if isinstance(step, int):
            if not isinstance(value, list) or step >= len(value):
                return None
            value = value[step]
        elif isinstance(value, dict) and step in value:
            value = value[step]
        else:
            return None
    return value


def _nested(path: Path) -> bool:
    return any(isinstance(step, int) for step in path)


def dotted(path: Path) -> str:
    """A path as the ``_truncated`` key and the desk's section name write it."""
    return ".".join(str(step) for step in path)


def _build(
    value: Any, path: Path, plan: _Plan, in_row: bool, exempt: bool = False
) -> Any:
    if isinstance(value, dict):
        return {
            key: inner
            if key == TRUNCATED_KEY
            else _build(inner, (*path, key), plan, in_row, exempt)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        rows = value[: plan.lists.get(path, len(value))]
        whole = exempt or path in plan.whole
        return [
            _build(row, (*path, index), plan, True, whole)
            for index, row in enumerate(rows)
        ]
    if isinstance(value, str):
        if in_row and exempt:
            return value
        if in_row:
            kept = len(value) if plan.cap is None else min(len(value), plan.cap)
        else:
            kept = plan.texts.get(path, len(value))
        return value[:kept]
    return value


def _first_capped(rows: list[Any], cap: int) -> int | None:
    """The index of the first row holding a text longer than ``cap``."""

    def longer(value: Any) -> bool:
        if isinstance(value, str):
            return len(value) > cap
        if isinstance(value, dict):
            return any(
                longer(inner) for key, inner in value.items() if key != TRUNCATED_KEY
            )
        if isinstance(value, list):
            return any(longer(inner) for inner in value)
        return False

    return next((index for index, row in enumerate(rows) if longer(row)), None)


def _flag_cut_list(parent: dict[str, Any], key: str, kept: int) -> None:
    """The fields beside a cut list that say whether it is whole."""
    if key == "items" and isinstance(parent.get("clipped"), bool):
        parent["clipped"] = True
    if isinstance(parent.get(f"{key}_clipped"), bool):
        parent[f"{key}_clipped"] = True
    if "has_more" in parent:
        parent["has_more"] = True
    if "next_offset" in parent:
        parent["next_offset"] = kept


def _render_plan(
    result: dict[str, Any],
    places: list[tuple[Path, Any]],
    plan: _Plan,
    rest: Reader,
) -> dict[str, Any]:
    """``result`` with ``plan`` applied and every cut recorded."""
    fitted = _build(result, (), plan, False)
    marks: dict[str, Any] = dict(fitted.get(TRUNCATED_KEY) or {})

    def mark(name: str, unit: str, total: int, reader: dict[str, Any] | None) -> None:
        entry = dict(marks.get(name) or {})
        entry.setdefault(unit, total)
        entry["next"] = reader
        marks[name] = entry

    for path, whole in places:
        if isinstance(whole, list):
            if _nested(path) and _get(fitted, path) is None:
                continue  # its row went with its list, whose own mark says so
            kept = plan.lists.get(path, len(whole))
            if kept < len(whole):
                mark(dotted(path), "total_items", len(whole), rest(path, kept))
                parent = _get(fitted, path[:-1])
                if isinstance(parent, dict):
                    _flag_cut_list(parent, str(path[-1]), kept)
            if plan.cap is not None and not _nested(path) and path not in plan.whole:
                first = _first_capped(whole[:kept], plan.cap)
                if first is not None:
                    marks[f"{dotted(path)}[*]"] = {
                        "max_chars": plan.cap,
                        "next": rest((*path, first), 0),
                    }
        else:
            kept = plan.texts.get(path, len(whole))
            if kept < len(whole):
                mark(dotted(path), "total_chars", len(whole), rest(path, kept))
    if marks:
        fitted[TRUNCATED_KEY] = marks
    return fitted


def _longest_row_text(places: list[tuple[Path, Any]]) -> int:
    def longest(value: Any) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, dict):
            return max(
                (longest(v) for k, v in value.items() if k != TRUNCATED_KEY), default=0
            )
        if isinstance(value, list):
            return max((longest(v) for v in value), default=0)
        return 0

    return max(
        (
            longest(whole)
            for path, whole in places
            if isinstance(whole, list) and not _nested(path)
        ),
        default=0,
    )


def shrink(
    result: dict[str, Any],
    limit: int,
    *,
    rest: Reader | None = None,
    keep: Mapping[Path, int] | None = None,
) -> dict[str, Any]:
    """``result`` cut by its shape until it is within ``limit``. It always ends
    within it, and always as one object.

    1. Balance: the largest list (at any depth, a row's own list included) or
       long text is halved, again and again, down to its floor: one row
       (``keep[path]`` rows where given) for a list, :data:`TEXT_FLOOR`
       characters for a text. The last one halved then keeps as much as still
       fits.
    2. Cap: the texts inside the rows still kept are cut to the longest length
       that fits.
    3. Drop: lists not in ``keep`` give up their last rows, largest first, and
       then the kept lists do.
    4. Should even that not fit (an object of very many keys), the answer is
       replaced by one record that says so.

    Every cut is recorded under ``_truncated``: a list with ``total_items``, a
    text with ``total_chars``, the texts capped inside a list's rows as
    ``<list>[*]`` with ``max_chars``, each with the ``next`` call ``rest``
    names (null when no tool reads it). A list inside a row is named with the
    row's index (``sections.0.chairs``)."""
    reader: Reader = rest if rest is not None else (lambda path, kept: None)
    floors = dict(keep or {})
    places = list(_places(result))
    plan = _Plan(
        lists={path: len(v) for path, v in places if isinstance(v, list)},
        texts={path: len(v) for path, v in places if isinstance(v, str)},
        whole=frozenset(path for path, rows in floors.items() if rows >= WHOLE),
    )

    def fits() -> bool:
        return serialized_size(_render_plan(result, places, plan, reader)) <= limit

    def done() -> dict[str, Any]:
        return _render_plan(result, places, plan, reader)

    def floor(path: Path, whole: Any) -> int:
        if isinstance(whole, list):
            return min(floors.get(path, 1), len(whole))
        return TEXT_FLOOR

    def table(path: Path) -> dict[Path, int]:
        return plan.lists if path in plan.lists else plan.texts

    def settle(path: Path, low: int, high: int) -> None:
        """Keep the most at ``path`` in [low, high] that fits; low fits."""

        def fits_at(amount: int) -> bool:
            table(path)[path] = amount
            return fits()

        table(path)[path] = _largest_fitting(fits_at, low, high)

    if fits():
        return done()

    # 1. Balance: halve the largest place above its floor until it fits.
    while True:
        shown = _build(result, (), plan, False)
        candidates = [
            (serialized_size(_get(shown, path)), path, whole)
            for path, whole in places
            if table(path)[path] > floor(path, whole) and _get(shown, path) is not None
        ]
        if not candidates:
            break
        _, path, whole = max(candidates, key=lambda row: row[0])
        before = table(path)[path]
        table(path)[path] = max(floor(path, whole), before // 2)
        if fits():
            settle(path, table(path)[path], before - 1)
            return done()

    # 2. Cap the texts in the rows that are left.
    def capped(amount: int) -> bool:
        plan.cap = amount
        return fits()

    if capped(0):
        plan.cap = _largest_fitting(capped, 0, _longest_row_text(places))
        return done()

    # 3. Drop rows: the lists not kept first, largest first; then the kept ones.
    lists = [
        (path, whole)
        for path, whole in places
        if isinstance(whole, list) and not _nested(path)
    ]
    lists.sort(key=lambda row: (row[0] in floors, -serialized_size(row[1])))
    for path, _whole in lists:
        before = plan.lists[path]
        plan.lists[path] = 0
        if fits():
            settle(path, 0, before - 1)
            plan.cap = _largest_fitting(capped, 0, _longest_row_text(places))
            return done()

    # 4. Nothing of this shape fits: one record says so.
    return {
        TRUNCATED_KEY: {
            "*": {"total_chars": serialized_size(result), "next": reader((), 0)}
        }
    }


def section(answer: dict[str, Any], name: str, offset: int) -> dict[str, Any]:
    """One place of ``answer``, by the dotted name a ``_truncated`` key gives
    it (object keys, and row indexes in a list): a list's rows from ``offset``,
    a text from character ``offset``, or any other value whole. A name that
    reaches nothing is refused, never answered empty."""
    if offset < 0:
        raise ValueError("offset must not be negative")
    value: Any = answer
    for step in name.split("."):
        if isinstance(value, dict) and step in value and step != TRUNCATED_KEY:
            value = value[step]
        elif isinstance(value, list) and step.isdigit() and int(step) < len(value):
            value = value[int(step)]
        else:
            raise ValueError(f"no section {name!r} in this answer")
    if isinstance(value, list):
        return {
            "section": name,
            "offset": offset,
            "total_items": len(value),
            "items": value[offset:],
        }
    if isinstance(value, str):
        return {
            "section": name,
            "offset": offset,
            "total_chars": len(value),
            "text": value[offset:],
        }
    return {"section": name, "value": value}


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
