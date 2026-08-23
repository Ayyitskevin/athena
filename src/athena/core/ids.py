"""The bounds every surrogate row id is held to, in one place.

SQLite stores INTEGER as a signed 64-bit value, and the ``sqlite3`` driver
raises ``OverflowError`` — not a clean refusal — when asked to bind anything
larger. A route that accepts an unbounded ``int`` therefore turns
``GET /workers/9223372036854775808`` into a 500 from deep inside the driver,
where every other out-of-range input on the same surface is a tidy 422.

The adversarial review found this first on ``/run-controls`` and it was fixed
there; a later review found the same class still open on that endpoint's
``worker_id`` body field, which is what makes the case for a SHARED bound
rather than a per-route literal. ``MAX_SQLITE_INTEGER`` was already declared
three times across ``aegis`` (issues, fleet_metrics, dispatch) — those keep
their names as re-exports so nothing outside changes, and every declaration now
resolves to this one value.

Use the annotated aliases on transport signatures: ``RowIdPath`` for a path
parameter (``def show(worker_id: RowIdPath) -> dict``) and ``RowIdQuery`` for a
query one (``def feed(after_id: RowIdQuery | None = None) -> list``).

(Those examples deliberately carry no ``...`` stub body. Coverage's default
exclusion list matches a stub body by LINE TEXT, without knowing the line sits
inside a docstring — an ellipsis here silently excludes this whole module
docstring and trips the repo's excluded-line-count gate.)

Both refuse zero and negatives too: a surrogate id is a positive integer, and
``0`` reaching a query is a caller bug worth a 422 rather than an empty result
that reads like "nothing found".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path, Query

#: The largest value SQLite's INTEGER can hold, and therefore the largest id
#: the driver can bind without raising.
MAX_SQLITE_INTEGER = (1 << 63) - 1

#: A surrogate row id in the URL path.
RowIdPath = Annotated[int, Path(ge=1, le=MAX_SQLITE_INTEGER)]

#: A surrogate row id in the query string (cursors, filters, references).
RowIdQuery = Annotated[int, Query(ge=1, le=MAX_SQLITE_INTEGER)]
