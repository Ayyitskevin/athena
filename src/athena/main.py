"""The Athena web application: a thin FastAPI layer over core and the modules.

`create_app()` is an *application factory* — call it to build a fresh app.
Tests call it with a throwaway database; the server calls it once for real.
No global app state, no surprises.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from athena import config
from athena.aegis import api as aegis_api
from athena.aegis import automation as aegis_automation
from athena.aegis import automation_api as aegis_automation_api
from athena.aegis import delegations_api as aegis_delegations_api
from athena.aegis import filters_api as aegis_filters_api
from athena.aegis import fleet_metrics_api as aegis_fleet_metrics_api
from athena.aegis import fleet_work_api as aegis_fleet_work_api
from athena.aegis import sprints_api as aegis_sprints_api
from athena.aegis import work_context_api as aegis_work_context_api
from athena.core import (
    activity,
    agent_runs_api,
    activity_api,
    approvals,
    approvals_api,
    attachments_api,
    budgets,
    db,
    identity,
    security_events,
    events_api,
    idempotency,
    notifications,
    notifications_api,
    rate_limits,
    run_context,
    search_api,
    security_api,
    sessions,
    tokens,
    tokens_api,
    undo,
    users,
    users_api,
    webhooks,
    webhooks_api,
    workers_api,
)
from athena.aegis import issue_undo as aegis_issue_undo
from athena.mentor import api as mentor_api
from athena.mentor import learnings_api as mentor_learnings_api
from athena.mentor import page_undo as mentor_page_undo
from athena.web import activity as web_activity
from athena.web import admin as web_admin
from athena.web import auth as web_auth
from athena.web import boards as web_boards
from athena.web import filters as web_filters
from athena.web import fleet_metrics as web_fleet_metrics
from athena.web import labels as web_labels
from athena.web import mentor as web_mentor
from athena.web import projects as web_projects
from athena.web import work_context as web_work_context
from athena.web import init_templates, router as web_router


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

_logger = logging.getLogger("athena")


def _configure_logging() -> None:
    """Make Athena's own logs visible without depending on the caller's setup.

    Sets the "athena" logger to config.LOG_LEVEL and attaches a stream handler once (the
    guard keeps the many apps a test suite builds from stacking handlers). An
    unconfigured root logger otherwise only surfaces WARNING+, hiding the startup and
    background-loop INFO lines this observability pass adds. Records still propagate, so
    pytest's caplog and any host logging config keep seeing them; under uvicorn (which
    adds no root handler) this is the single emitter, so no double lines."""
    _logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    if not _logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        _logger.addHandler(handler)


# Emit the "cookies are not Secure" warning at most once per process, no matter how
# many apps a test suite builds. Flipped true the first time create_app() warns.
_cookie_secure_warned = False


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies even when Content-Length is absent.

    The header check catches honest clients before any body is read. The receive
    pre-read is the real enforcement path for chunked or otherwise streamed
    requests, where there may be no trustworthy Content-Length header at all. It
    buffers only up to the configured cap, then replays the body downstream.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                content_length = int(value.decode("ascii"))
            except ValueError:
                await _send_json_response(
                    send, {"detail": "invalid content-length"}, status_code=400
                )
                return
            if content_length > self.max_bytes:
                await _send_json_response(
                    send, {"detail": "request body too large"}, status_code=413
                )
                return
            break

        consumed = 0
        messages = []
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    await _send_json_response(
                        send, {"detail": "request body too large"}, status_code=413
                    )
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(messages):
                message = messages[replay_index]
                replay_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


class RunContextMiddleware:
    """Capture run headers into the request-scoped run context, so every activity
    event recorded while handling this request is stamped with that run metadata.

    A pure-ASGI middleware (not @app.middleware) ON PURPOSE: it runs in the same task
    as the endpoint, so the contextvar it sets reliably propagates into the handler
    (including sync handlers run in the threadpool, which copy the current context) —
    the propagation that BaseHTTPMiddleware does not guarantee. Tokens are reset in
    a finally so run metadata never outlives its request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        run_raw = None
        parent_raw = None
        forked_from_raw = None
        for name, value in scope.get("headers", []):
            lname = name.lower()
            # Decode as UTF-8 (errors replaced, never raised) to match how Starlette
            # decodes the ?run_id= / ?parent_run_id= query params used to replay and
            # walk lineage — so a run id stored from a header matches when filtered
            # back. latin-1 here would make a non-ASCII id round-trip as mojibake.
            if lname == b"x-athena-run":
                run_raw = value.decode("utf-8", "replace")
            elif lname == b"x-athena-parent-run":
                parent_raw = value.decode("utf-8", "replace")
            elif lname == b"x-athena-fork-from-event":
                forked_from_raw = value.decode("utf-8", "replace")
        # The run id comes straight from an untrusted client header, so a
        # server-reserved namespace (e.g. the automation engine's firing ids) is
        # dropped here — otherwise a client could pre-stamp a rule's predictable
        # firing run id and silently suppress it. Parent/fork are pointers into
        # existing runs (forking from an automation run is legitimate), so they
        # are not reserved.
        run_token = run_context.set_client_run_id(run_raw)
        parent_token = run_context.set_parent_run_id(parent_raw)
        forked_from_token = run_context.set_forked_from_event_id(forked_from_raw)
        try:
            await self.app(scope, receive, send)
        finally:
            run_context.reset_forked_from_event_id(forked_from_token)
            run_context.reset_parent_run_id(parent_token)
            run_context.reset_run_id(run_token)


