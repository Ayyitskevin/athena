"""Browser login: the session half of authentication.

The REST API authenticates agents with bearer tokens (core/tokens.py). Humans
in a browser log in here: POST email+password, get a session cookie, and every
later request is identified by it. The cookie is HttpOnly (JS can't read it) and
SameSite=Lax; it carries the Secure flag only when config.COOKIE_SECURE is on.

Who-is-logged-in for every page is resolved once, in main.py's middleware, onto
request.state.user — these routes only mint and tear down the session.
"""
from __future__ import annotations

import secrets
import sqlite3
import urllib.error
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import jwt

from athena import config
from athena.core import oidc, oidc_flow, sessions, users
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates

router = APIRouter()

# Pins an in-flight SSO login to the browser that started it: set at /login/sso,
# required to match the returned `state` at /auth/callback (login CSRF/fixation
# defense). HttpOnly + SameSite=Lax (Lax, not Strict — the IdP returns via a
# top-level cross-site GET, which Strict would strip the cookie from). Short-lived.
_OIDC_STATE_COOKIE = "athena_oidc_state"
_OIDC_STATE_TTL_SECONDS = 600


def _cookie_secure(request: Request) -> bool:
    """Whether to set the Secure flag: the operator's explicit COOKIE_SECURE, OR the
    request actually arrived over HTTPS. The scheme check auto-secures a direct-HTTPS
    deploy so a forgotten ATHENA_COOKIE_SECURE can't leak the session cookie in the
    clear; behind a TLS-terminating proxy (scheme=http) COOKIE_SECURE stays the switch.
    Only ever ADDS Secure on a real HTTPS request — never drops it on local http."""
    return config.COOKIE_SECURE or request.url.scheme == "https"


