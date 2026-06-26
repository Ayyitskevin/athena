"""The Athena web application: a thin FastAPI layer over core and the modules.

`create_app()` is an *application factory* — call it to build a fresh app.
Tests call it with a throwaway database; the server calls it once for real.
No global app state, no surprises.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
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
from athena.aegis import filters_api as aegis_filters_api
from athena.core import (
    activity_api,
    attachments_api,
    db,
    events_api,
    notifications,
    notifications_api,
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
        try:
            yield
        finally:
            # Shutdown: stop the delivery loop cleanly.
            if delivery_task is not None:
                delivery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery_task

    app = FastAPI(title="Athena", lifespan=lifespan)
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
        raw = request.cookies.get(config.SESSION_COOKIE)
        if raw:
            conn = db.connect(request.app.state.db_path)
            try:
                request.state.user = sessions.resolve_session(conn, raw)
                if request.state.user is not None:
                    request.state.csrf_token = sessions.csrf_token_for(conn, raw)
                    request.state.unread_count = notifications.unread_count(
                        conn, request.state.user["id"]
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
