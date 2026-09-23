"""HTTP middleware and the response hardening helpers the app factory installs.

Split out of main.py so the composition root stays the place that wires
routers, and this module stays the place that decides what happens to a
request before a route runs.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from typing import Awaitable, Callable
from fastapi import Request
from starlette._utils import get_route_path
from athena import config
from athena.core import (
    db,
    deployment,
    deps,
    idempotency,
    rate_limits,
    run_context,
    tokens,
    users,
)

_logger = logging.getLogger("athena")


def content_security_policy(nonce: str | None = None) -> str:
    """The CSP for one response.

    `style-src` carries no 'unsafe-inline'. Athena renders a handful of styles that
    genuinely depend on data — a label's stored hex, a rollup bar's percentage, a
    page's nesting depth — and none of them can be a static class, because the value
    is not known until the row is read. A CSP nonce does NOT license inline `style=`
    ATTRIBUTES (only `<style>` elements and scripts), so those values are emitted as
    tiny nonce-carrying `<style>` elements instead, and the attribute form is gone
    from the templates entirely. See web/render.py and the label chips.

    The nonce is per response and unguessable, so markup an attacker manages to
    inject cannot carry a matching one.
    """
    style_src = "'self'" if nonce is None else f"'self' 'nonce-{nonce}'"
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        f"style-src {style_src}; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


SECURITY_HEADERS = {
    "Content-Security-Policy": content_security_policy(),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _is_signed_inbound(method: str, path: str) -> bool:
    """Machine-to-machine routes whose request body carries its own credential."""
    return method == "POST" and (
        path == "/callbacks/icarus"
        or path.startswith("/callbacks/icarus/")
        or path.startswith("/forge/")
    )


# The browser paths that must stay reachable when ATHENA_ANONYMOUS_READS=0, because
# closing them would lock the operator out of the instrument they use to stop being
# anonymous. Everything here either IS the sign-in path, carries its own credential,
# or discloses nothing: the login form and its SSO round trip, sign-out, the health
# probe, and the packaged static assets the login page itself needs to render.
# /readyz is DELIBERATELY absent: it is registered directly on the app with no
# browser dependency, so this gate never sees it — the uptime check survives
# ATHENA_ANONYMOUS_READS=0 structurally, and tests/test_anonymous_reads_closed.py
# pins that. Adding it here would be dead config claiming a mechanism that is
# not in play (measured 2026-08-24 while executing the close-anonymous-reads
# ruling; an earlier report that the flip closes /readyz did not reproduce).
_ANONYMOUS_ALWAYS_ALLOWED = frozenset(
    {"/login", "/login/sso", "/auth/callback", "/logout", "/healthz"}
)


class _BrowserSessionRequired(Exception):
    """A closed browser route was reached without a resolved session."""


def _require_browser_session(request: Request) -> None:
    """Require the browser identity path when anonymous reads are closed.

    Browser routes render from ``request.state.user``; bearer and trusted actor
    headers belong to REST/MCP and deliberately do not satisfy this dependency.
    """
    if _anonymous_web_read_allowed(request.method, get_route_path(request.scope)):
        return
    if getattr(request.state, "user", None) is None:
        raise _BrowserSessionRequired


def _anonymous_web_read_allowed(method: str, path: str) -> bool:
    """Whether a signed-out browser request may proceed with reads closed."""
    if config.ANONYMOUS_READS:
        return True
    if path in _ANONYMOUS_ALWAYS_ALLOWED or path.startswith("/static/"):
        return True
    # A machine callback authenticates its own body; it never had a session.
    if _is_signed_inbound(method, path):
        return True
    # Creating the FIRST user on an empty database presents the one-time bootstrap
    # credential instead of a session. Gating it would make a closed instance
    # impossible to set up — the flag would lock the operator out of their own box.
    return method == "POST" and path == "/users"


class DeploymentBoundaryMiddleware:
    """Refuse requests outside Athena's declared socket and Host boundary.

    The accepted socket address comes from the ASGI server scope; the Host value
    comes from the request and is therefore validated independently. This is a
    defense against accidental direct-interface exposure and DNS rebinding. It
    cannot detect a proxy or tunnel that reaches Athena over an allowed socket.
    """

    def __init__(
        self,
        app,
        *,
        network_mode: str,
        allowed_authorities: tuple[deployment.Authority, ...],
        expected_server: tuple[str, int] | None,
    ):
        self.app = app
        self.network_mode = network_mode
        self.allowed_authorities = allowed_authorities
        self.expected_server = expected_server
        self.allowed_ports = frozenset(
            authority.port for authority in allowed_authorities
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        server = scope.get("server")
        server_allowed = (
            isinstance(server, (list, tuple))
            and len(server) >= 2
            and isinstance(server[0], str)
            and isinstance(server[1], int)
            and not isinstance(server[1], bool)
            and (
                deployment.server_address_matches(
                    server[0],
                    server[1],
                    self.expected_server,
                )
                if self.expected_server is not None
                else (
                    deployment.address_allowed(server[0], self.network_mode)
                    and server[1] in self.allowed_ports
                )
            )
        )
        authority_allowed = deployment.request_authority_allowed(
            scope.get("headers", ()),
            scheme=scope.get("scheme", "http"),
            allowed=self.allowed_authorities,
        )
        if server_allowed and authority_allowed:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_json_response(
            send,
            {"detail": "request rejected by deployment boundary"},
            status_code=421 if server_allowed else 503,
            extra_headers={"Cache-Control": "private, no-store"},
        )


class SignedInboundRateLimitMiddleware:
    """Throttle signed-inbound attempts before body, session, or route work.

    These routes have no Athena actor. Their shared direct-peer-IP limiter must
    therefore sit outside the request-body buffer and browser middleware; putting
    the same check in a handler bounds HMAC/lookup work but still lets a saturated
    peer make Athena read a body and resolve an attacker-supplied session cookie.
    """

    def __init__(self, app, limiter: rate_limits.FixedWindowRateLimiter):
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_signed_inbound(
            scope.get("method", ""), get_route_path(scope)
        ):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_ip = client[0] if client is not None else "unknown"
        decision = self.limiter.check(client_ip)
        if decision.allowed:
            await self.app(scope, receive, send)
            return
        await _send_json_response(
            send,
            {"detail": "anonymous rate limit exceeded"},
            status_code=429,
            extra_headers={
                "Cache-Control": "private, no-store",
                "Retry-After": str(decision.retry_after_seconds),
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": str(decision.remaining),
            },
        )


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


class RequestConnectionMiddleware:
    """Give one request exactly one SQLite connection, and close it once.

    A request used to open several: the session middleware for the cookie, the route
    dependency for the handler, and an idempotent write three more (identity,
    reserve, publish). Opening is not free — `sqlite3.connect` is lazy and costs
    ~0.03 ms, but the first statement that touches the file pays ~2.2 ms to attach,
    per connection, and holding another open does not help. Measured here: ~4.4 ms
    of attach on a browser page and ~11 ms on an idempotent write, against a
    fleet-attention rollup that measures ~0.5 ms.

    Pure ASGI, and registered OUTSIDE the session middleware so it also spans the
    layers that run after the route returns — the idempotency publish, an exception
    handler recording a refusal. Those are why the route dependency cannot own the
    lifetime: it has already exited by then.

    The holder opens lazily, so this middleware costs a request that never touches
    the database exactly nothing — a `/static` fetch still opens zero connections.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        holder = deps.RequestConnection(lambda: scope["app"].state.db_path)
        scope.setdefault("state", {})["db_holder"] = holder
        try:
            await self.app(scope, receive, send)
        finally:
            # One close, whatever happened — a handler that raised past every
            # exception handler still must not leak the file handle.
            holder.close()


