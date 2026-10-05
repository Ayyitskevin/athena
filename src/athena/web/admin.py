"""Browser administration and settings routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena import config
from athena.core import (
    activity,
    identity,
    oidc,
    oidc_commands,
    run_controls,
    token_commands,
    tokens,
    user_commands,
    users,
    webhook_commands,
    webhooks,
)
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates

router = APIRouter()

ACTIVE_WORK_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie",
}

_USER_COMMAND_WEB_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "invalid": 400,
}


def _signin_required(verb: str) -> HTMLResponse:
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


def admin_required(user: dict | None) -> HTMLResponse | None:
    if user is None:
        return _signin_required("use admin tools")
    if not identity.is_admin(user):
        return HTMLResponse(
            '<div class="blocked">Admin role required.</div>', status_code=403
        )
    return None


def _write_required(user: dict | None, verb: str) -> HTMLResponse | None:
    if user is None:
        return _signin_required(verb)
    if not identity.can_write(user):
        return HTMLResponse(
            '<div class="blocked">Viewer role is read-only.</div>', status_code=403
        )
    return None


def selected_scopes(
    read: str | None,
    issue_write: str | None,
    docs_write: str | None,
    admin: str | None,
) -> list[str]:
    scopes: list[str] = []
    if read:
        scopes.append(tokens.READ_SCOPE)
    if issue_write:
        scopes.append(tokens.ISSUE_WRITE_SCOPE)
    if docs_write:
        scopes.append(tokens.DOCS_WRITE_SCOPE)
    if admin:
        scopes.append(tokens.ADMIN_SCOPE)
    return scopes


def _token_context(
    conn: sqlite3.Connection,
    user: dict,
    *,
    created: dict | None = None,
    error: str | None = None,
) -> dict:
    return {
        "tokens": tokens.list_tokens(conn, user["id"]),
        "created": created,
        "error": error,
        "can_manage_tokens": identity.can_write(user),
        "available_scopes": [
            (tokens.READ_SCOPE, "Read"),
            (tokens.ISSUE_WRITE_SCOPE, "Aegis writes"),
            (tokens.DOCS_WRITE_SCOPE, "Mentor writes"),
            (tokens.ADMIN_SCOPE, "Admin"),
        ],
    }


def _password_context(*, error: str | None = None, success: str | None = None) -> dict:
    return {"error": error, "success": success}


# --- Presentation preferences ------------------------------------------------
#
# Two cookie writes, no database row: which theme this BROWSER renders and
# whether its nav rail is collapsed are per-device presentation state, not
# facts about the operator, so they deliberately live outside the data model
# the way the session cookie does. base.html reads both and degrades to
# prefers-color-scheme + an open rail when they are absent, which is also why
# these are POSTs from plain forms — the buttons are progressive enhancement
# over working defaults.


def _safe_next(next_path: str) -> str:
    """Only ever redirect back into this app using an absolute local path.

    The value rides in a hidden form field, so a tampered form must not turn
    a preference toggle into an open redirect. Anything that is not a plain
    local path ("//host" is protocol-relative, "https://…" is absolute, and
    backslashes are authority separators in WHATWG URL parsing) falls back to
    the home page. Reject controls as well instead of relying on response-header
    serialization to make an unsafe value inert.
    """
    has_control = any(ord(char) < 0x20 or ord(char) == 0x7F for char in next_path)
    if (
        next_path.startswith("/")
        and not next_path.startswith("//")
        and "\\" not in next_path
        and not has_control
    ):
        return next_path
    return "/"


_THEMES = {"dark", "light", "system"}


@router.post("/settings/theme", dependencies=[Depends(verify_csrf)])
def set_theme(
    request: Request,
    theme: str = Form(...),
    next: str = Form("/"),
):
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("change the theme")
    if theme not in _THEMES:
        return HTMLResponse("unknown theme", status_code=400)
    response = RedirectResponse(_safe_next(next), status_code=303)
    if theme == "system":
        # No cookie IS the system setting; deleting beats storing a synonym.
        response.delete_cookie("athena_theme", path="/")
    else:
        response.set_cookie(
            "athena_theme",
            theme,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=config.COOKIE_SECURE or request.url.scheme == "https",
            path="/",
        )
    return response


@router.post("/settings/rail", dependencies=[Depends(verify_csrf)])
def set_rail(
    request: Request,
    state: str = Form(...),
    next: str = Form("/"),
):
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("collapse the rail")
    if state not in ("collapsed", "open"):
        return HTMLResponse("unknown rail state", status_code=400)
    response = RedirectResponse(_safe_next(next), status_code=303)
    if state == "open":
        response.delete_cookie("athena_rail", path="/")
    else:
        response.set_cookie(
            "athena_rail",
            "collapsed",
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=config.COOKIE_SECURE or request.url.scheme == "https",
            path="/",
        )
    return response


@router.get("/settings/password", response_class=HTMLResponse)
def password_settings(request: Request, updated: str | None = None):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("change your password")
    return templates.TemplateResponse(
        request=request,
        name="settings/password.html",
        context=_password_context(
            success="Password updated." if updated else None,
        ),
    )


@router.post(
    "/settings/password",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def update_own_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("change your password")

    new_password = new_password.strip()
    confirm_password = confirm_password.strip()
    if not current_password.strip():
        return templates.TemplateResponse(
            request=request,
            name="settings/password.html",
            context=_password_context(error="Current password is required."),
            status_code=400,
        )
    if not new_password:
        return templates.TemplateResponse(
            request=request,
            name="settings/password.html",
            context=_password_context(error="New password is required."),
            status_code=400,
        )
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="settings/password.html",
            context=_password_context(error="New passwords do not match."),
            status_code=400,
        )
    # The command owns the ownership check, the hash write, the revocation of every
    # OTHER session (so a device signed in on the old password can't keep riding a
    # live cookie for up to SESSION_TTL_DAYS, while this browser stays signed in),
    # and the atomic 'password_changed' event.
    try:
        user_commands.change_own_password(
            conn,
            actor=user,
            current_password=current_password,
            new_password=new_password,
            keep_session_raw=request.cookies.get(config.SESSION_COOKIE),
        )
    except user_commands.UserCommandError as exc:
        return templates.TemplateResponse(
            request=request,
            name="settings/password.html",
            context=_password_context(
                error=(
                    "Current password is incorrect."
                    if exc.kind == "bad_request"
                    else "Password could not be changed."
                )
            ),
            status_code=_USER_COMMAND_WEB_STATUS[exc.kind],
        )
    return RedirectResponse("/settings/password?updated=1", status_code=303)


def _identities_context(
    conn: sqlite3.Connection, user: dict, *, error: str | None = None
) -> dict:
    identities = oidc.list_identities(conn, user["id"])
    # request.state.user has no password_hash (sessions strips it), so re-read.
    target = users.get_user(conn, user["id"])
    has_password = bool(target and target.get("password_hash"))
    return {
        "identities": identities,
        # A user must keep at least one way to sign in: with no password, the LAST
        # remaining identity is protected from unlinking (template + handler both
        # enforce it). With a password, any identity can be unlinked.
        "has_password": has_password,
        "error": error,
    }


@router.get("/settings/identities", response_class=HTMLResponse)
def identities_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage your linked sign-ins")
    return templates.TemplateResponse(
        request=request,
        name="settings/identities.html",
        context=_identities_context(conn, user),
    )


@router.post(
    "/settings/identities/unlink",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def unlink_identity(
    request: Request,
    issuer: str = Form(""),
    subject: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage your linked sign-ins")

    identities = oidc.list_identities(conn, user["id"])
    # Only the current user's OWN identities may be unlinked — confirm the pair is in
    # their list before deleting (the (issuer, subject) pair maps to exactly one user,
    # so this is the authorization check, not just a 404 guard).
    owns_it = any(i["issuer"] == issuer and i["subject"] == subject for i in identities)
    if not owns_it:
        return HTMLResponse(
            '<div class="error">No such linked sign-in.</div>', status_code=404
        )

    target = users.get_user(conn, user["id"])
    has_password = bool(target and target.get("password_hash"))
    # Don't let a user remove their only way back in.
    if not has_password and len(identities) <= 1:
        return templates.TemplateResponse(
            request=request,
            name="settings/identities.html",
            context=_identities_context(
                conn,
                user,
                error="You can't unlink your only sign-in method. Set a password first.",
            ),
            status_code=409,
        )

    # The command removes the link AND records the atomic 'unlinked_identity' event,
    # attributed to the acting user (this only reaches their own identities), so
    # dropping a sign-in method is never a silent write.
    oidc_commands.unlink_identity(
        conn, actor_id=user["id"], issuer=issuer, subject=subject
    )
    return RedirectResponse("/settings/identities", status_code=303)


@router.get("/settings/tokens", response_class=HTMLResponse)
def token_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage tokens")
    return templates.TemplateResponse(
        request=request,
        name="settings/tokens.html",
        context=_token_context(conn, user),
    )


@router.post(
    "/settings/tokens", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)]
)
def create_token(
    request: Request,
    name: str = Form(""),
    scope_read: str | None = Form(None),
    scope_issue_write: str | None = Form(None),
    scope_docs_write: str | None = Form(None),
    scope_admin: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _write_required(user, "create tokens")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request=request,
            name="settings/tokens.html",
            context=_token_context(conn, user, error="Token name is required."),
            status_code=400,
        )
    scopes = selected_scopes(
        scope_read, scope_issue_write, scope_docs_write, scope_admin
    )
    try:
        created = token_commands.mint_token(
            conn, actor_id=user["id"], name=name, scopes=scopes
        )
    except token_commands.TokenCommandError as exc:
        return templates.TemplateResponse(
            request=request,
            name="settings/tokens.html",
            context=_token_context(conn, user, error=str(exc)),
            status_code=400,
        )
    return templates.TemplateResponse(
        request=request,
        name="settings/tokens.html",
        context=_token_context(conn, user, created=created),
        status_code=201,
    )


@router.post("/settings/tokens/{token_id}/revoke", dependencies=[Depends(verify_csrf)])
def revoke_token(
    request: Request, token_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = _write_required(user, "revoke tokens")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    if not token_commands.revoke_token(conn, actor_id=user["id"], token_id=token_id):
        return HTMLResponse(
            '<div class="error">No such live token.</div>', status_code=404
        )
    return RedirectResponse("/settings/tokens", status_code=303)


def _admin_context(
    conn: sqlite3.Connection,
    *,
    error: str | None = None,
    success: str | None = None,
) -> dict:
    return {
        "users": users.list_users(conn),
        "roles": users.ROLES,
        "error": error,
        "success": success,
    }


@router.get("/admin/users", response_class=HTMLResponse)
def users_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    return templates.TemplateResponse(
        request=request, name="admin/users.html", context=_admin_context(conn)
    )


@router.post(
    "/admin/users", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)]
)
def create_user(
    request: Request,
    email: str = Form(""),
    name: str = Form(""),
    password: str = Form(""),
    role: str = Form(users.DEFAULT_ROLE),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    actor = getattr(request.state, "user", None)
    err = admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "admin_required accepted a missing user"
    email = email.strip()
    name = name.strip()
    if not email or not name:
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error="Email and name are required."),
            status_code=400,
        )
    try:
        # The command owns the insert AND its atomic 'created_user' audit event,
        # attributed to the acting admin.
        user_commands.create_user(
            conn,
            actor_id=actor["id"],
            email=email,
            name=name,
            password=password.strip() or None,
            role=role,
            is_agent=False,
        )
    except user_commands.UserCommandError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error=exc.detail),
            status_code=_USER_COMMAND_WEB_STATUS[exc.kind],
        )
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error="Email already in use."),
            status_code=400,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse("/admin/users", status_code=303)


@router.post(
    "/admin/users/{user_id}/password",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def update_user_password(
    request: Request,
    user_id: int,
    password: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    actor = getattr(request.state, "user", None)
    err = admin_required(actor)
    if err is not None:
        return err
    target = users.get_user(conn, user_id)
    if target is None:
        return HTMLResponse('<div class="error">No such user.</div>', status_code=404)
    password = password.strip()
    if not password:
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error="Password is required."),
            status_code=400,
        )
    # The command owns the admin authorization, the hash write, the revocation of
    # EVERY live session (an admin resetting a compromised or departing user's
    # password must end that access now, not after SESSION_TTL_DAYS), and the atomic
    # 'password_reset' event — the privilege trail this lever previously lacked.
    try:
        user_commands.reset_user_password(
            conn, actor=actor, target_user_id=user_id, password=password
        )
    except user_commands.UserCommandError as exc:
        if exc.kind == "not_found":
            return HTMLResponse(
                '<div class="error">No such user.</div>', status_code=404
            )
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error=str(exc)),
            status_code=_USER_COMMAND_WEB_STATUS[exc.kind],
        )
    return RedirectResponse("/admin/users", status_code=303)


@router.post(
    "/admin/users/{user_id}/role",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def update_user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    actor = getattr(request.state, "user", None)
    err = admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "admin_required accepted a missing user"
    try:
        user_commands.set_user_role(
            conn, actor_id=actor["id"], target_user_id=user_id, role=role
        )
    except user_commands.UserCommandError as exc:
        if exc.kind == "not_found":
            return HTMLResponse(
                '<div class="error">No such user.</div>', status_code=404
            )
        # last-admin (409) keeps its fixed message; an invalid role re-renders at 400
        # with the validator's own text — matching the pre-command handler.
        if exc.kind == "conflict":
            message, status = "Cannot remove the last admin.", 409
        else:
            message, status = str(exc), 400
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error=message),
            status_code=status,
        )
    return RedirectResponse("/admin/users", status_code=303)


@router.post(
    "/admin/users/{user_id}/agent",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def update_user_agent(
    request: Request,
    user_id: int,
    is_agent: str = Form("0"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    actor = getattr(request.state, "user", None)
    err = admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "admin_required accepted a missing user"
    # The form posts the DESIRED next state ("1" to mark as agent, anything else to
    # mark as human), so the button is a deterministic toggle, not a read-then-flip.
    try:
        user_commands.set_user_agent(
            conn,
            actor_id=actor["id"],
            target_user_id=user_id,
            is_agent=is_agent == "1",
        )
    except user_commands.UserCommandError:
        return HTMLResponse('<div class="error">No such user.</div>', status_code=404)
    return RedirectResponse("/admin/users", status_code=303)


def _webhooks_context(
    conn: sqlite3.Connection,
    *,
    created: dict | None = None,
    error: str | None = None,
) -> dict:
    return {
        "webhooks": webhooks.list_webhooks(conn),
        # The signing secret is shown exactly once, right after creation.
        "created": created,
        "error": error,
        # Offer the kinds that actually occur in the trail as filter options (plus
        # "all"), so the list never drifts from what the recorders emit.
        "event_kinds": activity.distinct_target_kinds(conn),
    }


CONTROL_STATE_FILTERS = (
    run_controls.STATE_FILTER_OPEN,
    *run_controls.CONTROL_STATES,
    "all",
)


@router.get("/admin/webhooks", response_class=HTMLResponse)
def webhooks_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    return templates.TemplateResponse(
        request=request, name="admin/webhooks.html", context=_webhooks_context(conn)
    )


@router.post(
    "/admin/webhooks", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)]
)
def create_webhook(
    request: Request,
    url: str = Form(""),
    event_kind: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    actor = getattr(request.state, "user", None)
    err = admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "admin_required accepted a missing user"
    url = url.strip()
    # Same SSRF guard the REST API applies — refuse a private/loopback/malformed URL
    # at the boundary rather than at delivery time.
    ok, reason = webhooks.is_safe_url(url)
    if not ok:
        return templates.TemplateResponse(
            request=request,
            name="admin/webhooks.html",
            context=_webhooks_context(conn, error=reason),
            status_code=400,
        )
    # The command owns the registration, its atomic audit event, and the "start at
    # tip" cursor (only future events reach the endpoint, never the backlog).
    created = webhook_commands.register_webhook(
        conn,
        actor_id=actor["id"],
        url=url,
        event_kind=event_kind.strip() or None,
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/webhooks.html",
        context=_webhooks_context(conn, created=created),
        status_code=201,
    )


@router.post(
    "/admin/webhooks/{webhook_id}/active",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def toggle_webhook(
    request: Request,
    webhook_id: int,
    active: str = Form("0"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    actor = getattr(request.state, "user", None)
    err = admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "admin_required accepted a missing user"
    # The form posts the DESIRED next state ("1" resume, anything else pause) — a
    # deterministic toggle. Resuming clears the backoff so it retries promptly. The
    # command records the flip atomically.
    try:
        webhook_commands.set_webhook_active(
            conn, actor_id=actor["id"], webhook_id=webhook_id, active=active == "1"
        )
    except webhook_commands.WebhookCommandError:
        return HTMLResponse(
            '<div class="error">No such webhook.</div>', status_code=404
        )
    return RedirectResponse("/admin/webhooks", status_code=303)


@router.post(
    "/admin/webhooks/{webhook_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def delete_webhook(
    request: Request,
    webhook_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    actor = getattr(request.state, "user", None)
    err = admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "admin_required accepted a missing user"
    if not webhook_commands.delete_webhook(
        conn, actor_id=actor["id"], webhook_id=webhook_id
    ):
        return HTMLResponse(
            '<div class="error">No such webhook.</div>', status_code=404
        )
    return RedirectResponse("/admin/webhooks", status_code=303)


# --- automation rules -------------------------------------------------------
