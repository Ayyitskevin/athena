"""The ambient RUN id for the current request.

A run is a unit of agent work (see migration 0029). A client tags the requests
that make up one run with an `X-Athena-Run` header; we stash that id here, in a
contextvar, for the duration of the request. `activity.record()` reads it and
stamps every event it writes — so NO recorder has to thread a run id through its
signature; the id rides alongside, request-scoped.

A contextvar (not a global) because requests run concurrently: each gets its own
view, set at the edge by the ASGI middleware and reset when the request ends, so
one request's run id can never leak into another's events.
"""

from __future__ import annotations

import contextvars

# The run id in force for the current request, or None when none was supplied.
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "athena_run_id", default=None
)

# An opaque client string; bound its length so a hostile/huge header can't bloat
# every activity row. Long enough for any UUID/ULID/job id a caller would use.
_MAX_RUN_ID_LEN = 200


def normalize(raw: str | None) -> str | None:
    """Clean a header value into a stored run id, or None. Empty/whitespace becomes
    None (untagged), and an over-long value is truncated rather than rejected — a run
    id is a correlation hint, not a security boundary, so we never fail the request
    over it."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    return value[:_MAX_RUN_ID_LEN]


def set_run_id(raw: str | None) -> contextvars.Token:
    """Set the current run id from a raw header value and return the reset token.
    The caller (the ASGI middleware) MUST reset with it when the request ends."""
    return _run_id.set(normalize(raw))


def reset_run_id(token: contextvars.Token) -> None:
    """Restore the previous run id, so the value never outlives its request."""
    _run_id.reset(token)


def get_run_id() -> str | None:
    """The run id in force for the current request, or None if untagged."""
    return _run_id.get()
