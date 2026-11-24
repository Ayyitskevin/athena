"""The opaque cursor a keyset-paged list hands back (decision D2).

A list that pages in D2's vocabulary takes ``cursor`` and answers
``next_cursor`` and ``has_more``. The cursor is the sort key of the last row
the previous page returned, so the next page starts strictly after that row in
the list's total order: a row added or removed between two calls never shifts
the rows that follow, and no row is repeated or skipped because of it (an
offset counts rows, so one insertion ahead of it repeats a row).

The key is written as compact JSON, tagged with the list it belongs to (and,
for a sorted list, the sort), and base64url-encoded without padding. Callers
pass it back unchanged and never read it: its shape is this module's to
change. A cursor this module did not write for that list, including one from
another list or another sort, is refused rather than read as the first page.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from athena.core.ids import MAX_SQLITE_INTEGER

#: The characters a cursor may take on the query string. A key holds at most a
#: title, a timestamp and an id, so real cursors are far shorter.
MAX_CURSOR_CHARS = 8_192

#: What a list answers when a cursor is not one it handed out.
REFUSAL_DETAIL = "cursor is not one this list returned; omit it for the first page"


class CursorError(ValueError):
    """The cursor was not written by :func:`encode` for this list."""


def encode(tag: str, key: list[Any]) -> str:
    """The cursor that resumes ``tag``'s list after the row whose sort key is
    ``key`` (its columns in ORDER BY order, the row id last)."""
    text = json.dumps([tag, *key], separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def decode(cursor: str, tag: str, types: tuple[type, ...]) -> list[Any]:
    """The sort key ``cursor`` names for ``tag``'s list, one value per entry of
    ``types`` (int or str). Raises :class:`CursorError` for anything else."""
    if not cursor or len(cursor) > MAX_CURSOR_CHARS or not cursor.isascii():
        raise CursorError(REFUSAL_DETAIL)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise CursorError(REFUSAL_DETAIL) from exc
    if not isinstance(value, list) or len(value) != len(types) + 1 or value[0] != tag:
        raise CursorError(REFUSAL_DETAIL)
    key = value[1:]
    for item, kind in zip(key, types, strict=True):
        # bool is an int to Python; a key never holds one. An int SQLite cannot
        # bind would be a 500 from the driver, not a refusal.
        if type(item) is not kind:
            raise CursorError(REFUSAL_DETAIL)
        if isinstance(item, int) and not (
            -MAX_SQLITE_INTEGER - 1 <= item <= MAX_SQLITE_INTEGER
        ):
            raise CursorError(REFUSAL_DETAIL)
    return key
