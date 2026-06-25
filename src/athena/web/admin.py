"""Browser administration and token-management routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.core import identity, tokens, users
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates

router = APIRouter()


def _signin_required(verb: str) -> HTMLResponse:
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


def _admin_required(user: dict | None) -> HTMLResponse | None:
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


def _selected_scopes(
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


@router.get("/settings/tokens", response_class=HTMLResponse)
def token_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    err = _write_required(user, "create tokens")
    if err is not None:
        return err
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request=request,
            name="settings/tokens.html",
            context=_token_context(conn, user, error="Token name is required."),
            status_code=400,
        )
    scopes = _selected_scopes(
        scope_read, scope_issue_write, scope_docs_write, scope_admin
    )
    try:
        created = tokens.create_token(
            conn, user_id=user["id"], name=name, scopes=scopes
        )
    except ValueError as exc:
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
    if not tokens.revoke_token(conn, user_id=user["id"], token_id=token_id):
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
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
        users.create_user(
            conn,
            email=email,
            name=name,
            password=password.strip() or None,
            role=role,
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
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
    users.set_password(conn, user_id, password)
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    target = users.get_user(conn, user_id)
    if target is None:
        return HTMLResponse('<div class="error">No such user.</div>', status_code=404)
    if target["role"] == users.ADMIN_ROLE and role != users.ADMIN_ROLE:
        if users.count_admins(conn) <= 1:
            return templates.TemplateResponse(
                request=request,
                name="admin/users.html",
                context=_admin_context(conn, error="Cannot remove the last admin."),
                status_code=409,
            )
    try:
        users.set_role(conn, user_id, role)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context=_admin_context(conn, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse("/admin/users", status_code=303)
