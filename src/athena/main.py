"""The Athena web application: a thin FastAPI layer over core and the modules.

`create_app()` is an *application factory* — call it to build a fresh app.
Tests call it with a throwaway database; the server calls it once for real.
No global app state, no surprises.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from athena import config
from athena.aegis import api as aegis_api
from athena.aegis import automation as aegis_automation
from athena.aegis import automation_api as aegis_automation_api
from athena.aegis import filters_api as aegis_filters_api
from athena.aegis import sprints_api as aegis_sprints_api
from athena.core import (
    activity_api,
    attachments_api,
    db,
    events_api,
    idempotency,
    notifications,
    notifications_api,
    run_context,
    search_api,
    sessions,
    tokens_api,
    users_api,
    webhooks,
    webhooks_api,
)
from athena.mentor import api as mentor_api
from athena.web import admin as web_admin
from athena.web import auth as web_auth
from athena.web import labels as web_labels
from athena.web import mentor as web_mentor
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
    """Capture the X-Athena-Run header into the request-scoped run context, so every
    activity event recorded while handling this request is stamped with that run id.

    A pure-ASGI middleware (not @app.middleware) ON PURPOSE: it runs in the same task
    as the endpoint, so the contextvar it sets reliably propagates into the handler
    (including sync handlers run in the threadpool, which copy the current context) —
    the propagation that BaseHTTPMiddleware does not guarantee. The token is reset in
    a finally so a run id never outlives its request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        run_raw = None
        parent_raw = None
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
        run_token = run_context.set_run_id(run_raw)
        parent_token = run_context.set_parent_run_id(parent_raw)
        try:
            await self.app(scope, receive, send)
        finally:
            run_context.reset_parent_run_id(parent_token)
            run_context.reset_run_id(run_token)


class IdempotencyMiddleware:
    """Replay the first response to a retried write, so an agent's POST that times out
    on a network blip and gets retried can't double-create (two issues, two comments).

    When a POST carries an `Idempotency-Key` header from an identifiable caller, the
    first 2xx response is stored and any later request with the same key REPLAYS it
    verbatim instead of running the write again. Pure-ASGI (like RunContextMiddleware)
    so it can both short-circuit with the stored response and capture the live one by
    wrapping `send`.

    Bounded blast radius: anything that is not POST + Idempotency-Key + an identifiable
    credential passes straight through untouched, so non-opt-in traffic is unaffected.
    Only 2xx is stored — a retry of a *failed* write should re-run, not replay the
    error. Registered INSIDE harden_http, so a replayed response still gets the
    security headers. Sequential retries (the common case) are fully covered; truly
    concurrent same-key requests may both run, with the second store ignored — a
    future claim-first pass could close that.
    """

    def __init__(self, app, db_path):
        self.app = app
        self.db_path = db_path

    @staticmethod
    def _identity(headers: dict) -> str | None:
        """Scope the key to the caller, WITHOUT a DB lookup: a bearer token by its
        hash (never the raw value), or the trusted actor header by id. None means the
        request isn't identifiably authenticated, so we don't dedupe it (it will be
        handled — likely 401 — like any other request)."""
        auth = headers.get(b"authorization")
        if auth is not None and auth.lower().startswith(b"bearer "):
            token = auth[len(b"bearer "):].strip()
            return "tok:" + hashlib.sha256(token).hexdigest()
        if config.TRUST_ACTOR_HEADER:
            actor = headers.get(b"x-athena-actor")
            if actor is not None:
                return "actor:" + actor.decode("latin-1")
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        key_raw = headers.get(b"idempotency-key")
        if not key_raw:
            await self.app(scope, receive, send)
            return
        key = key_raw.decode("latin-1").strip()
        identity = self._identity(headers)
        if not key or identity is None:
            await self.app(scope, receive, send)
            return

        conn = db.connect(self.db_path)
        try:
            existing = idempotency.get(conn, key=key, identity=identity)
            if existing is not None:
                await self._replay(send, existing)
                return

            captured: dict = {"status": 500, "headers": [], "body": []}

            async def capture_send(message):
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                    captured["headers"] = message.get("headers", [])
                elif message["type"] == "http.response.body":
                    captured["body"].append(message.get("body", b""))
                await send(message)

            await self.app(scope, receive, capture_send)

            if 200 <= captured["status"] < 300:
                content_type = next(
                    (
                        v.decode("latin-1")
                        for n, v in captured["headers"]
                        if n.lower() == b"content-type"
                    ),
                    None,
                )
                idempotency.store(
                    conn,
                    key=key,
                    identity=identity,
                    method="POST",
                    path=scope.get("path", ""),
                    status_code=captured["status"],
                    content_type=content_type,
                    response_body=b"".join(captured["body"]),
                )
        finally:
            conn.close()

    @staticmethod
    async def _replay(send: Callable[[dict], Awaitable[None]], record: dict) -> None:
        body = record["response_body"]
        headers = [(b"content-length", str(len(body)).encode("ascii"))]
        if record["content_type"]:
            headers.append((b"content-type", record["content_type"].encode("latin-1")))
        # Let a caller tell a replay apart from a fresh write.
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
    send: Callable[[dict], Awaitable[None]], body: dict, *, status_code: int
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


