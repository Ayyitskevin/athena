"""CSRF protection for the browser write path.

The synchronizer-token pattern: every browser session carries an unguessable
token (minted in core/sessions.py, stashed on request.state.csrf_token by the
main.py middleware). Each form we render embeds it; `verify_csrf` requires it
back on every state-changing POST.

Why this is safe: a cross-site page can make the victim's browser SEND a request,
but the same-origin policy stops it from READING the token (neither the rendered
form nor the readable csrf cookie is reachable cross-origin), so it cannot forge
a valid submission.

Scope: this guards COOKIE-authenticated requests only. The REST API authenticates
agents with bearer tokens / the actor header, never the session cookie — those
requests carry no ambient cookie to forge, so they need no CSRF token and this
dependency is wired only onto the web (cookie) routes. When no session is present,
the dependency is a no-op and the route's own auth (a 401) still applies.
"""

from __future__ import annotations

import secrets

from fastapi import Form, Header, HTTPException, Request


def verify_csrf(
    request: Request,
    csrf_token: str = Form(""),
    x_csrf_token: str | None = Header(default=None),
) -> None:
    """Reject a state-changing request whose CSRF token is missing or wrong.

    The token may arrive in the `csrf_token` form field (server-rendered forms,
    incl. HTMX which serializes the hidden input) or an `X-CSRF-Token` header
    (scripted clients). No session → nothing to forge → allow (the handler's own
    auth gate still runs)."""
    expected = getattr(request.state, "csrf_token", None)
    if not expected:
        return
    submitted = x_csrf_token or csrf_token
    if not submitted or not secrets.compare_digest(submitted, expected):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
