"""W3C trace context on writes: a correlation hint stored beside the run headers.

A seat that writes to Athena usually writes spans somewhere else too. The run
headers (run_context) say which Athena run an event belongs to; `traceparent`
and `tracestate` say which trace the same action belongs to, so an operator can
jump from an Athena event into whatever tracing stack the agent writes to.

The pair has exactly the standing docs/RUNS.md gives the lineage headers:

* **A hint, never authorization.** Nothing reads it to decide who may do what.
  It is captured at the request edge into a contextvar (like the run id) and
  appended to the event detail by the single writer, after every gate has run.
* **Dropped, never refused.** A malformed value is dropped with a logged reason
  that names the header and the rule, never the value (a header is attacker
  controlled text), and the write proceeds as if the header were absent.
* **Stored in the detail, under a marker.** No schema change: the pair rides at
  the end of `detail` as ``; trace_context_v1 {"traceparent":...}``, the same
  idiom as the completion report, so the hash chain covers it and old rows are
  untouched. :func:`split_detail` is how every reader of structured detail sees
  through the suffix.

Validation follows the W3C Trace Context recommendation: `traceparent` is
`version-traceid-parentid-flags` in lowercase hex, version ``ff`` is reserved,
version ``00`` has exactly four fields (a higher version is kept to its first
four), and an all-zero trace or parent id is invalid. `tracestate` is up to 32
``key=value`` members, 512 characters in all, and is only meaningful beside a
valid `traceparent`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re

_logger = logging.getLogger("athena")

TRACE_CONTEXT_MARKER = "; trace_context_v1 "
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"
MAX_TRACESTATE_MEMBERS = 32
MAX_TRACESTATE_CHARS = 512
# Anything longer is refused before it is looked at: a header this size is not a
# trace, and parsing it would only cost time.
MAX_HEADER_CHARS = 2048

_HEX2 = re.compile(r"^[0-9a-f]{2}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_PARENT_ID = re.compile(r"^[0-9a-f]{16}$")
_SIMPLE_KEY = re.compile(r"^[a-z][a-z0-9_\-*/]{0,255}$")
_TENANT_KEY = re.compile(r"^[a-z0-9][a-z0-9_\-*/]{0,240}@[a-z][a-z0-9_\-*/]{0,13}$")
# 1 to 256 printable ASCII characters, no comma, no equals, no trailing space.
_VALUE = re.compile(
    r"^[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]$"
)

# The trace in force for the current request, or None. A contextvar for the same
# reason run_context uses one: set at the edge, reset when the request ends, so
# one request's trace can never leak into another's events.
_trace: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "athena_trace_context", default=None
)


def _check_traceparent(raw: object) -> tuple[str | None, str | None]:
    """(canonical value, None) or (None, reason). An absent or empty header is
    (None, None): nothing to keep, nothing to log."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "is not text"
    value = raw.strip()
    if not value:
        return None, None
    if len(value) > MAX_HEADER_CHARS:
        return None, "is longer than the header limit"
    parts = value.split("-")
    if len(parts) < 4:
        return None, "is not version-traceid-parentid-flags"
    version, trace_id, parent_id, flags = parts[:4]
    if not (_HEX2.match(version) and _HEX2.match(flags)):
        return None, "has a version or flags field that is not two lowercase hex digits"
    if not _TRACE_ID.match(trace_id):
        return None, "has a trace id that is not 32 lowercase hex digits"
    if not _PARENT_ID.match(parent_id):
        return None, "has a parent id that is not 16 lowercase hex digits"
    if version == "ff":
        return None, "uses the reserved version ff"
    if version == "00" and len(parts) != 4:
        return None, "has extra fields on version 00"
    if set(trace_id) == {"0"}:
        return None, "has an all-zero trace id"
    if set(parent_id) == {"0"}:
        return None, "has an all-zero parent id"
    return "-".join(parts[:4]), None