def _attach_security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if config.COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return response


def create_app(
    db_path: str | Path | None = None,
    *,
    max_request_body_bytes: int | None = None,
) -> FastAPI:
    resolved_db = Path(db_path) if db_path is not None else config.DB_PATH
    body_limit = (
        config.MAX_REQUEST_BODY_BYTES
        if max_request_body_bytes is None
        else max_request_body_bytes
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: bring the schema up to date before serving any request,
        # so the database is always the right shape. Stash the path for handlers.
        conn = db.connect(resolved_db)
        db.migrate(conn)
        conn.close()
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
        try:
            yield
        finally:
            # Shutdown: stop the background loops cleanly.
            for task in (delivery_task, automation_task):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Athena", lifespan=lifespan)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=body_limit)
    # Stamp the ambient run id (X-Athena-Run) for every event recorded this request.
    app.add_middleware(RunContextMiddleware)
    # Replay the first response to a retried write (opt-in via the Idempotency-Key
    # header). Added here so it sits INSIDE harden_http (a replay still gets the
    # security headers) but wraps the routes (it can capture their response).
    app.add_middleware(IdempotencyMiddleware, db_path=resolved_db)

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
        return _attach_security_headers(response)

    # Mount web foundation (static + Jinja templates + page router).
    # This is the only place the web layer is wired. Do not change /healthz or lifespan.
    # Resolve from the package location, not the process cwd, so the app boots the
    # same whether launched from the repo root or anywhere else (static/ and
    # templates/ live at the repo root, two levels above this src/athena/ package).
    repo_root = Path(__file__).resolve().parents[2]
    app.mount(
        "/static",
        StaticFiles(directory=repo_root / "static"),
        name="static",
    )
    templates = Jinja2Templates(directory=repo_root / "templates")
    init_templates(templates)
    app.include_router(web_router)
    app.include_router(web_auth.router)
    app.include_router(web_mentor.router)
    app.include_router(web_labels.router)
    app.include_router(web_admin.router)

    # Core REST API (users, api tokens, cross-module search).
    app.include_router(users_api.router)
    app.include_router(tokens_api.router)
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
    app.include_router(aegis_filters_api.router)
    app.include_router(aegis_sprints_api.router)
    app.include_router(aegis_automation_api.router)

    # Mentor REST API (spaces + pages; versions later).
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
                conn.execute("SELECT 1").fetchone()
                migrated = conn.execute(
                    "SELECT version FROM schema_migrations LIMIT 1"
                ).fetchone()
                if migrated is None:
                    raise sqlite3.DatabaseError("no migrations have run")
            finally:
                conn.close()
        except sqlite3.Error:
            return JSONResponse(
                {"status": "error", "database": "unavailable"}, status_code=503
            )
        return {"status": "ok", "database": "ok"}

    return app


# The instance the server runs:  uvicorn athena.main:app
app = create_app()