class SlowRequestLogMiddleware:
    """Log one WARNING for any request slower than the configured threshold.

    Nothing in Athena has ever measured request latency. Uvicorn's access log
    carries method, path, and status but no timing, so a read that quietly grew
    from 0.7ms to 229ms — which is exactly what F-0.1 was — looks identical to a
    healthy one in the only per-request record that exists. This is the smallest
    thing that makes that visible in production, and it is the runtime companion
    to the CI scaling gate in tests/test_activity_feed_scaling.py.

    Pure ASGI so it can time the response through to the last byte the inner
    stack sends, including the idempotency publish that runs after the route
    returns. `finally` so a request that raises is still timed — a slow failure
    is the interesting kind.

    What it deliberately does NOT log: query strings, headers, bodies, cookies,
    or the raw path. It reports the ROUTE TEMPLATE (`/issues/{issue_id}`), which
    is low-cardinality and cannot carry a page title, a search term, an
    attacker-supplied 404 path, or a token. An unmatched request logs its method
    and nothing else; uvicorn's access log already has the raw path for those.
    """

    def __init__(self, app, *, threshold_ms: int):
        self.app = app
        self.threshold_ms = threshold_ms

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.threshold_ms <= 0:
            await self.app(scope, receive, send)
            return
        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if elapsed_ms >= self.threshold_ms:
                route = getattr(scope.get("route"), "path", None)
                _logger.warning(
                    "slow request: %s %s -> %s in %.0fms (threshold %dms)",
                    scope.get("method", "?"),
                    route or "<unmatched>",
                    status_code or "no response",
                    elapsed_ms,
                    self.threshold_ms,
                )


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
    "/run-controls",
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
    def _run_db(source, operation, **kwargs):
        """Run one idempotency operation against the request's connection.

        `source` is the request's RequestConnection when the request-connection
        middleware is installed — the normal case, and the reason an idempotent
        write no longer pays three extra ~2.2 ms attaches. It falls back to a path
        so this middleware still works when mounted without that one (a bare ASGI
        app, a focused test).

        Nothing here closes a borrowed connection: the reserve happens before the
        route and the publish after it, and the middleware that opened it closes it
        once. Every idempotency operation commits or rolls back its own transaction,
        so the connection goes back clean — RequestConnection.get verifies that on
        the next handoff rather than trusting it.
        """
        if isinstance(source, deps.RequestConnection):
            return operation(source.get(), **kwargs)
        conn = db.connect(source)
        try:
            return operation(conn, **kwargs)
        finally:
            conn.close()

    def _source(self, scope):
        """This request's connection holder, or the path if there is no middleware."""
        return scope.get("state", {}).get("db_holder") or self.db_path

    async def _authenticate(
        self, scope, headers: dict[bytes, bytes]
    ) -> tuple[str | None, int | None, bool]:
        """Return a canonical live credential identity, optional token id, and
        whether that credential's account is currently paused."""
        authorization = headers.get(b"authorization")
        if authorization is not None and authorization.lower().startswith(b"bearer "):
            raw = authorization[len(b"bearer ") :].strip().decode("latin-1")
            actor = await asyncio.to_thread(
                self._resolve_token, self._source(scope), raw
            )
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
                actor = await asyncio.to_thread(
                    self._resolve_actor, self._source(scope), actor_id
                )
                if actor is not None:
                    return f"actor:{actor_id}", None, bool(actor.get("paused_at"))
        return None, None, False

    @classmethod
    def _resolve_token(cls, source, raw: str) -> dict | None:
        return cls._run_db(source, tokens.resolve_token, raw=raw)

    @classmethod
    def _resolve_actor(cls, source, actor_id: int) -> dict | None:
        return cls._run_db(source, users.get_user, user_id=actor_id)

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
        scope,
        *,
        key: str,
        identity: str,
        method: str,
        path: str,
        fingerprint: str,
    ) -> idempotency.ClaimResult:
        return await asyncio.to_thread(
            self._run_db,
            self._source(scope),
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
        scope,
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
                    self._source(scope),
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

        # Starlette strips ASGI root_path before routing. Classify the same
        # route-relative path here so a mounted deployment cannot bypass either
        # idempotency handling or the signed-inbound exemption.
        path = get_route_path(scope)
        # Signed machine callbacks own retry semantics in their authenticated
        # command. An attacker-supplied Idempotency-Key must not intercept the
        # request before its HMAC gate or trigger credential/database work.
        if _is_signed_inbound(method, path):
            await self.app(scope, receive, send)
            return
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
            identity, token_id, paused = await self._authenticate(scope, headers)
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
            # The route itself rejects a key on credentialed first-user bootstrap
            # before inspecting database state. Let it also charge the anonymous
            # limiter and produce that state-independent response. Ordinary
            # protected routes likewise reach their canonical identity gate.
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
                    scope,
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
                scope,
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
                    self._source(scope),
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
                    scope,
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
                scope,
                key=key,
                identity=identity,
                owner_token=owner_token,
                failure_code="server_error_response",
            )
        else:
            try:
                await asyncio.to_thread(
                    self._run_db,
                    self._source(scope),
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