def _check_tracestate(raw: object) -> tuple[str | None, str | None]:
    """(normalized value, None) or (None, reason); absent or empty is (None, None)."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "is not text"
    if not raw.strip():
        return None, None
    if len(raw) > MAX_HEADER_CHARS:
        return None, "is longer than the header limit"
    members: list[str] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        member = piece.strip()
        if not member:
            continue
        key, equals, value = member.partition("=")
        if not equals:
            return None, "has a member without a value"
        if not (_SIMPLE_KEY.match(key) or _TENANT_KEY.match(key)):
            return None, "has a member with an invalid key"
        if not _VALUE.match(value):
            return None, "has a member with an invalid value"
        if key in seen:
            return None, "repeats a key"
        seen.add(key)
        members.append(f"{key}={value}")
    if not members:
        return None, "has no members"
    if len(members) > MAX_TRACESTATE_MEMBERS:
        return None, f"has more than {MAX_TRACESTATE_MEMBERS} members"
    joined = ",".join(members)
    if len(joined) > MAX_TRACESTATE_CHARS:
        return None, f"is longer than {MAX_TRACESTATE_CHARS} characters"
    return joined, None


def parse_traceparent(raw: object) -> str | None:
    """The canonical `traceparent`, or None when absent or malformed."""
    return _check_traceparent(raw)[0]


def parse_tracestate(raw: object) -> str | None:
    """The normalized `tracestate`, or None when absent or malformed."""
    return _check_tracestate(raw)[0]


def validate(traceparent_raw: object, tracestate_raw: object) -> dict[str, str] | None:
    """The trace to store for this pair of raw header values, or None.

    Every drop is logged with the header and the rule it broke, never the value."""
    parent, reason = _check_traceparent(traceparent_raw)
    if reason is not None:
        _logger.warning("trace context dropped: %s %s", TRACEPARENT_HEADER, reason)
    state, state_reason = _check_tracestate(tracestate_raw)
    if state_reason is not None:
        _logger.warning("trace context dropped: %s %s", TRACESTATE_HEADER, state_reason)
    if parent is None:
        if state is not None:
            _logger.warning(
                "trace context dropped: %s without a valid %s",
                TRACESTATE_HEADER,
                TRACEPARENT_HEADER,
            )
        return None
    trace = {TRACEPARENT_HEADER: parent}
    if state is not None:
        trace[TRACESTATE_HEADER] = state
    return trace


def set_client_trace(
    traceparent_raw: object, tracestate_raw: object
) -> contextvars.Token:
    """Set the trace for the current request from the raw headers (validated,
    drops logged) and return the reset token. The caller MUST reset with it."""
    return _trace.set(validate(traceparent_raw, tracestate_raw))


def reset_trace(token: contextvars.Token) -> None:
    """Restore the previous trace, so a trace never outlives its request."""
    _trace.reset(token)


def get_trace() -> dict[str, str] | None:
    """The trace in force for the current request, or None."""
    return _trace.get()


def attach(detail: str) -> str:
    """The detail to store: the caller's detail plus the trace suffix when a
    trace is in force. Called by the single event writer for native rows only."""
    trace = _trace.get()
    if not trace:
        return detail
    return (
        detail
        + TRACE_CONTEXT_MARKER
        + json.dumps(trace, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def split_detail(detail: str | None) -> tuple[str, dict[str, str] | None]:
    """(the detail as the recorder wrote it, the trace) for a stored detail.

    A detail with no well-formed suffix is returned whole with None: a marker
    that happens to occur inside ordinary text, or a payload that does not parse
    as a valid pair, is just text. The suffix is the LAST marker in the detail."""
    if not detail:
        return detail or "", None
    plain, separator, payload = detail.rpartition(TRACE_CONTEXT_MARKER)
    if not separator:
        return detail, None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return detail, None
    if not isinstance(parsed, dict) or not set(parsed) <= {
        TRACEPARENT_HEADER,
        TRACESTATE_HEADER,
    }:
        return detail, None
    parent = parsed.get(TRACEPARENT_HEADER)
    if parse_traceparent(parent) != parent:
        return detail, None
    state = parsed.get(TRACESTATE_HEADER)
    if TRACESTATE_HEADER in parsed and parse_tracestate(state) != state:
        return detail, None
    return plain, parsed


def plain_detail(detail: str | None) -> str:
    """Template filter: the detail without its trace suffix."""
    return split_detail(detail)[0]


def trace_hint(detail: str | None) -> dict[str, str] | None:
    """Template filter: the trace recorded with the event, or None."""
    return split_detail(detail)[1]
