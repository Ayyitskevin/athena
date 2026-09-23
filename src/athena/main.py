"""The Athena web application: a thin FastAPI layer over core and the modules.

`create_app()` is an *application factory* — call it to build a fresh app.
Tests call it with a throwaway database; the server calls it once for real.
No global app state, no surprises.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import logging
import secrets
from pathlib import Path
import sqlite3

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette._utils import get_route_path

from athena import __version__ as athena_version
from athena import config
from athena.aegis import api as aegis_api
from athena.aegis import automation as aegis_automation
from athena.aegis import fleet_attention as aegis_fleet_attention
from athena.aegis import fleet_attention_api as aegis_fleet_attention_api
from athena.aegis import notification_priority_api as aegis_notification_priority_api
from athena.aegis import automation_api as aegis_automation_api
from athena.aegis import delegations_api as aegis_delegations_api
from athena.aegis import desk_api as aegis_desk_api
from athena.workflows import playbook_api as workflows_playbook_api
from athena.workflows import workspace_search_api as workflows_workspace_search_api
from athena.aegis import dispatch_api as aegis_dispatch_api
from athena.aegis import forge_api as aegis_forge_api
from athena.web import chips, render
from athena.aegis import embeds_api as aegis_embeds_api
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
    answerability_api,
    attachments_api,
    budgets,
    db,
    deployment,
    deps,
    identity,
    security_events,
    events_api,
    notifications,
    notifications_api,
    provenance,
    rate_limits,
    run_context,
    run_controls_api,
    search_api,
    security_api,
    sessions,
    tokens_api,
    undo,
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
from athena.web import admin_agents as web_admin_agents
from athena.web import admin_automation as web_admin_automation
from athena.web import admin_security as web_admin_security
from athena.web import issues as web_issues
from athena.web import auth as web_auth
from athena.web import boards as web_boards
from athena.web import filters as web_filters
from athena.web import fleet_metrics as web_fleet_metrics
from athena.web import labels as web_labels
from athena.web import mentor as web_mentor
from athena.web import mentor_graph as web_mentor_graph
from athena.web import mentor_pages as web_mentor_pages
from athena.web import mentor_spaces as web_mentor_spaces
from athena.web import projects as web_projects
from athena.web import work_context as web_work_context
from athena.web import init_templates, palette as web_palette, router as web_router
from athena.middleware import (
    SECURITY_HEADERS,
    DeploymentBoundaryMiddleware,
    IdempotencyMiddleware,
    RequestBodyLimitMiddleware,
    RequestConnectionMiddleware,
    RunContextMiddleware,
    SignedInboundRateLimitMiddleware,
    SlowRequestLogMiddleware,
    _BrowserSessionRequired,
    _is_signed_inbound,
    _require_browser_session,
    content_security_policy,
)


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


# How long a browser may keep a packaged static asset. Long, because the URL
# carries a build fingerprint (see static_version): a changed file is a changed
# URL, so a stale copy is unreachable rather than merely old. `immutable` tells the
# browser not to revalidate even on reload.
STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"


def static_version(static_dir: Path) -> str:
    """A short fingerprint of the packaged static assets, computed once at startup.

    Appended to every static URL the templates emit, so a released change to
    styles.css or the vendored htmx bundle reaches browsers that are holding a
    year-long cached copy of the old one. Content-based rather than a version
    number, because the version does not change on every asset edit and a wrong
    answer here is a user staring at a stale stylesheet.

    No build chain (VISION rule 4): this is a hash over the files themselves, taken
    at startup. Editing a static file while the server runs therefore does not
    change the fingerprint until it restarts — which is the documented trade for
    not having a watcher, and is invisible in the deployments Athena supports,
    where the files change only when the package does.
    """
    digest = hashlib.sha256()
    for path in sorted(static_dir.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(static_dir).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _apply_private_cache_policy(request: Request, response: Response) -> None:
    """Prevent shared or browser caches from retaining sensitive responses."""
    # Packaged static assets are the same bytes for every caller and disclose
    # nothing, so they are exempt: marking them `private, no-store` because the
    # browser happened to send a session cookie made every page load re-fetch the
    # stylesheet, the htmx bundle and the confirm script. They are cached hard and
    # busted by URL instead.
    if _path_matches_root(request.url.path, "/static"):
        response.headers.setdefault("Cache-Control", STATIC_CACHE_CONTROL)
        return
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


def _attach_security_headers(
    response: Response, *, is_https: bool = False, nonce: str | None = None
) -> Response:
    for name, value in SECURITY_HEADERS.items():
        if name == "Content-Security-Policy" and nonce is not None:
            response.headers.setdefault(name, content_security_policy(nonce))
            continue
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
    network_mode: str | None = None,
    allowed_authorities: tuple[str, ...] | None = None,
    expected_server: tuple[str, int] | None = None,
    max_request_body_bytes: int | None = None,
    token_rate_limit_per_minute: int | None = None,
    anon_rate_limit_per_minute: int | None = None,
    login_rate_limit_per_minute: int | None = None,
    login_account_rate_limit_per_minute: int | None = None,
    idempotency_wait_seconds: float | None = None,
    idempotency_lease_seconds: int | None = None,
    idempotency_ttl_seconds: int | None = None,
    idempotency_max_response_bytes: int | None = None,
) -> FastAPI:
    _configure_logging()
    resolved_db = Path(db_path) if db_path is not None else config.DB_PATH
    resolved_network_mode = (
        config.NETWORK_MODE if network_mode is None else network_mode
    )
    resolved_authorities = deployment.normalize_runtime_authorities(
        config.ALLOWED_AUTHORITIES
        if allowed_authorities is None
        else allowed_authorities,
        network_mode=resolved_network_mode,
    )
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
    login_account_limit = (
        config.LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE
        if login_account_rate_limit_per_minute is None
        else login_account_rate_limit_per_minute
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

    app = FastAPI(title="Athena", lifespan=lifespan, docs_url=None, redoc_url=None)
    # The footer states which build rendered the page. Not decoration: a
    # long-running process serves old Python under live-reloaded templates
    # (this exact skew broke every htmx button on 2026-08-08), and a visible
    # version that disagrees with the checkout is the ten-second diagnosis.
    # athena.__version__ is the same source guide.py and recovery bundles
    # stamp, so every surface names one version.
    app.state.version = athena_version
    app.state.build_provenance = provenance.CURRENT_BUILD

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
    # Throttles optional-identity REST reads and signed-inbound attempts by
    # direct client IP. It is not a global browser-request ceiling.
    app.state.anon_rate_limiter = rate_limits.FixedWindowRateLimiter(anon_limit)
    # Throttles POST /login by client IP, before the password hash; see web/auth.py.
    app.state.login_rate_limiter = rate_limits.FixedWindowRateLimiter(login_limit)
    # ...and by the SUBMITTED EMAIL, which is the axis credential stuffing cannot
    # spread across: the per-IP limiter above never sees a distributed run converge
    # on one account. Same limiter, different key.
    app.state.login_account_rate_limiter = rate_limits.FixedWindowRateLimiter(
        login_account_limit
    )

    # Middleware is registered inside-out. Among the general request stack,
    # idempotency wraps the routes and run context wraps idempotency. The body
    # cap is added after session middleware below, so oversized requests are
    # bounded before cookie-controlled SQLite work. The deployment boundary is
    # registered last and sits outside this entire stack.
    async def _browser_session_required(
        request: Request, exc: Exception
    ) -> RedirectResponse:
        if not isinstance(exc, _BrowserSessionRequired):
            raise exc
        del request
        return RedirectResponse("/login", status_code=303)

    app.add_exception_handler(_BrowserSessionRequired, _browser_session_required)

    app.add_middleware(
        IdempotencyMiddleware,
        db_path=resolved_db,
        wait_seconds=idempotency_wait,
        lease_seconds=idempotency_lease,
        ttl_seconds=idempotency_ttl,
        max_response_bytes=idempotency_response_limit,
    )
    app.add_middleware(RunContextMiddleware)

    @app.middleware("http")
    async def attach_session_user(request: Request, call_next):
        # Packaged static assets are identical for everyone and identify nobody, so
        # they skip session resolution entirely. This is not a micro-optimization:
        # the cookie a signed-in browser sends on EVERY asset request was opening a
        # SQLite connection, resolving the session, and — for an admin — building
        # the whole fleet-attention rollup, once per stylesheet and once per script.
        # A page load therefore paid for the rollup several times over, and
        # `private, no-store` (below) guaranteed the browser asked again next time.
        if _path_matches_root(request.url.path, "/static"):
            request.state.user = None
            request.state.csrf_token = None
            request.state.unread_count = 0
            request.state.attention_total = 0
            request.state.attention_signals = []
            request.state.oidc_enabled = False
            return await call_next(request)
        # Resolve the browser session once per request onto request.state.user,
        # so every page (and the nav) knows who is logged in without each route
        # re-doing it. The session's CSRF token rides alongside on
        # request.state.csrf_token, so forms can embed it and verify_csrf can
        # check it. No cookie → no DB hit; both stay None.
        request.state.user = None
        request.state.csrf_token = None
        # Unread-inbox count for the nav badge; 0 when logged out.
        request.state.unread_count = 0
        # Fleet-attention rollup for the Intervene badge. Admin-only below —
        # every input is an admin-scoped read, so for everyone else these
        # stay zero/empty and the template renders nothing.
        request.state.attention_total = 0
        request.state.attention_signals = []
        # Whether SSO is configured, so the nav can show the linked-identities link
        # only when it's relevant. Evaluated per request so tests/config see it live.
        request.state.oidc_enabled = config.oidc_enabled()
        # Machine callbacks authenticate their raw body, not a browser cookie.
        # Ignoring cookies here keeps an attacker-controlled cookie from opening
        # SQLite before the endpoint's HMAC gate.
        signed_inbound = _is_signed_inbound(
            request.method, get_route_path(request.scope)
        )
        raw = None if signed_inbound else request.cookies.get(config.SESSION_COOKIE)
        if raw:
            # The request's one connection (see RequestConnectionMiddleware) rather
            # than a second one opened and thrown away here — resolving a cookie used
            # to cost a full ~2.2 ms attach on top of the route's own.
            conn = deps.request_connection(request).get()
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
                    # The number that decides whether you need to look must not
                    # live only on the dashboard. The same build the dashboard
                    # card runs, so nav and card cannot disagree. Admins only:
                    # the rollup aggregates admin-scoped reads.
                    #
                    # Cost, re-measured on a seeded 10k-issue / 100k-event database
                    # (scripts/seed_benchmark.py, no ANALYZE): 0.11 ms with no
                    # supervision state, 0.50 ms at 5 agents, 1.21 ms at 25, 3.81 ms
                    # at 100. It scales with FLEET SIZE, not with trail size — the
                    # 0075 verb-window index removed the activity scans that used to
                    # make it grow with the log (12.9 ms at 100k events before that
                    # index; the "~0.1 ms" this comment used to claim predated the
                    # measurement). What remains is per-agent work: the active-work
                    # projection, the worker registry, the approval queue.
                    #
                    # There is deliberately NO cache here. A cache would trade
                    # staleness in an attention badge — the signal whose whole job is
                    # to be current — for about half a millisecond at the fleet sizes
                    # this product is for. If a much larger fleet ever makes this
                    # hurt, the honest fix is a short TTL measured against a
                    # re-measurement, not a cache added on the strength of this note.
                    if request.state.user.get("role") == "admin":
                        attention = aegis_fleet_attention.build_attention(conn)
                        request.state.attention_total = attention["total"]
                        request.state.attention_signals = attention["signals"]
            finally:
                # Not closed here: the route, the idempotency publish and any
                # exception handler still need it. The middleware that opened it
                # closes it once, at the end of the request.
                deps.RequestConnection._ensure_clean(conn)
        return await call_next(request)

    # Outside the session middleware, so the one connection also spans the layers
    # that run AFTER the route returns (the idempotency publish, an exception
    # handler recording a refusal). It opens lazily, so sitting this high costs a
    # request that never reaches the database nothing at all.
    app.add_middleware(RequestConnectionMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=body_limit)

    @app.middleware("http")
    async def harden_http(request: Request, call_next):
        # Minted BEFORE the routes run, because the templates have to embed it, and
        # spent in this response's own CSP below. One per response, unguessable, so
        # injected markup cannot carry a matching one.
        request.state.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        _apply_private_cache_policy(request, response)
        return _attach_security_headers(
            response,
            nonce=request.state.csp_nonce,
            is_https=request.url.scheme == "https",
        )

    # A saturated signed-inbound peer is refused before the body cap buffers bytes
    # or browser/session middleware runs.
    app.add_middleware(
        SignedInboundRateLimitMiddleware,
        limiter=app.state.anon_rate_limiter,
    )
    # Inside the deployment boundary, outside everything else: a request refused
    # for an unsupported Host or socket never ran any Athena work, so timing it
    # would report the cost of a rejection as if it were a slow page. Everything
    # past that point — limiter, body cap, session, route, database, idempotency
    # publish — is inside this timer.
    app.add_middleware(
        SlowRequestLogMiddleware,
        threshold_ms=config.SLOW_REQUEST_LOG_MS,
    )
    # Registered last, therefore outermost: unsupported accepted-socket addresses
    # and Host authorities are refused before body, session, limiter, route, or DB
    # work. Empty authorities intentionally make raw unconfigured ASGI startup
    # answer no HTTP requests; athena-serve installs the validated allowlist.
    app.add_middleware(
        DeploymentBoundaryMiddleware,
        network_mode=resolved_network_mode,
        allowed_authorities=resolved_authorities,
        expected_server=expected_server,
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
    # Forge event details carry a URL supplied by an outside system. The filter
    # links it only when its host belongs to a registered source; a template that
    # passes no hosts renders inert text, so a surface that has not opted in
    # degrades to today's behavior rather than breaking.
    templates.env.filters["forge_detail"] = render.render_forge_detail
    # Chip tone mapping (design system: docs/DESIGN_SYSTEM.md). Presentation-only,
    # no database access — see chips.py for which domains are exact vs. best-effort.
    templates.env.filters["status_tone"] = chips.status_tone
    templates.env.filters["category_tone"] = chips.category_tone
    templates.env.filters["priority_tone"] = chips.priority_tone
    templates.env.filters["checkin_tone"] = chips.checkin_tone
    templates.env.filters["health_tone"] = chips.health_tone
    templates.env.filters["token_tone"] = chips.token_tone
    templates.env.filters["sprint_tone"] = chips.sprint_tone
    # The static fingerprint, computed once here rather than per render. Templates
    # append it to every /static URL so a released asset change reaches a browser
    # holding a year-long cached copy (see STATIC_CACHE_CONTROL).
    app.state.static_version = static_version(package_root / "static")
    templates.env.globals["static_version"] = app.state.static_version
    init_templates(templates)
    browser_dependencies = [Depends(_require_browser_session)]
    app.include_router(web_router, dependencies=browser_dependencies)
    app.include_router(web_issues.router, dependencies=browser_dependencies)
    app.include_router(web_projects.router, dependencies=browser_dependencies)
    app.include_router(web_activity.router, dependencies=browser_dependencies)
    app.include_router(web_boards.router, dependencies=browser_dependencies)
    app.include_router(web_filters.router, dependencies=browser_dependencies)
    app.include_router(web_auth.router, dependencies=browser_dependencies)
    app.include_router(web_mentor.router, dependencies=browser_dependencies)
    app.include_router(web_mentor_graph.router, dependencies=browser_dependencies)
    app.include_router(web_mentor_spaces.router, dependencies=browser_dependencies)
    app.include_router(web_mentor_pages.router, dependencies=browser_dependencies)
    app.include_router(web_labels.router, dependencies=browser_dependencies)
    app.include_router(web_admin.router, dependencies=browser_dependencies)
    app.include_router(web_admin_agents.router, dependencies=browser_dependencies)
    app.include_router(web_admin_security.router, dependencies=browser_dependencies)
    app.include_router(web_admin_automation.router, dependencies=browser_dependencies)
    app.include_router(web_work_context.router, dependencies=browser_dependencies)
    app.include_router(web_palette.router, dependencies=browser_dependencies)
    app.include_router(web_fleet_metrics.router, dependencies=browser_dependencies)

    # Core REST API (users, api tokens, cross-module search).
    app.include_router(users_api.router)
    app.include_router(approvals_api.router)
    app.include_router(workers_api.router)
    app.include_router(security_api.router)
    app.include_router(mentor_learnings_api.router)
    app.include_router(aegis_dispatch_api.router)
    app.include_router(aegis_embeds_api.router)
    app.include_router(aegis_forge_api.router)
    app.include_router(tokens_api.router)
    app.include_router(agent_runs_api.router)
    app.include_router(run_controls_api.router)
    app.include_router(answerability_api.router)
    app.include_router(aegis_desk_api.router)
    app.include_router(workflows_playbook_api.router)
    # Before search_api: both live under /search, and the workspace route is the
    # more specific path. Registration order does not decide the match here (the
    # paths are distinct), but keeping the composed route adjacent to its layer's
    # other router is what makes the layering legible in one read.
    app.include_router(workflows_workspace_search_api.router)
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
    app.include_router(aegis_fleet_attention_api.router)
    app.include_router(aegis_notification_priority_api.router)
    app.include_router(aegis_sprints_api.router)
    app.include_router(aegis_automation_api.router)
    app.include_router(aegis_work_context_api.router)
    app.include_router(aegis_fleet_metrics_api.router)

    # Mentor REST API (spaces + pages + versions).
    app.include_router(mentor_api.spaces_router)
    app.include_router(mentor_api.pages_router)

    # Framework-generated browser HTML follows the same session-only policy as
    # Athena web routes. The machine-readable OpenAPI document remains public
    # product metadata; it carries no workspace rows or credential facts.
    @app.get(
        "/docs",
        include_in_schema=False,
        dependencies=[Depends(_require_browser_session)],
    )
    def swagger_ui() -> Response:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url="/docs/oauth2-redirect",
        )

    @app.get(
        "/docs/oauth2-redirect",
        include_in_schema=False,
        dependencies=[Depends(_require_browser_session)],
    )
    def swagger_ui_redirect() -> Response:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get(
        "/redoc",
        include_in_schema=False,
        dependencies=[Depends(_require_browser_session)],
    )
    def redoc() -> Response:
        return get_redoc_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} - ReDoc",
        )

    @app.get("/healthz")
    def healthz():
        """Liveness check — cheap, no DB hit. Used by tests and monitoring."""
        return {"status": "ok"}

    @app.get("/version")
    def version(request: Request):
        """Code identity snapshot — public metadata with no database access."""
        return request.app.state.build_provenance.as_dict()

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


# Direct Uvicorn startup through this module-global instance is an unsupported
# development escape hatch. athena-serve constructs a fresh app pinned to the
# exact listener it preflighted and hands that object to Uvicorn directly.
app = create_app()
