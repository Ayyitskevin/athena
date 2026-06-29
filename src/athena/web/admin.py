"""Browser administration and settings routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import automation, projects, statuses
from athena.core import activity, identity, oidc, tokens, users, webhooks
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


def _password_context(*, error: str | None = None, success: str | None = None) -> dict:
    return {"error": error, "success": success}


@router.get("/settings/password", response_class=HTMLResponse)
def password_settings(request: Request, updated: str | None = None):
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
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
    if (
        users.verify_credentials(conn, email=user["email"], password=current_password)
        is None
    ):
        return templates.TemplateResponse(
            request=request,
            name="settings/password.html",
            context=_password_context(error="Current password is incorrect."),
            status_code=400,
        )

    users.set_password(conn, user["id"], new_password)
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage your linked sign-ins")

    identities = oidc.list_identities(conn, user["id"])
    # Only the current user's OWN identities may be unlinked — confirm the pair is in
    # their list before deleting (the (issuer, subject) pair maps to exactly one user,
    # so this is the authorization check, not just a 404 guard).
    owns_it = any(
        i["issuer"] == issuer and i["subject"] == subject for i in identities
    )
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

    oidc.unlink_identity(conn, issuer=issuer, subject=subject)
    return RedirectResponse("/settings/identities", status_code=303)


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
    is_agent: str | None = Form(None),
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
            is_agent=is_agent is not None,
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
    # The form posts the DESIRED next state ("1" to mark as agent, anything else to
    # mark as human), so the button is a deterministic toggle, not a read-then-flip.
    users.set_agent(conn, user_id, is_agent == "1")
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


@router.get("/admin/webhooks", response_class=HTMLResponse)
def webhooks_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
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
    created = webhooks.create_webhook(
        conn,
        url=url,
        event_kind=event_kind.strip() or None,
        created_by=actor["id"],
        # Start at the current tip so the endpoint receives only future events.
        start_cursor=webhooks.current_tip(conn),
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
    err = _admin_required(actor)
    if err is not None:
        return err
    # The form posts the DESIRED next state ("1" resume, anything else pause) — a
    # deterministic toggle. Resuming clears the backoff so it retries promptly.
    if webhooks.set_webhook_active(conn, webhook_id, active == "1") is None:
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
    err = _admin_required(actor)
    if err is not None:
        return err
    if not webhooks.delete_webhook(conn, webhook_id):
        return HTMLResponse(
            '<div class="error">No such webhook.</div>', status_code=404
        )
    return RedirectResponse("/admin/webhooks", status_code=303)


# --- automation rules -------------------------------------------------------


def _int_or_none(raw: str) -> int | None:
    """A select's value as an int, or None for the empty ('any'/unset) option. Selects
    only ever submit a known id or '', so a non-numeric value is treated as unset."""
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _automation_context(conn: sqlite3.Connection, *, error: str | None = None) -> dict:
    """Everything the rule-builder page renders: the existing rules plus the option
    sets the create form offers (the SAME closed sets the validator enforces, so the
    form can't suggest a value the boundary would reject). Project/user id→name maps let
    the rules table show "in <Project>" / "assign <Name>" instead of bare ids."""
    project_rows = projects.list_projects(conn)  # admin: every project
    user_rows = users.list_users(conn)
    return {
        "rules": automation.list_rules(conn),
        "trigger_verbs": automation.TRIGGER_VERBS,
        "action_types": automation.ACTION_TYPES,
        "projects": project_rows,
        "users": user_rows,
        # The default workflow's statuses, offered as suggestions for set_status — a rule
        # may still target a project with custom statuses (validated at fire time).
        "status_suggestions": statuses.status_names(conn, None),
        "project_names": {p["id"]: p["name"] for p in project_rows},
        "user_names": {u["id"]: u["name"] for u in user_rows},
        "error": error,
    }


def _action_params_from_form(
    action_type: str, *, user_id: int | None, status: str, label: str, body: str
) -> dict:
    """Fold the per-action form fields down to the one action_params dict the chosen
    action_type needs. Unrelated fields are ignored, so switching the action select
    doesn't smuggle a stale value into the rule. An empty field yields {}, which
    validate_rule then rejects with the right 'requires …' message."""
    if action_type in ("assign", "add_contributor"):
        return {"user_id": user_id} if user_id is not None else {}
    if action_type == "set_status":
        return {"status": status.strip()} if status.strip() else {}
    if action_type == "add_label":
        return {"label": label.strip()} if label.strip() else {}
    if action_type == "comment":
        return {"body": body.strip()} if body.strip() else {}
    return {}


@router.get("/admin/automation", response_class=HTMLResponse)
def automation_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    return templates.TemplateResponse(
        request=request, name="admin/automation.html", context=_automation_context(conn)
    )


@router.post(
    "/admin/automation",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def create_rule(
    request: Request,
    name: str = Form(""),
    trigger_verb: str = Form(""),
    condition_project: str = Form(""),
    action_type: str = Form(""),
    action_user_id: str = Form(""),
    action_status: str = Form(""),
    action_label: str = Form(""),
    action_body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err

    name = name.strip()
    conditions: dict = {}
    project_id = _int_or_none(condition_project)
    if project_id is not None:
        conditions["project_id"] = project_id
    action_params = _action_params_from_form(
        action_type,
        user_id=_int_or_none(action_user_id),
        status=action_status,
        label=action_label,
        body=action_body,
    )

    def _reject(message: str):
        return templates.TemplateResponse(
            request=request,
            name="admin/automation.html",
            context=_automation_context(conn, error=message),
            status_code=400,
        )

    if not name:
        return _reject("Rule name is required.")
    # The SAME validator the REST API uses — the form can't persist a rule the API
    # wouldn't, so both surfaces reject a typo'd verb / missing param identically.
    spec_error = automation.validate_rule(
        trigger_verb=trigger_verb,
        action_type=action_type,
        conditions=conditions,
        action_params=action_params,
    )
    if spec_error is not None:
        return _reject(spec_error)

    automation.create_rule(
        conn,
        name=name,
        trigger_verb=trigger_verb,
        action_type=action_type,
        created_by=actor["id"],
        conditions=conditions,
        action_params=action_params,
    )
    return RedirectResponse("/admin/automation", status_code=303)


@router.post(
    "/admin/automation/{rule_id}/enabled",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def toggle_rule(
    request: Request,
    rule_id: int,
    enabled: str = Form("0"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    # The form posts the DESIRED next state ("1" enable, anything else pause) — a
    # deterministic toggle that keeps the rule (and its place in fire order).
    if automation.set_enabled(conn, rule_id, enabled == "1") is None:
        return HTMLResponse('<div class="error">No such rule.</div>', status_code=404)
    return RedirectResponse("/admin/automation", status_code=303)


@router.post(
    "/admin/automation/{rule_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def delete_rule(
    request: Request,
    rule_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    if not automation.delete_rule(conn, rule_id):
        return HTMLResponse('<div class="error">No such rule.</div>', status_code=404)
    return RedirectResponse("/admin/automation", status_code=303)