def _set_session_cookie(response, raw: str, *, request: Request) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        raw,
        max_age=config.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def _set_csrf_cookie(response, csrf: str, *, request: Request) -> None:
    # NOT HttpOnly on purpose: a same-origin script (or HTMX) may read this to
    # echo the token in an X-CSRF-Token header. Same-origin policy still stops a
    # cross-site attacker from reading it, so the double-submit guarantee holds.
    # Server-rendered forms get the token from request.state.csrf_token instead,
    # so plain HTML forms work without any JavaScript.
    response.set_cookie(
        config.CSRF_COOKIE,
        csrf,
        max_age=config.SESSION_TTL_DAYS * 24 * 3600,
        httponly=False,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def _is_cross_site_post(request: Request) -> bool:
    """True when the request carries an Origin whose host differs from ours — a cross-site
    form POST, the login-CSRF vector. A browser ALWAYS sends Origin on a cross-origin POST,
    so the actual attack (a victim's browser auto-submitting an attacker's login form to
    log them into an attacker-controlled account) is caught. A missing Origin is treated as
    same-site: non-browser clients (curl, the API) omit it, and there is no CSRF risk
    without a browser. (Deploy note: front Athena so the request's Host header is the public
    host — a proxy that rewrites Host without forwarding the original could false-positive.)"""
    origin = request.headers.get("origin")
    if not origin:
        return False
    return urlsplit(origin).netloc != request.headers.get("host", "")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    templates = get_templates()
    # Already signed in? Skip the form.
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/aegis", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"oidc_enabled": config.oidc_enabled()},
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()

    # Login CSRF defense: /login has no session yet, so it can't use the session-bound
    # double-submit token every other mutating route does. Reject a cross-site POST so an
    # attacker can't auto-submit their own credentials and silently log the victim into an
    # attacker-controlled account (SameSite=Lax does not stop this — login needs no cookie).
    if _is_cross_site_post(request):
        return HTMLResponse(
            '<div class="error">Cross-site login blocked.</div>', status_code=403
        )

    # Brute-force / credential-stuffing throttle, checked BEFORE the password hash so a
    # flood is bounded by the limiter, not by pbkdf2's per-guess CPU cost. Keyed by the
    # direct peer IP; over the limit returns 429 with Retry-After. Off when the limiter
    # is unset or its limit is 0.
    limiter = getattr(request.app.state, "login_rate_limiter", None)
    if limiter is not None:
        client_ip = request.client.host if request.client else "unknown"
        decision = limiter.check(client_ip)
        if not decision.allowed:
            return HTMLResponse(
                '<div class="error">Too many login attempts. Please wait and try again.</div>',
                status_code=429,
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

    user = users.verify_credentials(conn, email=email, password=password)
    if user is None:
        # One opaque message — never reveal whether the email exists.
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Invalid email or password.",
                "email": email,
                "oidc_enabled": config.oidc_enabled(),
            },
            status_code=401,
        )

    # Rotate the session at the auth boundary: tear down whatever session the
    # browser arrived with before minting the new one. create_session already
    # issues a fresh random cookie value, so a planted/"fixed" value can't survive
    # login on its own — but destroying the prior row too means a re-login or an
    # account switch leaves no still-valid session orphaned server-side, and any
    # cookie the browser presented is positively killed rather than merely
    # overwritten in the response.
    sessions.destroy_session(conn, request.cookies.get(config.SESSION_COOKIE))
    raw = sessions.create_session(conn, user["id"])
    # 303 so the browser re-requests /aegis with GET, carrying the new cookie.
    response = RedirectResponse("/aegis", status_code=303)
    _set_session_cookie(response, raw, request=request)
    _set_csrf_cookie(response, sessions.csrf_token_for(conn, raw), request=request)
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    sessions.destroy_session(conn, request.cookies.get(config.SESSION_COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    response.delete_cookie(config.CSRF_COOKIE, path="/")
    return response


# --- SSO (OpenID Connect) login -------------------------------------------


def _set_oidc_state_cookie(response, state: str, *, request: Request) -> None:
    response.set_cookie(
        _OIDC_STATE_COOKIE,
        state,
        max_age=_OIDC_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def _sso_error(request, templates, message: str, *, status: int = 400):
    """Re-render the login page with an SSO error and clear the in-flight state
    cookie. The message is deliberately generic — never leak which step failed."""
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": message, "oidc_enabled": config.oidc_enabled()},
        status_code=status,
    )
    response.delete_cookie(_OIDC_STATE_COOKIE, path="/")
    return response


@router.get("/login/sso")
def login_sso(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Begin an SSO login: generate the per-login secrets, stash them, and redirect
    the browser to the IdP. 404 when SSO isn't configured, so an unconfigured deploy
    exposes nothing."""
    templates = get_templates()
    if not config.oidc_enabled():
        return HTMLResponse("Not found", status_code=404)
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/aegis", status_code=303)

    try:
        discovery = oidc_flow.discover(config.OIDC_ISSUER)
    except (oidc_flow.OidcError, urllib.error.URLError, ValueError):
        return _sso_error(
            request, templates, "SSO is unavailable right now.", status=502
        )

    state = oidc_flow.new_state()
    nonce = oidc_flow.new_nonce()
    verifier = oidc_flow.new_pkce_verifier()
    oidc.start_login(conn, state=state, nonce=nonce, code_verifier=verifier)

    url = oidc_flow.build_authorization_url(
        discovery["authorization_endpoint"],
        client_id=config.OIDC_CLIENT_ID,
        redirect_uri=config.OIDC_REDIRECT_URL,
        state=state,
        nonce=nonce,
        code_challenge=oidc_flow.pkce_challenge(verifier),
    )
    response = RedirectResponse(url, status_code=302)
    _set_oidc_state_cookie(response, state, request=request)
    return response


@router.get("/auth/callback")
def auth_callback(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Finish an SSO login: verify the callback is the one we started, exchange the
    code, validate the ID token, map it to a user, and mint a session — the same
    session a password login produces."""
    templates = get_templates()
    if not config.oidc_enabled():
        return HTMLResponse("Not found", status_code=404)

    params = request.query_params
    if params.get("error"):
        return _sso_error(request, templates, "SSO sign-in was cancelled or failed.")

    state = params.get("state") or ""
    code = params.get("code") or ""
    cookie_state = request.cookies.get(_OIDC_STATE_COOKIE) or ""
    # The returned state must match the cookie we set at the start — this binds the
    # callback to THIS browser (defeating a forged/replayed callback / login CSRF).
    if not (state and cookie_state and secrets.compare_digest(state, cookie_state)):
        return _sso_error(
            request, templates, "SSO sign-in could not be verified. Please try again."
        )
    pending = oidc.take_login_state(conn, state, max_age_seconds=_OIDC_STATE_TTL_SECONDS)
    if pending is None or not code:
        return _sso_error(
            request, templates, "SSO sign-in expired. Please try again."
        )

    try:
        discovery = oidc_flow.discover(config.OIDC_ISSUER)
        token = oidc_flow.exchange_code(
            discovery["token_endpoint"],
            code=code,
            redirect_uri=config.OIDC_REDIRECT_URL,
            client_id=config.OIDC_CLIENT_ID,
            client_secret=config.OIDC_CLIENT_SECRET,
            code_verifier=pending["code_verifier"],
        )
        claims = oidc_flow.verify_id_token(
            token["id_token"],
            jwks_uri=discovery["jwks_uri"],
            issuer=config.OIDC_ISSUER,
            client_id=config.OIDC_CLIENT_ID,
            nonce=pending["nonce"],
        )
        user = oidc_flow.provision_or_link(
            conn,
            issuer=config.OIDC_ISSUER,
            claims=claims,
            allowed_domains=config.OIDC_ALLOWED_DOMAINS,
        )
    except (
        oidc_flow.OidcError,
        jwt.InvalidTokenError,
        urllib.error.URLError,
        KeyError,
        ValueError,
    ):
        # One generic message — don't reveal which step (exchange/verify/policy) failed.
        return _sso_error(request, templates, "SSO sign-in failed.")

    # Mint the session exactly as a password login does (rotate, then issue).
    sessions.destroy_session(conn, request.cookies.get(config.SESSION_COOKIE))
    raw = sessions.create_session(conn, user["id"])
    response = RedirectResponse("/aegis", status_code=303)
    _set_session_cookie(response, raw, request=request)
    _set_csrf_cookie(response, sessions.csrf_token_for(conn, raw), request=request)
    response.delete_cookie(_OIDC_STATE_COOKIE, path="/")
    return response
