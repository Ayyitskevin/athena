"""Browser login: the session half of authentication.

The REST API authenticates agents with bearer tokens (core/tokens.py). Humans
in a browser log in here: POST email+password, get a session cookie, and every
later request is identified by it. The cookie is HttpOnly (JS can't read it) and
SameSite=Lax; it carries the Secure flag only when config.COOKIE_SECURE is on.

Who-is-logged-in for every page is resolved once, in main.py's middleware, onto
request.state.user — these routes only mint and tear down the session.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena import config
from athena.core import sessions, users
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates

router = APIRouter()


def _set_session_cookie(response, raw: str) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        raw,
        max_age=config.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )


def _set_csrf_cookie(response, csrf: str) -> None:
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
        secure=config.COOKIE_SECURE,
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    # Already signed in? Skip the form.
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/aegis", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    user = users.verify_credentials(conn, email=email, password=password)
    if user is None:
        # One opaque message — never reveal whether the email exists.
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid email or password.", "email": email},
            status_code=401,
        )

    raw = sessions.create_session(conn, user["id"])
    # 303 so the browser re-requests /aegis with GET, carrying the new cookie.
    response = RedirectResponse("/aegis", status_code=303)
    _set_session_cookie(response, raw)
    _set_csrf_cookie(response, sessions.csrf_token_for(conn, raw))
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    sessions.destroy_session(conn, request.cookies.get(config.SESSION_COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    response.delete_cookie(config.CSRF_COOKIE, path="/")
    return response
