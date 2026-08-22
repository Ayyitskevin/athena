"""Browser administration and settings routes."""

from __future__ import annotations

from html import escape
import json
import sqlite3

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from athena import config
from athena.aegis import (
    automation,
    automation_commands,
    delegations,
    fleet_work,
    issue_commands,
    issues,
    office,
    projects,
    sprints,
    statuses,
)
from athena.core import (
    activity,
    activity_chain,
    agent_commands,
    agents,
    answerability,
    approvals,
    budgets,
    identity,
    oidc,
    oidc_commands,
    run_control_commands,
    run_controls,
    run_replay,
    security_events,
    token_commands,
    tokens,
    user_commands,
    users,
    webhook_commands,
    webhooks,
    fleet_roster,
    worker_commands,
    workers,
)
from athena.core.deps import get_conn
from athena.mcp.config import claude_mcp_config
from athena.web import live
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates
from athena.workflows import fleet_assign_commands

router = APIRouter()

_ACTIVE_WORK_PRIVATE_HEADERS = {
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
    """Only ever redirect back into this app: a one-segment local path.

    The value rides in a hidden form field, so a tampered form must not turn
    a preference toggle into an open redirect. Anything that is not a plain
    local path ("//host" is protocol-relative, "https://…" is absolute)
    falls back to the home page.
    """
    if next_path.startswith("/") and not next_path.startswith("//"):
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
    scopes = _selected_scopes(
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
    err = _admin_required(user)
    if err is not None:
        return err
    return templates.TemplateResponse(
        request=request, name="admin/users.html", context=_admin_context(conn)
    )


def _agents_context(conn: sqlite3.Connection, viewer: dict, **extra) -> dict:
    """The agents-cockpit page context, shared by the GET view and the inline
    onboarding POST (which re-renders the page carrying the one-time token)."""
    agent_rows = agents.agent_admin_summaries(conn)
    for agent in agent_rows:
        agent["delegation_inbox"] = delegations.list_delegations(
            conn, agent["user"], viewer=viewer, limit=20
        )
        # The durable ceiling, rolled forward to now so an elapsed window reads as
        # fresh rather than spent. None means unbudgeted (unlimited) — metering is
        # opt-in, so most fleets start here.
        budget = budgets.observed(conn, agent["user"]["id"])
        agent["budget"] = None if budget is None else budget.public()
        agent["gated_actions"] = approvals.gated_kinds(conn, agent["user"]["id"])
        # Where this agent says it runs. Cooperative presence only: a worker that
        # stops heartbeating reads as stale, never as a process that died.
        agent["workers"] = workers.list_workers(
            conn, agent_id=agent["user"]["id"], limit=20
        )
    # Pending approvals are the steer-by-exception queue: actions an agent asked to
    # take that are waiting on this human. Names are resolved here so the template
    # stays a renderer.
    pending = []
    for request in approvals.list_requests(conn, state="pending", limit=50):
        requester = users.get_user(conn, request.requested_by)
        pending.append(
            {
                **request.public(),
                "requested_by_name": requester["name"] if requester else "unknown",
            }
        )
    # Workers told to stop that have not answered are the operator's live question,
    # so they are surfaced above the per-agent detail rather than buried in it.
    unanswered_kills = [
        worker
        for worker in workers.list_workers(conn, limit=200)
        if worker["kill_state"] in (workers.KILL_REQUESTED, workers.KILL_DEFIED)
    ]
    return {
        "agents": agent_rows,
        # The ask-and-answer ledger, one row per agent (zero-filled). Facts per
        # lane from core/answerability.py — deliberately never a score.
        "answerability": answerability.build_answerability(conn)["agents"],
        "unanswered_kills": unanswered_kills,
        "budget_windows": sorted(budgets.WINDOWS),
        "approval_kinds": sorted(approvals.ACTION_KINDS),
        "pending_approvals": pending,
        "notice": None,
        "error": None,
        **extra,
    }


@router.get("/admin/agents", response_class=HTMLResponse)
def agents_admin(
    request: Request,
    revoked: int | None = Query(None),
    offboarded: str | None = Query(None),
    paused: str | None = Query(None),
    resumed: str | None = Query(None),
    budget_set: str | None = Query(None),
    budget_cleared: str | None = Query(None),
    decided: str | None = Query(None),
    gate_changed: str | None = Query(None),
    notice: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    # Post/redirect/get carries the outcome back as a query param so a refresh
    # doesn't re-post the destructive action.
    if revoked is not None:
        notice = f"Revoked {revoked} live token{'' if revoked == 1 else 's'}."
    elif offboarded:
        notice = "Offboarded: demoted to viewer, sessions and tokens revoked."
    elif paused:
        notice = "Paused: every authenticated action is refused until resumed."
    elif resumed:
        notice = "Resumed: the account is active again."
    elif budget_set:
        notice = "Budget set: metered writes are now capped for this window."
    elif budget_cleared:
        notice = "Budget cleared: metered writes are unlimited again."
    elif decided == "approve":
        notice = "Approved: the requester may retry that action once."
    elif decided == "reject":
        notice = "Rejected: the requester is told to stop rather than wait."
    elif gate_changed:
        notice = "Approval policy updated."
    return templates.TemplateResponse(
        request=request,
        name="admin/agents.html",
        context=_agents_context(conn, user, notice=notice, error=error),
    )


def _fleet_page_context(
    conn: sqlite3.Connection,
    *,
    notice: str | None = None,
    error: str | None = None,
) -> dict:
    roster = fleet_roster.build_roster(conn)
    open_issues = [
        issue
        for issue in issues.list_issues(conn, include_archived=False, limit=80)
        if not statuses.is_done(conn, issue.get("project_id"), issue["status"])
    ]
    assignable = [
        seat
        for seat in roster["seats"]
        if seat["athena"] is not None and seat["athena"].get("is_agent")
    ]
    return {
        "roster": roster,
        "open_issues": open_issues,
        "assignable": assignable,
        "radio_configured": config.buzz_radio_configured(),
        "occupancy": office.build_occupancy(conn),
        "notice": notice,
        "error": error,
    }


@router.get("/admin/fleet", response_class=HTMLResponse)
def fleet_roster_page(
    request: Request,
    assigned: str | None = Query(None),
    seat: str | None = Query(None),
    radio: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Who we declared, plus assign-to-desk + optional Buzz radio."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    notice = None
    if assigned:
        notice = f"Assigned {assigned} to {seat or 'seat'}."
        if radio == "sent":
            notice += " Radio posted to command-deck."
        elif radio == "skipped":
            notice += " Radio skipped (not configured)."
        elif radio == "failed":
            notice += " Radio failed; the desk assignment still landed."
    return templates.TemplateResponse(
        request=request,
        name="admin/fleet.html",
        context=_fleet_page_context(conn, notice=notice, error=error),
    )


@router.post(
    "/admin/fleet/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def fleet_assign_issue(
    request: Request,
    issue_id: int = Form(...),
    seat_slug: str = Form(...),
    note: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None
    try:
        result = fleet_assign_commands.assign_issue_to_seat(
            conn,
            actor=user,
            issue_id=issue_id,
            seat_slug=seat_slug,
            note=note,
        )
    except fleet_assign_commands.AssignError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/fleet.html",
            context=_fleet_page_context(conn, error=exc.detail),
            status_code=400,
        )
    except issue_commands.IssueCommandError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/fleet.html",
            context=_fleet_page_context(conn, error=str(exc.detail)),
            status_code=400,
        )
    radio = (result.get("radio") or {}).get("status") or "skipped"
    return RedirectResponse(
        f"/admin/fleet?assigned={result['issue_key']}&seat={result['seat']}&radio={radio}",
        status_code=303,
    )


@router.get("/admin/fleet.json")
def fleet_roster_json(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    user = getattr(request.state, "user", None)
    if user is None:
        return JSONResponse({"detail": "sign in required"}, status_code=401)
    if user.get("role") != "admin":
        return JSONResponse({"detail": "admin only"}, status_code=403)
    return fleet_roster.build_roster(conn)


@router.post(
    "/admin/agents/onboard",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def onboard_agent(
    request: Request,
    email: str = Form(""),
    name: str = Form(""),
    token_name: str = Form(""),
    scope_read: str | None = Form(None),
    scope_issue_write: str | None = Form(None),
    scope_docs_write: str | None = Form(None),
    scope_admin: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """One-click provisioning from the cockpit: agent user + first scoped token
    in one audited command. Renders the result inline (no redirect) because the
    raw token and its MCP config block are shown exactly once — a secret must
    never ride a query string."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    # Validation (blank email/name, missing scopes) lives in the command — the
    # except branch below renders its message inline.
    try:
        result = agent_commands.onboard_agent(
            conn,
            actor=user,
            name=name,
            email=email or None,
            scopes=_selected_scopes(
                scope_read, scope_issue_write, scope_docs_write, scope_admin
            ),
            token_name=token_name.strip() or None,
        )
    except agent_commands.AgentCommandError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/agents.html",
            context=_agents_context(conn, user, error=str(exc)),
            status_code=400,
        )
    onboarded = {
        "user": result["user"],
        "token": result["token"],
        "config_json": json.dumps(
            claude_mcp_config(
                base_url=str(request.base_url).rstrip("/"),
                token=result["token"]["token"],
            ),
            indent=2,
        ),
    }
    return templates.TemplateResponse(
        request=request,
        name="admin/agents.html",
        context=_agents_context(conn, user, onboarded=onboarded),
        status_code=201,
    )


@router.post(
    "/admin/approvals/{request_id}/decision",
    dependencies=[Depends(verify_csrf)],
)
def decide_approval(
    request: Request,
    request_id: int,
    decision: str = Form(""),
    note: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Decide one pending approval from the cockpit — the same core owner REST and
    MCP call. Approving opens the gate for exactly one retry by the requester; it
    does not perform the action."""
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    try:
        approvals.decide(
            conn,
            actor_id=user["id"],
            request_id=request_id,
            decision=decision.strip(),
            note=note.strip() or None,
        )
    except approvals.ApprovalDecisionError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse(
        f"/admin/agents?decided={decision.strip()}", status_code=303
    )


@router.post(
    "/admin/workers/{worker_id}/kill",
    dependencies=[Depends(verify_csrf)],
)
def request_worker_kill(
    request: Request,
    worker_id: int,
    cancel: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Ask a worker to stop, or withdraw an unanswered request — the same core
    command owner REST and MCP call.

    The notice deliberately says "asked to stop", not "stopped": Athena records an
    instruction and cannot end a process. The worker learns of it on its next
    heartbeat, and the registry reports only what has actually been said."""
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    withdrawing = cancel.strip().lower() in ("1", "true", "on", "yes")
    try:
        if withdrawing:
            worker_commands.cancel_kill(conn, actor=user, worker_id=worker_id)
        else:
            worker_commands.request_kill(conn, actor=user, worker_id=worker_id)
    except worker_commands.WorkerCommandError as exc:
        return RedirectResponse(f"/admin/agents?error={exc.detail}", status_code=303)
    notice = (
        "Kill request withdrawn."
        if withdrawing
        else "Worker asked to stop; it will be told on its next heartbeat."
    )
    return RedirectResponse(f"/admin/agents?notice={notice}", status_code=303)


@router.post(
    "/admin/agents/{user_id}/approval-policy",
    dependencies=[Depends(verify_csrf)],
)
def set_approval_policy(
    request: Request,
    user_id: int,
    action_kind: str = Form(""),
    gate: str = Form("on"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Gate or ungate an action kind for one agent."""
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    try:
        if gate == "off":
            approvals.clear_policy(
                conn,
                actor_id=user["id"],
                target_user_id=user_id,
                action_kind=action_kind.strip(),
            )
        else:
            approvals.set_policy(
                conn,
                actor_id=user["id"],
                target_user_id=user_id,
                action_kind=action_kind.strip(),
            )
    except ValueError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse("/admin/agents?gate_changed=1", status_code=303)


@router.post(
    "/admin/agents/{user_id}/budget",
    dependencies=[Depends(verify_csrf)],
)
def set_agent_budget(
    request: Request,
    user_id: int,
    action_limit: str = Form(""),
    window: str = Form("day"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Set an agent's durable action ceiling from the cockpit — the same command
    the REST endpoint and MCP tool call, so the three cannot drift."""
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    raw = action_limit.strip()
    if not raw.isdigit():
        return RedirectResponse(
            "/admin/agents?error=Action+limit+must+be+a+whole+number.",
            status_code=303,
        )
    try:
        budgets.set_budget(
            conn,
            actor_id=user["id"],
            target_user_id=user_id,
            window=window,
            action_limit=int(raw),
        )
    except ValueError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse("/admin/agents?budget_set=1", status_code=303)


@router.post(
    "/admin/agents/{user_id}/budget/clear",
    dependencies=[Depends(verify_csrf)],
)
def clear_agent_budget(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Return an agent to unlimited metered writes. Idempotent."""
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    budgets.clear_budget(conn, actor_id=user["id"], target_user_id=user_id)
    return RedirectResponse("/admin/agents?budget_cleared=1", status_code=303)


@router.post(
    "/admin/agents/{user_id}/pause",
    dependencies=[Depends(verify_csrf)],
)
def pause_agent(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    try:
        agent_commands.set_user_paused(
            conn, actor=user, target_user_id=user_id, paused=True
        )
    except agent_commands.AgentCommandError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse("/admin/agents?paused=1", status_code=303)


@router.post(
    "/admin/agents/{user_id}/resume",
    dependencies=[Depends(verify_csrf)],
)
def resume_agent(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    try:
        agent_commands.set_user_paused(
            conn, actor=user, target_user_id=user_id, paused=False
        )
    except agent_commands.AgentCommandError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse("/admin/agents?resumed=1", status_code=303)


@router.post(
    "/admin/agents/{user_id}/revoke-tokens",
    dependencies=[Depends(verify_csrf)],
)
def revoke_agent_tokens(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    try:
        result = agent_commands.revoke_agent_tokens(
            conn, actor=user, target_user_id=user_id
        )
    except agent_commands.AgentCommandError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse(
        f"/admin/agents?revoked={result['revoked_token_count']}", status_code=303
    )


@router.post(
    "/admin/agents/{user_id}/offboard",
    dependencies=[Depends(verify_csrf)],
)
def offboard_agent(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    try:
        agent_commands.offboard_user(conn, actor=user, target_user_id=user_id)
    except agent_commands.AgentCommandError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse("/admin/agents?offboarded=1", status_code=303)


@router.get("/admin/agents/runs", response_class=HTMLResponse)
def agent_runs_admin(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        err.headers.update(_ACTIVE_WORK_PRIVATE_HEADERS)
        return err
    try:
        # `panel` is this page's own refresh marker, not an active-work criterion.
        # fleet_work.parse_query_pairs is strict by design — an unknown key is a 400
        # rather than a silently widened request — so the marker is removed before
        # the criteria are parsed. Everything else still faces the strict parse.
        query_pairs = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key != live.PANEL_PARAM
        ]
        agent_values = [value for key, value in query_pairs if key == "agent_id"]
        # The no-JS "All agents" option submits one empty select value. Treat that
        # exact form shape as absence; repeated keys and empty REST criteria remain
        # strict errors rather than silently widening a request.
        if agent_values == [""]:
            query_pairs = [
                (key, value) for key, value in query_pairs if key != "agent_id"
            ]
        agent_id, work_limit, attention_state = fleet_work.parse_query_pairs(
            query_pairs
        )
    except fleet_work.ActiveWorkQueryError as exc:
        return HTMLResponse(
            f"<h1>Invalid active-work request</h1><p>{escape(exc.detail)}</p>",
            status_code=400,
            headers=_ACTIVE_WORK_PRIVATE_HEADERS,
        )
    context = agents.agent_run_health(conn, agent_id=agent_id)
    context["active_work"] = fleet_work.build_active_work(
        conn, agent_id=agent_id, limit=work_limit, attention_state=attention_state
    )
    context["attention_states"] = list(fleet_work.ATTENTION_STATES)
    context["automation_failures"] = automation.list_rules(conn, failing_only=True)
    context["live"] = live.build(request, live.ACTIVE_WORK)
    # The table refreshes itself, and its poll re-enters this route — past the same
    # _admin_required above, through the same parse of the same query string. The
    # filter an operator chose therefore survives a refresh instead of silently
    # widening to the whole fleet, and there is no partial-only path that could
    # forget a gate this one applies.
    if live.wants_panel(request, live.ACTIVE_WORK):
        return templates.TemplateResponse(
            request=request,
            name="admin/partials/fleet_active_work.html",
            context=context,
            headers=_ACTIVE_WORK_PRIVATE_HEADERS,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/agent_runs.html",
        context=context,
        headers=_ACTIVE_WORK_PRIVATE_HEADERS,
    )


@router.get("/admin/agents/runs/{run_id}/replay.json")
def agent_run_replay_admin(
    request: Request, run_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    if not agents.agent_run_exists(conn, run_id):
        return JSONResponse({"detail": "no such agent run"}, status_code=404)
    try:
        artifact = run_replay.build_run_replay_artifact(conn, run_id)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    if artifact is None:
        return JSONResponse({"detail": "no such agent run"}, status_code=404)
    return JSONResponse(artifact)


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
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
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
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
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
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
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


@router.get("/admin/security", response_class=HTMLResponse)
def security_signals(
    request: Request,
    verb: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Boundary refusals — the probing an operator should see before a compromise.

    These events have always been on the trail; what was missing was a place to
    read them without already knowing the four verb names. Admin-only, because a
    list of who has been probing is operator intelligence."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    selected = verb if verb in security_events.SECURITY_VERBS else None
    return templates.TemplateResponse(
        request=request,
        name="admin/security.html",
        context={
            "events": security_events.list_failures(conn, verb=selected, limit=100),
            "counts": security_events.failure_counts(conn),
            "verbs": list(security_events.SECURITY_VERBS),
            "selected_verb": selected,
            # Trail integrity (0072): where the hash chain stands, plus a cheap
            # tail recheck for THIS render. The card says exactly what the tail
            # check covers; the full walk belongs to athena-doctor / the API.
            "chain": activity_chain.status(conn),
            "chain_tail": activity_chain.verify_tail(conn),
        },
        headers=_ACTIVE_WORK_PRIVATE_HEADERS,
    )


_CONTROL_STATE_FILTERS = (
    run_controls.STATE_FILTER_OPEN,
    *run_controls.CONTROL_STATES,
    "all",
)


@router.get("/admin/run-controls", response_class=HTMLResponse)
def run_controls_admin(
    request: Request,
    state: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Every recorded control request in one place — the page the
    fleet-attention rollup's "Run controls awaiting an agent" count links to.

    Until now controls lived only on each run's lineage page, so an operator
    had to already know which runs they had steered. Admin-only, like the rest
    of the fleet cockpit; the bound agent's own inbox stays the API/MCP list.
    This page renders the same command read those serve — it owns no data."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    # A hand-edited filter falls back to the default rather than erroring —
    # the security page's lenient stance for GET filters.
    selected = state if state in _CONTROL_STATE_FILTERS else "open"
    controls = run_control_commands.readable_controls(
        conn,
        actor=user,
        state=None if selected == "all" else selected,
        limit=run_controls.MAX_LIST_LIMIT,
    )
    # A heartbeat-only run is steerable (the check-in fallback admits it) but
    # has NO lineage page: that page is built from activity events, and a run
    # whose agent only checked in has none. Linking there anyway would hand the
    # operator a 404 from their own cockpit, so each row learns whether its run
    # has a page before the template decides to link it.
    linkable = {
        control["run_id"]
        for control in controls
        if activity.run_lineage(conn, control["run_id"], actor=user) is not None
    }
    context = {
        "controls": controls,
        "linkable_runs": linkable,
        "states": list(_CONTROL_STATE_FILTERS),
        "selected_state": selected,
        "clipped": len(controls) == run_controls.MAX_LIST_LIMIT,
        "limit": run_controls.MAX_LIST_LIMIT,
        "live": live.build(request, live.RUN_CONTROLS),
    }
    # Refreshes itself through this same route, so the state filter survives and the
    # admin gate cannot be bypassed by asking for the panel directly.
    name = (
        "admin/partials/run_controls_list.html"
        if live.wants_panel(request, live.RUN_CONTROLS)
        else "admin/run_controls.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        headers=_ACTIVE_WORK_PRIVATE_HEADERS,
    )


@router.get("/admin/webhooks", response_class=HTMLResponse)
def webhooks_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
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
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
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
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
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
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
    if not webhook_commands.delete_webhook(
        conn, actor_id=actor["id"], webhook_id=webhook_id
    ):
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


def _optional_form_int(raw: str, *, field: str) -> tuple[int | None, str | None]:
    """Parse optional operator-entered integer text without treating errors as unset.

    Project/user ids come from closed selects, where ``_int_or_none`` is appropriate.
    Schedule values are free text: silently treating ``"hourly"`` as absent could turn
    a requested recurring rule into a one-shot rule, so malformed text fails closed.
    """
    value = (raw or "").strip()
    if not value:
        return None, None
    try:
        return int(value), None
    except ValueError:
        return None, f"{field} must be an integer"


def _automation_context(conn: sqlite3.Connection, *, error: str | None = None) -> dict:
    """Everything the rule-builder page renders: the existing rules plus the option
    sets the create form offers (the SAME closed sets the validator enforces, so the
    form can't suggest a value the boundary would reject). Project/user id→name maps let
    the rules table show "in <Project>" / "assign <Name>" instead of bare ids."""
    project_rows = projects.list_projects(conn)  # admin: every project
    sprint_rows = sprints.list_sprints(conn)
    user_rows = users.list_users(conn)
    return {
        "rules": automation.list_rules(conn),
        "trigger_verbs": automation.TRIGGER_VERBS,
        "action_types": automation.ACTION_TYPES,
        "projects": project_rows,
        "sprints": sprint_rows,
        "users": user_rows,
        # The default workflow's statuses, offered as suggestions for set_status — a rule
        # may still target a project with custom statuses (validated at fire time).
        "status_suggestions": statuses.status_names(conn, None),
        "project_names": {p["id"]: p["name"] for p in project_rows},
        "sprint_names": {s["id"]: s["name"] for s in sprint_rows},
        "user_names": {u["id"]: u["name"] for u in user_rows},
        # Shown as the buzz_message channel placeholder so the operator can see
        # what "blank" means on this deployment.
        "default_assign_channel": config.buzz_assign_channel(),
        "error": error,
    }


def _action_params_from_form(
    action_type: str,
    *,
    user_id: int | None,
    status: str,
    label: str,
    body: str,
    buzz_channel: str = "",
    buzz_mention: str = "",
    buzz_note: str = "",
) -> dict:
    """Fold the per-action form fields down to the one action_params dict the chosen
    action_type needs. Unrelated fields are ignored, so switching the action select
    doesn't smuggle a stale value into the rule. An empty field yields {}, which
    validate_rule then rejects with the right 'requires …' message (buzz_message
    accepts {} — every param is optional there)."""
    if action_type in ("assign", "add_contributor"):
        return {"user_id": user_id} if user_id is not None else {}
    if action_type == "set_status":
        return {"status": status.strip()} if status.strip() else {}
    if action_type == "add_label":
        return {"label": label.strip()} if label.strip() else {}
    if action_type == "comment":
        return {"body": body.strip()} if body.strip() else {}
    if action_type == "buzz_message":
        params: dict = {}
        if buzz_channel.strip():
            params["channel"] = buzz_channel.strip()
        if buzz_mention.strip():
            params["mention"] = buzz_mention.strip()
        if buzz_note.strip():
            params["note"] = buzz_note.strip()
        return params
    return {}


@router.get("/admin/automation", response_class=HTMLResponse)
def automation_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
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
    trigger_type: str = Form("event"),
    trigger_verb: str = Form(""),
    schedule_at: str = Form(""),
    schedule_every_seconds: str = Form(""),
    condition_project: str = Form(""),
    condition_sprint: str = Form(""),
    condition_inactive_for_seconds: str = Form(""),
    action_type: str = Form(""),
    action_user_id: str = Form(""),
    action_status: str = Form(""),
    action_label: str = Form(""),
    action_body: str = Form(""),
    action_buzz_channel: str = Form(""),
    action_buzz_mention: str = Form(""),
    action_buzz_note: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"

    name = name.strip()
    trigger_type = trigger_type.strip()
    if trigger_type == "schedule":
        trigger_verb = automation.SCHEDULE_TRIGGER_VERB
        schedule_at_value = schedule_at.strip() or None
        schedule_every, schedule_every_error = _optional_form_int(
            schedule_every_seconds, field="schedule_every_seconds"
        )
        inactive_for, inactive_for_error = _optional_form_int(
            condition_inactive_for_seconds, field="inactive_for_seconds"
        )
    else:
        # Ignore fields from the unselected trigger type, just as action-specific
        # fields below are ignored. Stale schedule text cannot alter an event rule.
        schedule_at_value = None
        schedule_every = None
        schedule_every_error = None
        inactive_for = None
        inactive_for_error = None

    conditions: dict = {}
    project_id = _int_or_none(condition_project)
    if project_id is not None:
        conditions["project_id"] = project_id
    sprint_id = _int_or_none(condition_sprint)
    if sprint_id is not None:
        conditions["sprint_id"] = sprint_id
    if inactive_for is not None:
        conditions["inactive_for_seconds"] = inactive_for
    action_params = _action_params_from_form(
        action_type,
        user_id=_int_or_none(action_user_id),
        status=action_status,
        label=action_label,
        body=action_body,
        buzz_channel=action_buzz_channel,
        buzz_mention=action_buzz_mention,
        buzz_note=action_buzz_note,
    )

    def _reject(message: str):
        return templates.TemplateResponse(
            request=request,
            name="admin/automation.html",
            context=_automation_context(conn, error=message),
            status_code=400,
        )

    if schedule_every_error is not None:
        return _reject(schedule_every_error)
    if inactive_for_error is not None:
        return _reject(inactive_for_error)
    try:
        automation_commands.create_rule(
            conn,
            actor_id=actor["id"],
            name=name,
            trigger_verb=trigger_verb,
            action_type=action_type,
            conditions=conditions,
            action_params=action_params,
            trigger_type=trigger_type,
            schedule_at=schedule_at_value,
            schedule_every_seconds=schedule_every,
        )
    except automation_commands.AutomationCommandError as exc:
        return _reject(str(exc))
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
    assert actor is not None, "_admin_required accepted a missing user"
    # The form posts the DESIRED next state ("1" enable, anything else pause) — a
    # deterministic toggle that keeps the rule (and its place in fire order). The
    # command records the flip atomically.
    try:
        automation_commands.set_rule_enabled(
            conn, actor_id=actor["id"], rule_id=rule_id, enabled=enabled == "1"
        )
    except automation_commands.AutomationCommandError:
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
    assert actor is not None, "_admin_required accepted a missing user"
    if not automation_commands.delete_rule(conn, actor_id=actor["id"], rule_id=rule_id):
        return HTMLResponse('<div class="error">No such rule.</div>', status_code=404)
    return RedirectResponse("/admin/automation", status_code=303)
