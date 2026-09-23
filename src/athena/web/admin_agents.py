"""Browser routes for the agent cockpit: roster, fleet assignment, pause,
budgets, approvals, and run replay.

Split out of web/admin.py. Writes go through the same command owners the
REST adapters use."""

from __future__ import annotations
from html import escape
import json
import sqlite3
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from athena import config
from athena.aegis import (
    automation,
    delegations,
    fleet_work,
    issue_commands,
    issues,
    office,
    statuses,
)
from athena.core import (
    agent_commands,
    agents,
    answerability,
    approval_commands,
    approvals,
    approvals_api,
    budget_commands,
    budgets,
    run_replay,
    users,
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

from athena.web.admin import (
    ACTIVE_WORK_PRIVATE_HEADERS,
    admin_required,
    selected_scopes,
)

router = APIRouter()


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
    removed: str | None = Query(None),
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
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    # Post/redirect/get carries the outcome back as a query param so a refresh
    # doesn't re-post the destructive action.
    if revoked is not None:
        notice = f"Revoked {revoked} live token{'' if revoked == 1 else 's'}."
    elif offboarded:
        notice = "Offboarded: demoted to viewer, sessions and tokens revoked."
    elif removed:
        notice = (
            "Removed: offboarded and hidden from every list; history stays "
            "attributed. Restore via POST /users/{id}/restore."
        )
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
    err = admin_required(user)
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
    err = admin_required(user)
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
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    # Validation (blank email/name, missing scopes) lives in the command — the
    # except branch below renders its message inline.
    try:
        result = agent_commands.onboard_agent(
            conn,
            actor=user,
            name=name,
            email=email or None,
            scopes=selected_scopes(
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
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    try:
        approvals_api.decide_for_actor(
            conn,
            actor=user,
            request_id=request_id,
            decision=decision.strip(),
            note=note.strip() or None,
        )
    except HTTPException as exc:
        return RedirectResponse(f"/admin/agents?error={exc.detail}", status_code=303)
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
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
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
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    try:
        if gate == "off":
            approval_commands.clear_policy(
                conn,
                actor_id=user["id"],
                target_user_id=user_id,
                action_kind=action_kind.strip(),
            )
        else:
            approval_commands.set_policy(
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
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    raw = action_limit.strip()
    if not raw.isdigit():
        return RedirectResponse(
            "/admin/agents?error=Action+limit+must+be+a+whole+number.",
            status_code=303,
        )
    try:
        budget_commands.set_budget(
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
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    budget_commands.clear_budget(conn, actor_id=user["id"], target_user_id=user_id)
    return RedirectResponse("/admin/agents?budget_cleared=1", status_code=303)


@router.post(
    "/admin/agents/{user_id}/pause",
    dependencies=[Depends(verify_csrf)],
)
def pause_agent(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = admin_required(user)
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
    err = admin_required(user)
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
    err = admin_required(user)
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
    err = admin_required(user)
    if err is not None:
        return err
    try:
        agent_commands.offboard_user(conn, actor=user, target_user_id=user_id)
    except agent_commands.AgentCommandError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse("/admin/agents?offboarded=1", status_code=303)


@router.post(
    "/admin/agents/{user_id}/remove",
    dependencies=[Depends(verify_csrf)],
)
def remove_agent(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    try:
        agent_commands.remove_user(conn, actor=user, target_user_id=user_id)
    except agent_commands.AgentCommandError as exc:
        return RedirectResponse(f"/admin/agents?error={exc}", status_code=303)
    return RedirectResponse("/admin/agents?removed=1", status_code=303)


@router.get("/admin/agents/runs", response_class=HTMLResponse)
def agent_runs_admin(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        err.headers.update(ACTIVE_WORK_PRIVATE_HEADERS)
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
            headers=ACTIVE_WORK_PRIVATE_HEADERS,
        )
    context = agents.agent_run_health(conn, agent_id=agent_id)
    context["active_work"] = fleet_work.build_active_work(
        conn, agent_id=agent_id, limit=work_limit, attention_state=attention_state
    )
    context["attention_states"] = list(fleet_work.ATTENTION_STATES)
    context["automation_failures"] = automation.list_rules(conn, failing_only=True)
    context["live"] = live.build(request, live.ACTIVE_WORK)
    # The table refreshes itself, and its poll re-enters this route — past the same
    # admin_required above, through the same parse of the same query string. The
    # filter an operator chose therefore survives a refresh instead of silently
    # widening to the whole fleet, and there is no partial-only path that could
    # forget a gate this one applies.
    if live.wants_panel(request, live.ACTIVE_WORK):
        return templates.TemplateResponse(
            request=request,
            name="admin/partials/fleet_active_work.html",
            context=context,
            headers=ACTIVE_WORK_PRIVATE_HEADERS,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/agent_runs.html",
        context=context,
        headers=ACTIVE_WORK_PRIVATE_HEADERS,
    )


@router.get("/admin/agents/runs/{run_id}/replay.json")
def agent_run_replay_admin(
    request: Request, run_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    err = admin_required(user)
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