# Mutating REST APIs that support durable Idempotency-Key semantics. Browser and
# session endpoints are deliberately outside this contract: replaying HTML, CSRF
# state, redirects, or cookies would be unsafe. New API roots must opt in here.
_IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_IDEMPOTENCY_API_ROOTS = (
    "/attachments",
    "/automation",
    "/filters",
    "/issues",
    "/labels",
    "/notifications",
    "/pages",
    "/projects",
    "/spaces",
    "/sprints",
    "/tokens",
    "/users",
    "/watches",
    "/webhooks",
)
_SECRET_CREATE_PATHS = frozenset(
    {
        "/tokens",
        "/users/onboard_agent",
        "/webhooks",
        "/settings/tokens",
        "/admin/webhooks",
    }
)
_FINGERPRINT_HEADERS = (
    b"accept",
    b"content-encoding",
    b"content-type",
    b"if-match",
    b"if-unmodified-since",
    b"x-athena-fork-from-event",
    b"x-athena-parent-run",
    b"x-athena-run",
)
_REPLAYABLE_RESPONSE_HEADERS = frozenset(
    {
        b"cache-control",
        b"content-language",
        b"content-type",
        b"etag",
        b"last-modified",
        b"location",
    }
)
_MAX_IDEMPOTENCY_KEY_BYTES = 255
_MAX_STORED_HEADER_COUNT = 64
_MAX_STORED_HEADERS_BYTES = 16 * 1024


class _IdempotencyCaptureError(RuntimeError):
    """The downstream response cannot be stored and replayed safely."""


