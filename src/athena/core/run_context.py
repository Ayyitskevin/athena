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
import unicodedata

# The run id in force for the current request, or None when none was supplied.
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "athena_run_id", default=None
)

# The PARENT run id — the run that spawned the current one (lineage). None for a
# top-level run, or any untagged request. Set/reset alongside _run_id by the
# middleware, so every event records both which run it is and which run begot it.
_parent_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "athena_parent_run_id", default=None
)

# The parent activity event after which the current run forked, or None for top-level
# runs, ordinary child runs, and untagged requests.
_forked_from_event_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "athena_forked_from_event_id", default=None
)

# An opaque client string; bound its length so a hostile/huge header can't bloat
# every activity row. Long enough for any UUID/ULID/job id a caller would use.
_MAX_RUN_ID_LEN = 200


def strict_run_id(raw: object) -> str:
    """Return a canonical persisted run id, or reject invalid input.

    Ordinary correlation headers remain forgiving through :func:`normalize`: an
    overlong header is merely truncated. A persistent check-in is an addressable row,
    so truncation could alias two caller ids; that boundary uses this strict form.
    """
    if not isinstance(raw, str):
        raise ValueError("run_id must be a string")
    # Validate before trimming so a trailing newline/tab cannot be normalized into
    # an apparently safe identifier. ``isprintable`` also rejects lone surrogates,
    # bidi controls, zero-width format characters, and line/paragraph separators
    # while preserving ordinary spaces, visible CJK, emoji, and combining marks.
    if not all(char.isprintable() for char in raw):
        raise ValueError("run_id must contain only printable characters")
    value = unicodedata.normalize("NFC", raw.strip())
    if not value:
        raise ValueError("run_id is required")
    if len(value) > _MAX_RUN_ID_LEN:
        raise ValueError(f"run_id must be at most {_MAX_RUN_ID_LEN} characters")
    return value


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


def set_parent_run_id(raw: str | None) -> contextvars.Token:
    """Set the current parent run id from a raw header value; return the reset token.
    The caller (the ASGI middleware) MUST reset with it when the request ends."""
    return _parent_run_id.set(normalize(raw))


def reset_parent_run_id(token: contextvars.Token) -> None:
    """Restore the previous parent run id, so it never outlives its request."""
    _parent_run_id.reset(token)


def get_parent_run_id() -> str | None:
    """The parent run id in force for the current request, or None at top level."""
    return _parent_run_id.get()


def normalize_event_id(raw: str | int | None) -> int | None:
    """Clean a raw fork-point event id into a positive integer, or None.

    The header is metadata, not authorization, so malformed values simply mean
    "unknown fork point" rather than failing an otherwise valid write. Callers that
    want a guaranteed-valid fork should first ask the fork-contract endpoint, which
    validates the event against the visible parent run and returns a safe header set.
    """
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def set_forked_from_event_id(raw: str | int | None) -> contextvars.Token:
    """Set the current fork-point event id from a raw header value; return the reset
    token. The caller MUST reset with it when the request ends."""
    return _forked_from_event_id.set(normalize_event_id(raw))


def reset_forked_from_event_id(token: contextvars.Token) -> None:
    """Restore the previous fork-point event id."""
    _forked_from_event_id.reset(token)


def get_forked_from_event_id() -> int | None:
    """The fork-point event id in force for the current request, if any."""
    return _forked_from_event_id.get()
