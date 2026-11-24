"""One log line per request, and an id on every response (H15).

The only per-request record was uvicorn's access line: a raw path and a
status, with no time, no account, no duration and nothing to tie it to an
error. So the log could not answer what an agent did or which request
failed.

Every HTTP request now leaves one line on the ``athena.request`` logger::

    request id=5f0c2a9be41d7c33 actor=7 method=POST route=/issues/{issue_id}/claim status=409 code=lease_held ms=3.1

``id`` is also the response's ``X-Request-Id`` header. A caller may send its
own (1 to 64 of ``A-Z a-z 0-9 . _ -``) to follow one request across systems;
anything else is replaced. ``actor`` is the account a token, the actor header
or a browser session resolved to. ``route`` is the route's template, never
the raw path, so no title, search term, token or probed URL reaches the log.
``code`` is the registered refusal code of a 4xx or 5xx JSON answer. A field
with nothing to say is ``-``.

An unhandled error is answered and logged here, once: a 500 with the
request's id in its header, and one line with that id and one traceback.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import traceback

from athena.core import refusals

HEADER = b"x-request-id"

_WORD = re.compile(r"[A-Za-z0-9._-]{1,64}")
# A refusal body is small; one that is not is logged without its code.
_MAX_REFUSAL_BYTES = 64 * 1024

_logger = logging.getLogger("athena.request")


def _request_id(scope) -> str:
    """The caller's id when it is one this log can carry, else a new one."""
    for name, value in scope.get("headers", ()):
        if name == HEADER:
            text = value.decode("latin-1")
            if _WORD.fullmatch(text):
                return text
            break
    return secrets.token_hex(8)


def _actor_id(scope) -> int | None:
    """The account the request resolved to: ``current_actor`` and the browser
    session each leave it in the request's state."""
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    actor_id = state.get("actor_id")
    if actor_id is None and isinstance(state.get("user"), dict):
        actor_id = state["user"].get("id")
    return actor_id


def _refusal_code(body: bytes) -> str | None:
    try:
        code = json.loads(body).get("code")
    except (ValueError, AttributeError):
        return None
    # A registered code is one word; anything else would break the line.
    return code if isinstance(code, str) and _WORD.fullmatch(code) else None


def _is_its_own_group(exc: BaseException, other: BaseException | None) -> bool:
    """Whether ``other`` is a task group's error that only wraps ``exc``."""
    return isinstance(other, BaseExceptionGroup) and any(
        inner is exc or _is_its_own_group(exc, inner) for inner in other.exceptions
    )


def format_error(exc: BaseException) -> str:
    """One traceback for ``exc``. The middleware stack raises a route's error
    out of a task group and names that group as the error's cause, though the
    group holds nothing but the error: every traceback printed three times.
    A cause the code itself named is kept."""
    printed = traceback.TracebackException.from_exception(exc)
    if _is_its_own_group(exc, exc.__cause__):
        printed.__cause__ = None
    if _is_its_own_group(exc, exc.__context__):
        printed.__context__ = None
    return "".join(printed.format()).rstrip()


class RequestLogMiddleware:
    """Outermost: it names and times every request, whichever layer answers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _request_id(scope)
        status = 0
        refusal: list[bytes] = []
        refusal_bytes = 0
        code = None

        async def send_with_id(message):
            nonlocal status, refusal_bytes, code
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() != HEADER
                ]
                headers.append((HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and status >= 400:
                chunk = message.get("body", b"")
                refusal_bytes += len(chunk)
                if refusal_bytes <= _MAX_REFUSAL_BYTES:
                    refusal.append(chunk)
                    if not message.get("more_body"):
                        code = _refusal_code(b"".join(refusal))
            await send(message)

        method = scope.get("method", "-")
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            if not status:
                # Answered here, not by the framework's own outermost layer,
                # which sits outside this one and would send no id.
                await refusals.internal_error()(scope, receive, send_with_id)
            _logger.error(
                "unhandled error id=%s method=%s route=%s\n%s",
                request_id,
                method,
                getattr(scope.get("route"), "path", None) or "-",
                format_error(exc),
            )
            raise
        finally:
            actor_id = _actor_id(scope)
            _logger.info(
                "request id=%s actor=%s method=%s route=%s status=%s code=%s ms=%.1f",
                request_id,
                "-" if actor_id is None else actor_id,
                method,
                getattr(scope.get("route"), "path", None) or "-",
                status or "-",
                code or "-",
                (time.perf_counter() - started) * 1000.0,
            )