class IdempotencyMiddleware:
    """Durably single-flight mutating API retries across workers and restarts.

    A bounded, authenticated request first commits an executing ownership row.
    Exactly one owner runs the route; concurrent identical requests briefly wait
    and then replay its completed result. The owner buffers a successful response,
    commits that response to SQLite, and only then sends the first client byte.

    Domain mutations still use a different connection from the ownership row. A
    crash between the domain commit and response finalization is therefore
    unknowable. Athena never steals such an expired claim: retries receive an
    explicit indeterminate 409 until an operator reconciles it.
    """

    def __init__(
        self,
        app,
        db_path,
        *,
        wait_seconds: float,
        lease_seconds: int,
        ttl_seconds: int,
        max_response_bytes: int,
    ):
        self.app = app
        self.db_path = db_path
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.lease_seconds = max(1, int(lease_seconds))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_response_bytes = max(1, int(max_response_bytes))

    @staticmethod
    def _is_api_path(path: str) -> bool:
        return any(
            path == root or path.startswith(root + "/")
            for root in _IDEMPOTENCY_API_ROOTS
        )

    @staticmethod
    def _headers(scope) -> dict[bytes, bytes]:
        return {name.lower(): value for name, value in scope.get("headers", [])}

    @staticmethod
    def _header_values(scope, wanted: bytes) -> list[bytes]:
        return [
            value for name, value in scope.get("headers", []) if name.lower() == wanted
        ]

    @staticmethod
    def _run_db(db_path, operation, **kwargs):
        conn = db.connect(db_path)
        try:
            return operation(conn, **kwargs)
        finally:
            conn.close()

    async def _authenticate(
        self, headers: dict[bytes, bytes]
    ) -> tuple[str | None, int | None, bool]:
        """Return a canonical live credential identity, optional token id, and
        whether that credential's account is currently paused."""
        authorization = headers.get(b"authorization")
        if authorization is not None and authorization.lower().startswith(b"bearer "):
            raw = authorization[len(b"bearer ") :].strip().decode("latin-1")
            actor = await asyncio.to_thread(self._resolve_token, raw)
            if actor is None:
                return None, None, False
            return (
                "tok:" + tokens._hash(raw),
                int(actor["_token_id"]),
                bool(actor.get("paused_at")),
            )

        if config.TRUST_ACTOR_HEADER:
            raw_actor = headers.get(b"x-athena-actor")
            if raw_actor is not None:
                try:
                    actor_id = int(raw_actor.decode("latin-1"))
                except ValueError:
                    return None, None, False
                actor = await asyncio.to_thread(self._resolve_actor, actor_id)
                if actor is not None:
                    return f"actor:{actor_id}", None, bool(actor.get("paused_at"))
        return None, None, False

    def _resolve_token(self, raw: str) -> dict | None:
        conn = db.connect(self.db_path)
        try:
            return tokens.resolve_token(conn, raw)
        finally:
            conn.close()

    def _resolve_actor(self, actor_id: int) -> dict | None:
        conn = db.connect(self.db_path)
        try:
            return users.get_user(conn, actor_id)
        finally:
            conn.close()

    def _has_users(self) -> bool:
        conn = db.connect(self.db_path)
        try:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
        finally:
            conn.close()

    @staticmethod
    async def _read_body(receive):
        messages: list[dict] = []
        body_parts: list[bytes] = []
        disconnected = False
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                disconnected = True
                break

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(messages):
                message = messages[replay_index]
                replay_index += 1
                return message
            return await receive()

        return b"".join(body_parts), replay_receive, disconnected

    @classmethod
    def _fingerprint(cls, scope, body: bytes) -> str:
        digest = hashlib.sha256()

        def add(label: bytes, value: bytes) -> None:
            digest.update(len(label).to_bytes(2, "big"))
            digest.update(label)
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)

        add(b"version", b"athena-idempotency-v2")
        add(b"method", scope.get("method", "").encode("ascii"))
        raw_path = scope.get("raw_path")
        if raw_path is None:
            raw_path = scope.get("path", "").encode("utf-8")
        add(b"path", raw_path)
        add(b"query", scope.get("query_string", b""))
        for header_name in _FINGERPRINT_HEADERS:
            values = cls._header_values(scope, header_name)
            add(header_name + b"-count", str(len(values)).encode("ascii"))
            for value in values:
                add(header_name, value)
        add(b"body-sha256", hashlib.sha256(body).digest())
        return digest.hexdigest()

    async def _claim(
        self,
        *,
        key: str,
        identity: str,
        method: str,
        path: str,
        fingerprint: str,
    ) -> idempotency.ClaimResult:
        return await asyncio.to_thread(
            self._run_db,
            self.db_path,
            idempotency.claim_or_read,
            key=key,
            identity=identity,
            method=method,
            path=path,
            request_fingerprint=fingerprint,
            lease_seconds=self.lease_seconds,
        )

    async def _mark_indeterminate(
        self,
        *,
        key: str,
        identity: str,
        owner_token: str,
        failure_code: str,
    ) -> None:
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    self._run_db,
                    self.db_path,
                    idempotency.mark_indeterminate,
                    key=key,
                    identity=identity,
                    owner_token=owner_token,
                    failure_code=failure_code,
                )
            )
        except BaseException:
            # The durable executing row itself is already fail-closed. If the
            # database is unavailable, leave it to age into the same outcome.
            _logger.exception("could not mark idempotency key indeterminate")

    @staticmethod
    def _serialize_headers(headers: list[tuple[bytes, bytes]]) -> str:
        selected = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in headers
            if name.lower() in _REPLAYABLE_RESPONSE_HEADERS
        ]
        if len(selected) > _MAX_STORED_HEADER_COUNT:
            raise _IdempotencyCaptureError("too many replayable response headers")
        encoded = json.dumps(selected, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_STORED_HEADERS_BYTES:
            raise _IdempotencyCaptureError("replayable response headers are too large")
        return encoded

    async def __call__(self, scope, receive, send):
        method = scope.get("method")
        if scope["type"] != "http" or method not in _IDEMPOTENT_METHODS:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_secret_create = method == "POST" and path in _SECRET_CREATE_PATHS
        key_values = self._header_values(scope, b"idempotency-key")
        if not key_values:
            await self.app(scope, receive, send)
            return
        if not self._is_api_path(path) and not is_secret_create:
            await _send_json_response(
                send,
                {"detail": "Idempotency-Key is not supported for this endpoint"},
                status_code=400,
            )
            return
        if len(key_values) != 1:
            await _send_json_response(
                send,
                {"detail": "exactly one Idempotency-Key header is required"},
                status_code=400,
            )
            return
        key_bytes = key_values[0]
        if (
            not key_bytes
            or len(key_bytes) > _MAX_IDEMPOTENCY_KEY_BYTES
            or any(value < 0x21 or value > 0x7E for value in key_bytes)
        ):
            await _send_json_response(
                send,
                {"detail": ("Idempotency-Key must be 1-255 visible ASCII characters")},
                status_code=400,
            )
            return
        key = key_bytes.decode("ascii")
        if is_secret_create:
            await _send_json_response(
                send,
                {
                    "detail": (
                        "Idempotency-Key is not supported for endpoints that "
                        "return a one-time secret"
                    )
                },
                status_code=400,
            )
            return
        headers = self._headers(scope)
        if (
            len(self._header_values(scope, b"authorization")) > 1
            or len(self._header_values(scope, b"x-athena-actor")) > 1
        ):
            await _send_json_response(
                send,
                {"detail": "ambiguous authentication headers"},
                status_code=400,
            )
            return

        try:
            identity, token_id, paused = await self._authenticate(headers)
        except sqlite3.Error:
            _logger.exception("could not authenticate idempotent request")
            await _send_json_response(
                send,
                {"detail": "idempotency service unavailable"},
                status_code=503,
                extra_headers={"Retry-After": "1"},
            )
            return
        if paused:
            # A paused account is authenticated but deliberately frozen: pause
            # "refuses every authenticated action", and reading a stored receipt
            # is an authenticated action. Skip idempotency processing entirely —
            # no claim, no replay, no receipt disclosure — and let the route's
            # identity gate produce the canonical bounded 403 + audit event,
            # exactly as it does for the same request without a key. Stored
            # responses are additionally revision-fenced when the pause state
            # flips (migration 0061), so a later resume cannot replay a body
            # committed under pre-pause authorization.
            await self.app(scope, receive, send)
            return
        if identity is None:
            # First-user bootstrap uniquely permits an anonymous/invalid caller;
            # refuse its key so that path cannot mutate while silently bypassing
            # retry protection. Ordinary protected routes keep their established
            # 401 by reaching the normal dependency.
            if method == "POST" and path == "/users":
                try:
                    has_users = await asyncio.to_thread(self._has_users)
                except sqlite3.Error:
                    _logger.exception("could not inspect user bootstrap state")
                    await _send_json_response(
                        send,
                        {"detail": "idempotency service unavailable"},
                        status_code=503,
                        extra_headers={"Retry-After": "1"},
                    )
                    return
                if not has_users:
                    await _send_json_response(
                        send,
                        {
                            "detail": (
                                "Idempotency-Key requires an authenticated API "
                                "credential for user creation"
                            )
                        },
                        status_code=400,
                    )
                    return
            await self.app(scope, receive, send)
            return

        if token_id is not None:
            limiter = getattr(scope["app"].state, "token_rate_limiter", None)
            if limiter is not None:
                decision = limiter.check(token_id)
                if not decision.allowed:
                    await _send_json_response(
                        send,
                        {"detail": "token rate limit exceeded"},
                        status_code=429,
                        extra_headers={
                            "Retry-After": str(decision.retry_after_seconds),
                            "X-RateLimit-Limit": str(decision.limit),
                            "X-RateLimit-Remaining": str(decision.remaining),
                        },
                    )
                    return
            scope.setdefault("state", {})[
                "_athena_idempotency_rate_limited_token_id"
            ] = token_id

        body, replay_receive, disconnected = await self._read_body(receive)
        if disconnected:
            await self.app(scope, replay_receive, send)
            return
        fingerprint = self._fingerprint(scope, body)

        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        poll_delay = 0.025
        while True:
            try:
                claim = await self._claim(
                    key=key,
                    identity=identity,
                    method=method,
                    path=path,
                    fingerprint=fingerprint,
                )
            except sqlite3.Error:
                _logger.exception("could not claim idempotency key")
                await _send_json_response(
                    send,
                    {"detail": "idempotency service unavailable"},
                    status_code=503,
                    extra_headers={"Retry-After": "1"},
                )
                return

            if claim.kind == "owner":
                if claim.record is None:
                    raise RuntimeError("idempotency owner claim is missing its record")
                owner_token = claim.record["owner_token"]
                break
            if claim.kind == "replay":
                if claim.record is None:
                    raise RuntimeError("idempotency replay claim is missing its record")
                await self._replay(send, claim.record)
                return
            if claim.kind == "mismatch":
                await _send_json_response(
                    send,
                    {
                        "detail": "Idempotency-Key reused for a different request",
                        "code": "idempotency_mismatch",
                    },
                    status_code=409,
                )
                return
            if claim.kind == "authorization_changed":
                await _send_json_response(
                    send,
                    {
                        "detail": (
                            "Authorization changed after this Idempotency-Key "
                            "was claimed; its stored response cannot be replayed"
                        ),
                        "code": "idempotency_authorization_changed",
                    },
                    status_code=409,
                )
                return
            if claim.kind == "indeterminate":
                await _send_json_response(
                    send,
                    {
                        "detail": (
                            "The earlier request outcome is indeterminate; "
                            "reconcile it before reusing this Idempotency-Key"
                        ),
                        "code": "idempotency_indeterminate",
                    },
                    status_code=409,
                )
                return

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _send_json_response(
                    send,
                    {
                        "detail": (
                            "A request with this Idempotency-Key is still in progress"
                        ),
                        "code": "idempotency_in_progress",
                    },
                    status_code=409,
                    extra_headers={"Retry-After": "1"},
                )
                return
            await asyncio.sleep(min(poll_delay, remaining))
            poll_delay = min(0.25, poll_delay * 2)

        captured: dict = {
            "status": None,
            "headers": [],
            "body": [],
            "size": 0,
            "finished": False,
        }

        async def capture_send(message):
            if message["type"] == "http.response.start":
                if captured["status"] is not None:
                    raise _IdempotencyCaptureError("duplicate response start")
                captured["status"] = int(message["status"])
                captured["headers"] = list(message.get("headers", []))
                return
            if message["type"] != "http.response.body":
                raise _IdempotencyCaptureError("unsupported ASGI response message")
            if captured["status"] is None:
                raise _IdempotencyCaptureError("response body preceded response start")
            chunk = message.get("body", b"")
            captured["size"] += len(chunk)
            if captured["size"] > self.max_response_bytes:
                raise _IdempotencyCaptureError("response body is too large to replay")
            captured["body"].append(chunk)
            if not message.get("more_body", False):
                captured["finished"] = True

        try:
            await self.app(scope, replay_receive, capture_send)
            if captured["status"] is None or not captured["finished"]:
                raise _IdempotencyCaptureError("incomplete ASGI response")
        except BaseException:
            await self._mark_indeterminate(
                key=key,
                identity=identity,
                owner_token=owner_token,
                failure_code="execution_failed",
            )
            raise

        status_code = captured["status"]
        response_body = b"".join(captured["body"])
        response_headers = captured["headers"]

        if 200 <= status_code < 300:
            try:
                serialized_headers = self._serialize_headers(response_headers)
                content_type = next(
                    (
                        value.decode("latin-1")
                        for name, value in response_headers
                        if name.lower() == b"content-type"
                    ),
                    None,
                )
                completion = await asyncio.to_thread(
                    self._run_db,
                    self.db_path,
                    idempotency.complete,
                    key=key,
                    identity=identity,
                    owner_token=owner_token,
                    status_code=status_code,
                    content_type=content_type,
                    response_headers=serialized_headers,
                    response_body=response_body,
                    ttl_seconds=self.ttl_seconds,
                )
                if completion == "lost":
                    raise RuntimeError(
                        "idempotency ownership was lost before completion"
                    )
                if completion not in {"completed", "authorization_changed"}:
                    raise RuntimeError("invalid idempotency completion state")
            except BaseException:
                await self._mark_indeterminate(
                    key=key,
                    identity=identity,
                    owner_token=owner_token,
                    failure_code="finalization_failed",
                )
                raise
            await self._send_captured(
                send,
                status_code=status_code,
                headers=response_headers,
                body=response_body,
            )
            return

        if status_code >= 500:
            await self._mark_indeterminate(
                key=key,
                identity=identity,
                owner_token=owner_token,
                failure_code="server_error_response",
            )
        else:
            try:
                await asyncio.to_thread(
                    self._run_db,
                    self.db_path,
                    idempotency.release,
                    key=key,
                    identity=identity,
                    owner_token=owner_token,
                )
            except sqlite3.Error:
                # The response itself is still valid. A stranded claim fails closed
                # after its lease instead of allowing a duplicate.
                _logger.exception("could not release failed idempotency claim")

        await self._send_captured(
            send,
            status_code=status_code,
            headers=response_headers,
            body=response_body,
        )

    @staticmethod
    async def _send_captured(
        send: Callable[[dict], Awaitable[None]],
        *,
        status_code: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
    ) -> None:
        fresh_headers = [
            (name, value)
            for name, value in headers
            if name.lower() != b"idempotent-replay"
        ]
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": fresh_headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _replay(send: Callable[[dict], Awaitable[None]], record: dict) -> None:
        body = record["response_body"]
        headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in json.loads(record["response_headers"])
        ]
        headers = [
            (name, value)
            for name, value in headers
            if name.lower() not in {b"content-length", b"idempotent-replay"}
        ]
        if record["status_code"] != 204:
            headers.append((b"content-length", str(len(body)).encode("ascii")))
        headers.append((b"idempotent-replay", b"true"))
        await send(
            {
                "type": "http.response.start",
                "status": record["status_code"],
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _send_json_response(
    send: Callable[[dict], Awaitable[None]],
    body: dict,
    *,
    status_code: int,
    extra_headers: dict[str, str] | None = None,
) -> None:
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    headers.extend(
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in SECURITY_HEADERS.items()
    )
    if extra_headers:
        headers.extend(
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in extra_headers.items()
        )
    if config.COOKIE_SECURE:
        headers.append(
            (
                b"strict-transport-security",
                b"max-age=63072000; includeSubDomains",
            )
        )
    await send(
        {"type": "http.response.start", "status": status_code, "headers": headers}
    )
    await send({"type": "http.response.body", "body": payload})


_CACHEABLE_REQUEST_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SESSION_SENSITIVE_PATH_ROOTS = ("/auth", "/login", "/logout", "/settings")


def _path_matches_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _append_vary(response: Response, *dimensions: str) -> None:
    """Add cache-key dimensions while preserving every existing Vary value."""
    existing = [
        dimension.strip()
        for value in response.headers.getlist("vary")
        for dimension in value.split(",")
        if dimension.strip()
    ]
    seen = {dimension.casefold() for dimension in existing}
    if "*" in seen:
        return
    for dimension in dimensions:
        folded = dimension.casefold()
        if folded not in seen:
            existing.append(dimension)
            seen.add(folded)
    if existing:
        response.headers["Vary"] = ", ".join(existing)


def _apply_private_cache_policy(request: Request, response: Response) -> None:
    """Prevent shared or browser caches from retaining sensitive responses."""
    request_has_cookie = "cookie" in request.headers
    response_sets_cookie = "set-cookie" in response.headers
    session_path = any(
        _path_matches_root(request.url.path, root)
        for root in _SESSION_SENSITIVE_PATH_ROOTS
    )
    identity_request = any(
        header in request.headers for header in ("authorization", "x-athena-actor")
    )
    attachment_request = _path_matches_root(request.url.path, "/attachments")
    content_disposition = response.headers.get("content-disposition", "")
    download_response = (
        content_disposition.partition(";")[0].strip().casefold() == "attachment"
    )
    mutating_request = request.method not in _CACHEABLE_REQUEST_METHODS

    if not (
        request_has_cookie
        or response_sets_cookie
        or session_path
        or identity_request
        or attachment_request
        or download_response
        or mutating_request
    ):
        return

    vary: list[str] = []
    if request_has_cookie or response_sets_cookie or session_path:
        vary.append("Cookie")
    if identity_request or attachment_request:
        vary.extend(
            (
                "Authorization",
                "X-Athena-Actor",
            )
        )

    response.headers["Cache-Control"] = "private, no-store"
    _append_vary(response, *vary)


def _attach_security_headers(response: Response, *, is_https: bool = False) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # Emit HSTS when the operator declared HTTPS (COOKIE_SECURE) OR the request actually
    # arrived over TLS. The scheme check auto-covers a direct-HTTPS deploy; behind a
    # TLS-terminating proxy the scheme is http, so COOKIE_SECURE remains the switch there.
    # This only ever ADDS HSTS on a real HTTPS request — it never sends it over plain http.
    if config.COOKIE_SECURE or is_https:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return response


def create_app(
    db_path: str | Path | None = None,
    *,
    max_request_body_bytes: int | None = None,
    token_rate_limit_per_minute: int | None = None,
    anon_rate_limit_per_minute: int | None = None,
    login_rate_limit_per_minute: int | None = None,
    idempotency_wait_seconds: float | None = None,
    idempotency_lease_seconds: int | None = None,
    idempotency_ttl_seconds: int | None = None,
    idempotency_max_response_bytes: int | None = None,
) -> FastAPI:
    _configure_logging()
    resolved_db = Path(db_path) if db_path is not None else config.DB_PATH
    body_limit = (
        config.MAX_REQUEST_BODY_BYTES
        if max_request_body_bytes is None
        else max_request_body_bytes
    )
    token_limit = (
        config.TOKEN_RATE_LIMIT_PER_MINUTE
        if token_rate_limit_per_minute is None
        else token_rate_limit_per_minute
    )
    anon_limit = (
        config.ANON_RATE_LIMIT_PER_MINUTE
        if anon_rate_limit_per_minute is None
        else anon_rate_limit_per_minute
    )
    login_limit = (
        config.LOGIN_RATE_LIMIT_PER_MINUTE
        if login_rate_limit_per_minute is None
        else login_rate_limit_per_minute
    )
    idempotency_wait = (
        config.IDEMPOTENCY_WAIT_SECONDS
        if idempotency_wait_seconds is None
        else idempotency_wait_seconds
    )
    idempotency_lease = (
        config.IDEMPOTENCY_LEASE_SECONDS
        if idempotency_lease_seconds is None
        else idempotency_lease_seconds
    )
    idempotency_ttl = (
        config.IDEMPOTENCY_TTL_SECONDS
        if idempotency_ttl_seconds is None
        else idempotency_ttl_seconds
    )
    idempotency_response_limit = (
        config.IDEMPOTENCY_MAX_RESPONSE_BYTES
        if idempotency_max_response_bytes is None
        else idempotency_max_response_bytes
    )

    # Cookies without the Secure flag ride over plain HTTP too, so a network attacker
    # on an HTTPS deploy could capture the session/CSRF cookie. We keep the default OFF
    # (http dev + the test suite need it), but warn loudly once so a real HTTPS deploy
    # doesn't ship insecure by accident. There is no dev/prod signal to key off, so this
    # is a warning, not a hard flip.
    global _cookie_secure_warned
    if not config.COOKIE_SECURE and not _cookie_secure_warned:
        _cookie_secure_warned = True
        _logger.warning(
            "Session/CSRF cookies are NOT marked Secure (ATHENA_COOKIE_SECURE is off); "
            "set ATHENA_COOKIE_SECURE=1 for any deployment served over HTTPS."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: bring the schema up to date before serving any request,
        # so the database is always the right shape. Stash the path for handlers.
        conn = db.connect(resolved_db)
        try:
            applied = db.migrate(conn)
        finally:
            conn.close()
        # Log what startup actually did, so an operator watching stdout can see the
        # schema was brought current (or was already so) instead of guessing.
        if applied:
            _logger.info(
                "applied %d migration(s): %s", len(applied), ", ".join(applied)
            )
        else:
            _logger.info("schema already current; no migrations to apply")
        app.state.db_path = resolved_db
        # Start the single in-process webhook delivery loop (unless disabled — e.g.
        # in tests, or in extra worker processes that must not double-deliver).
        delivery_task = (
            asyncio.create_task(webhooks.delivery_loop(resolved_db))
            if config.WEBHOOK_DELIVERY_ENABLED
            else None
        )
        # The automation rules engine: a sibling in-process loop that drains new activity
        # events and fires matching rules' in-app actions. Same single-loop caveat as
        # webhooks (one per deployment, off in tests).
        automation_task = (
            asyncio.create_task(aegis_automation.process_loop(resolved_db))
            if config.AUTOMATION_ENABLED
            else None
        )
        _logger.info(
            "background loops: webhook delivery %s, automation %s",
            "started" if delivery_task else "disabled",
            "started" if automation_task else "disabled",
        )
        try:
            yield
        finally:
            # Shutdown: stop the background loops cleanly.
            tasks = tuple(
                task for task in (delivery_task, automation_task) if task is not None
            )
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    raise result

    app = FastAPI(title="Athena", lifespan=lifespan)

    # Teach the undo engine each layer's inverses. core/undo.py owns the mechanism
    # and may not import aegis/mentor (the import contract), so the composition
    # root is the only place that may know both — the same reason the routers are
    # assembled here. Registration is idempotent, so building many apps in one
    # process (every test) is fine.
    aegis_issue_undo.register()
    mentor_page_undo.register()

    # A tagged write that tries to continue ANOTHER actor's run is refused deep
    # in activity.record (transport-neutral, transaction-rolled-back); this maps
    # the refusal to the same 403 shape every authorization failure uses.
    def _run_binding_conflict(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, activity.RunBindingError):
            raise exc
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    app.add_exception_handler(activity.RunBindingError, _run_binding_conflict)

    # A scope denial is a probe worth remembering: put it on the trail (its own
    # connection — the request's is dependency-scoped), then answer exactly as
    # the plain 403 always did. Registered on the subclass, so every other
    # HTTPException keeps FastAPI's default handling.
    def _record_scope_denial(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, identity.ScopeDenied):
            raise exc
        # Best-effort in full: opening the connection is INSIDE the guard too, so
        # a connect failure can never turn this deliberate 403 into a 500.
        if exc.actor_id is not None:
            try:
                conn = db.connect(request.app.state.db_path)
                try:
                    security_events.record_failure(
                        conn,
                        actor_id=exc.actor_id,
                        verb=security_events.VERB_SCOPE_DENIED,
                        target_kind="user",
                        target_id=exc.actor_id,
                        detail=f"{exc.scope} on {request.method} {request.url.path}",
                    )
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 — the 403 must go out regardless
                _logger.exception("could not record scope denial")
        return JSONResponse(
            status_code=403,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    app.add_exception_handler(identity.ScopeDenied, _record_scope_denial)

    # A budget refusal is raised inside the metered command's transaction, so that
    # write has already rolled back whole by the time this runs. Recording it here
    # — one app-level owner, its own connection — means every transport (REST, the
    # browser, and MCP through REST) reports the same 429 with the same stable code
    # and Retry-After, and an agent hitting its ceiling always reaches the trail as
    # the decision the operator should see.
    def _budget_exhausted(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, budgets.BudgetExhausted):
            raise exc
        try:
            conn = db.connect(request.app.state.db_path)
            try:
                budgets.record_exhaustion(
                    conn,
                    actor_id=exc.budget.user_id,
                    detail=f"{request.method} {request.url.path}",
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — the 429 must go out regardless
            _logger.exception("could not record budget exhaustion")
        return JSONResponse(
            status_code=429,
            content={
                "detail": str(exc),
                "code": budgets.BUDGET_EXHAUSTED_CODE,
                "budget": exc.budget.public(),
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    app.add_exception_handler(budgets.BudgetExhausted, _budget_exhausted)

    # A gated action refuses inside the command's transaction, so that write has
    # rolled back whole by the time this runs — which is exactly why the pending
    # request is recorded HERE, on its own connection: a row written inside that
    # transaction would have rolled back with the refusal. Recording is idempotent,
    # so an agent retrying while it waits re-reads its existing ask rather than
    # flooding the operator's queue. 202: the ask was accepted, the action was not.
    def _approval_required(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, approvals.ApprovalRequired):
            raise exc
        recorded = None
        try:
            conn = db.connect(request.app.state.db_path)
            try:
                recorded = approvals.open_request(
                    conn,
                    actor_id=exc.actor_id,
                    action_kind=exc.action_kind,
                    target_kind=exc.target_kind,
                    target_id=exc.target_id,
                    run_id=run_context.get_run_id(),
                ).public()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — the 202 must go out regardless
            _logger.exception("could not record approval request")
        return JSONResponse(
            status_code=202,
            content={
                "detail": str(exc),
                "code": approvals.APPROVAL_REQUIRED_CODE,
                "approval": recorded,
            },
        )

    app.add_exception_handler(approvals.ApprovalRequired, _approval_required)

    # A rejection is an ANSWER, not a delay: 409 rather than 202, so an agent stops
    # retrying instead of waiting for a decision that already arrived.
    def _approval_rejected(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, approvals.ApprovalRejected):
            raise exc
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": approvals.APPROVAL_REJECTED_CODE,
                "approval": exc.request.public(),
            },
        )

    app.add_exception_handler(approvals.ApprovalRejected, _approval_rejected)

    # Undo refusals are raised transport-neutrally from core/undo.py and from the
    # compensators, so one handler answers identically for REST, the browser, and
    # MCP-through-REST. The status code travels on the exception because the
    # reasons genuinely differ (404 invisible, 409 already reversed or nothing
    # left to reverse, 422 not reversible); `code` is the stable contract.
    def _undo_refused(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, undo.UndoRefused):
            raise exc
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc), "code": exc.code},
        )

    app.add_exception_handler(undo.UndoRefused, _undo_refused)
    app.state.token_rate_limiter = rate_limits.FixedWindowRateLimiter(token_limit)
    # Throttles anonymous (credential-free) reads by client IP; see optional_actor.
    app.state.anon_rate_limiter = rate_limits.FixedWindowRateLimiter(anon_limit)
    # Throttles POST /login by client IP, before the password hash; see web/auth.py.
    app.state.login_rate_limiter = rate_limits.FixedWindowRateLimiter(login_limit)
    # Middleware is registered inside-out. Idempotency wraps the routes, run
    # context wraps idempotency, and the request-body cap is outermost so keyed
    # requests are bounded before the fingerprint layer buffers them.
    app.add_middleware(
        IdempotencyMiddleware,
        db_path=resolved_db,
        wait_seconds=idempotency_wait,
        lease_seconds=idempotency_lease,
        ttl_seconds=idempotency_ttl,
        max_response_bytes=idempotency_response_limit,
    )
    app.add_middleware(RunContextMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=body_limit)

    @app.middleware("http")
    async def attach_session_user(request: Request, call_next):
        # Resolve the browser session once per request onto request.state.user,
        # so every page (and the nav) knows who is logged in without each route
        # re-doing it. The session's CSRF token rides alongside on
        # request.state.csrf_token, so forms can embed it and verify_csrf can
        # check it. No cookie → no DB hit; both stay None.
        request.state.user = None
        request.state.csrf_token = None
        # Unread-inbox count for the nav badge; 0 when logged out.
        request.state.unread_count = 0
        # Whether SSO is configured, so the nav can show the linked-identities link
        # only when it's relevant. Evaluated per request so tests/config see it live.
        request.state.oidc_enabled = config.oidc_enabled()
        raw = request.cookies.get(config.SESSION_COOKIE)
        if raw:
            conn = db.connect(request.app.state.db_path)
            try:
                request.state.user = sessions.resolve_session(conn, raw)
                # The pause lever reaches browser sessions too: a paused user is
                # treated as signed out (no writes, no personal surfaces) until
                # an admin resumes them — without burning their session.
                if request.state.user is not None and request.state.user.get(
                    "paused_at"
                ):
                    request.state.user = None
                if request.state.user is not None:
                    request.state.csrf_token = sessions.csrf_token_for(conn, raw)
                    # Gate the badge by visibility too, so it matches the inbox: an
                    # unread notification on a target the user can no longer see
                    # doesn't inflate the count.
                    request.state.unread_count = notifications.unread_count(
                        conn, request.state.user["id"], actor=request.state.user
                    )
            finally:
                conn.close()
        return await call_next(request)

    @app.middleware("http")
    async def harden_http(request: Request, call_next):
        response = await call_next(request)
        _apply_private_cache_policy(request, response)
        return _attach_security_headers(
            response, is_https=request.url.scheme == "https"
        )

    # Mount web foundation (static + Jinja templates + page router).
    # This is the only place the web layer is wired. Do not change /healthz or lifespan.
    # Resolve package-owned assets from the installed module, not the process cwd.
    # Keeping them inside athena/ makes editable checkouts and built wheels obey the
    # same runtime contract instead of making a wheel reach back into a repo layout.
    package_root = Path(__file__).resolve().parent
    app.mount(
        "/static",
        StaticFiles(directory=package_root / "static"),
        name="static",
    )
    templates = Jinja2Templates(directory=package_root / "templates")
    init_templates(templates)
    app.include_router(web_router)
    app.include_router(web_projects.router)
    app.include_router(web_activity.router)
    app.include_router(web_boards.router)
    app.include_router(web_filters.router)
    app.include_router(web_auth.router)
    app.include_router(web_mentor.router)
    app.include_router(web_labels.router)
    app.include_router(web_admin.router)
    app.include_router(web_work_context.router)
    app.include_router(web_fleet_metrics.router)

    # Core REST API (users, api tokens, cross-module search).
    app.include_router(users_api.router)
    app.include_router(approvals_api.router)
    app.include_router(workers_api.router)
    app.include_router(security_api.router)
    app.include_router(mentor_learnings_api.router)
    app.include_router(tokens_api.router)
    app.include_router(agent_runs_api.router)
    app.include_router(search_api.router)
    app.include_router(activity_api.router)
    app.include_router(events_api.router)
    app.include_router(webhooks_api.router)
    app.include_router(attachments_api.router)
    app.include_router(notifications_api.router)

    # Aegis REST API (issues + labels + projects + saved filters).
    app.include_router(aegis_api.router)
    app.include_router(aegis_api.labels_router)
    app.include_router(aegis_api.projects_router)
    app.include_router(aegis_delegations_api.router)
    app.include_router(aegis_filters_api.router)
    app.include_router(aegis_fleet_work_api.router)
    app.include_router(aegis_sprints_api.router)
    app.include_router(aegis_automation_api.router)
    app.include_router(aegis_work_context_api.router)
    app.include_router(aegis_fleet_metrics_api.router)

    # Mentor REST API (spaces + pages + versions).
    app.include_router(mentor_api.spaces_router)
    app.include_router(mentor_api.pages_router)

    @app.get("/healthz")
    def healthz():
        """Liveness check — cheap, no DB hit. Used by tests and monitoring."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request):
        """Readiness check — verifies the SQLite database is reachable and migrated."""
        try:
            conn = db.connect(request.app.state.db_path)
            try:
                db.migration_status(conn)
            finally:
                conn.close()
        except (OSError, sqlite3.Error, db.MigrationIntegrityError):
            return JSONResponse(
                {"status": "error", "database": "unavailable"}, status_code=503
            )
        return {"status": "ok", "database": "ok"}

    return app


# The instance the server runs:  uvicorn athena.main:app
app = create_app()
