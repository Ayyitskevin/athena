"""Browser routes for the agent cockpit: roster, fleet assignment, pause,
budgets, approvals, and run replay.

Split out of web/admin.py. Writes go through the same command owners the
REST adapters use."""

from __future__ import annotations
from datetime import UTC, datetime
from html import escape
import json
import re
import sqlite3
from urllib.parse import quote
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from athena import config
from athena.aegis import (
    approval_rows,
    automation,
    delegations,
    fleet_work,
    issue_commands,
    issues,
    office,
    reported_spend,
    run_state,
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
    db,
    run_replay,
    users,
    fleet_roster,
    fleet_seat_commands,
    worker_commands,
    workers,
)
from athena.core.deps import get_conn
from athena.core.ids import MAX_SQLITE_INTEGER
from athena.mcp.config import (
    athena_mcp_command,
    PRINTABLE_PROFILES,
    claude_mcp_config,
)
from athena.web import live
from athena.web.csrf import verify_csrf
from athena.web.form_errors import BrowserRefusal
from athena.web.router import get_templates
from athena.workflows import fleet_assign_commands

from athena.web.admin import (
    ACTIVE_WORK_PRIVATE_HEADERS,
    admin_required,
    selected_scopes,
)

router = APIRouter()


_REPORTED_ROWS = 5


def _reported_spend(
    conn: sqlite3.Connection, viewer: dict, agent_id: int, reads: reported_spend.Scan
) -> dict | None:
    """What this agent SAID it used, as the viewer may see it; None when it said nothing."""
    days = reported_spend.by_agent_day(
        conn, viewer=viewer, agent_id=agent_id, reads=reads
    )
    if not days:
        return None
    return {
        "label": reported_spend.LABEL,
        "clipped": reads.clipped,
        "days": days[:7],
        "runs": reported_spend.by_run(
            conn, viewer=viewer, agent_id=agent_id, reads=reads
        )[:_REPORTED_ROWS],
        "issues": reported_spend.by_issue(
            conn, viewer=viewer, agent_id=agent_id, reads=reads
        )[:_REPORTED_ROWS],
    }


def _agents_redirect(
    *, agent_id: int | None = None, anchor: bool = True, **query: str
) -> RedirectResponse:
    """Send the operator back to the agents page with one encoded value per key.

    Error and notice text can contain & # ? = (it may echo input), so it must be
    encoded rather than pasted into the URL.

    ``agent_id`` is the agent the action was made on (U10): the page opens that
    agent's section and says the outcome there, and the fragment scrolls to it.
    ``anchor`` is off when the agent has no section to return to (it was just
    removed); the outcome still names it."""
    if agent_id is not None:
        query["agent"] = str(agent_id)
    query_string = "&".join(
        f"{key}={quote(value, safe='')}" for key, value in query.items()
    )
    fragment = f"#agent-{agent_id}" if agent_id is not None and anchor else ""
    return RedirectResponse(f"/admin/agents?{query_string}{fragment}", status_code=303)


# What each outcome says: its lead, the word that joins the agent's name to it,
# and the rest. With the agent it reads "Paused Sol: every ..."; without one
# (an agent this page no longer has, or a link somebody shortened) the same
# words with no name.
_OUTCOMES: dict[str, tuple[str, str, str | None]] = {
    "offboarded": (
        "Offboarded",
        " ",
        "demoted to viewer, sessions and tokens revoked.",
    ),
    "removed": (
        "Removed",
        " ",
        "offboarded and hidden from every list; history stays attributed. "
        "Restore it under Removed agents, below.",
    ),
    "restored": (
        "Restored",
        " ",
        "back as a read-only viewer with no token. Give it a role under Manage "
        "users, then mint it a token here.",
    ),
    "paused": ("Paused", " ", "every authenticated action is refused until resumed."),
    "resumed": ("Resumed", " ", "the account is active again."),
    "budget_set": (
        "Budget set",
        " for ",
        "metered writes are now capped for this window.",
    ),
    "budget_cleared": (
        "Budget cleared",
        " for ",
        "metered writes are unlimited again.",
    ),
    "gate_changed": ("Approval policy updated", " for ", None),
}


def _outcome(lead: str, joiner: str, rest: str | None, name: str | None) -> str:
    said = lead if name is None else f"{lead}{joiner}{name}"
    return f"{said}." if rest is None else f"{said}: {rest}"


def _agents_context(conn: sqlite3.Connection, viewer: dict, **extra) -> dict:
    """The agents-cockpit page context, shared by the GET view and the inline
    onboarding POST (which re-renders the page carrying the one-time token)."""
    with db.transaction(conn):
        return _build_agents_context(conn, viewer, now=datetime.now(UTC), **extra)


def _build_agents_context(
    conn: sqlite3.Connection, viewer: dict, *, now: datetime, **extra
) -> dict:
    agent_rows = agents.agent_admin_summaries(conn)
    reads = reported_spend.scan(conn, viewer)  # one read for every agent on the page
    for agent in agent_rows:
        agent["delegation_inbox"] = delegations.list_delegations(
            conn, agent["user"], viewer=viewer, limit=20
        )
        # The durable ceiling, rolled forward to now so an elapsed window reads as
        # fresh rather than spent. None means unbudgeted (unlimited) — metering is
        # opt-in, so most fleets start here.
        budget = budgets.observed(conn, agent["user"]["id"])
        agent["budget"] = None if budget is None else budget.public()
        agent["reported_spend"] = _reported_spend(
            conn, viewer, agent["user"]["id"], reads
        )
        agent["gated_actions"] = approvals.gated_kinds(conn, agent["user"]["id"])
        # Where this agent says it runs. Cooperative presence only: a worker that
        # stops heartbeating reads as stale, never as a process that died.
        agent["workers"] = workers.list_workers(
            conn, agent_id=agent["user"]["id"], limit=20, now=now
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
    # What each row asks about (U9): the issue's key and title and, for a close,
    # the verdict the requester reported — read as far as this viewer may read.
    described = approval_rows.describe(conn, viewer=viewer, requests=pending)
    for row in pending:
        row.update(described[row["id"]])
    # Workers told to stop that have not answered are the operator's live question,
    # so they are surfaced above the per-agent detail rather than buried in it.
    unanswered_kills, stop_total = workers.unanswered_page(conn, limit=200, now=now)
    return {
        "agents": agent_rows,
        # The ask-and-answer ledger, one row per agent (zero-filled). Facts per
        # lane from core/answerability.py — deliberately never a score.
        "answerability": answerability.build_answerability(conn, now=now)["agents"],
        "unanswered_kills": unanswered_kills,
        "unanswered_kills_total": stop_total,
        # Removed agents are hidden from every other list; this page removed
        # them, so this page is where one is restored (U5).
        "removed_agents": [
            account
            for account in users.list_users(conn, include_removed=True)
            if account["removed_at"] and account["is_agent"]
        ],
        "budget_windows": sorted(budgets.WINDOWS),
        "approval_kinds": sorted(approvals.ACTION_KINDS),
        # The MCP profiles the onboarding form may print (F9); empty = default.
        "printable_profiles": PRINTABLE_PROFILES,
        "pending_approvals": pending,
        # Beside the approvals: blocking questions from yielded claims nobody has
        # resumed. The page is admin-only; the reader still applies visibility.
        # Each marked answered or not by the ready lane's rule (U7).
        "open_questions": run_state.open_questions(
            conn, actor=viewer, with_answers=True
        ),
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
    restored: str | None = Query(None),
    paused: str | None = Query(None),
    resumed: str | None = Query(None),
    budget_set: str | None = Query(None),
    budget_cleared: str | None = Query(None),
    decided: str | None = Query(None),
    gate_changed: str | None = Query(None),
    notice: str | None = Query(None),
    error: str | None = Query(None),
    agent: int | None = Query(None, ge=1, le=MAX_SQLITE_INTEGER),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    # The agent the action was made on (U10). Its name is read, never taken
    # from the URL; a removed agent is still named, as its outcome is about it.
    acted_on = None if agent is None else users.get_user(conn, agent)
    name = None if acted_on is None else acted_on["name"]
    # Post/redirect/get carries the outcome back as a query param so a refresh
    # doesn't re-post the destructive action.
    flags = {
        "offboarded": offboarded,
        "removed": removed,
        "restored": restored,
        "paused": paused,
        "resumed": resumed,
        "budget_set": budget_set,
        "budget_cleared": budget_cleared,
    }
    outcome = next((key for key, value in flags.items() if value), None)
    if revoked is not None:
        tokens_revoked = f"Revoked {revoked} live token{'' if revoked == 1 else 's'}"
        notice = _outcome(tokens_revoked, " of ", None, name)
    elif outcome is not None:
        notice = _outcome(*_OUTCOMES[outcome], name)
    elif decided == "approve":
        notice = "Approved: the requester may retry that action once."
    elif decided == "reject":
        notice = "Rejected: the requester is told to stop rather than wait."
    elif gate_changed:
        notice = _outcome(*_OUTCOMES["gate_changed"], name)
    context = _agents_context(conn, user, notice=notice, error=error)
    # The outcome is said in the section it was made in, which opens; an agent
    # the page does not list has none, and the outcome is said at the top.
    listed = {row["user"]["id"] for row in context["agents"]}
    context["acted_on_agent_id"] = agent if agent in listed else None
    return templates.TemplateResponse(
        request=request, name="admin/agents.html", context=context
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
        # A starting slug for the declare form on each seatless agent's row:
        # the address's local part, cut to what a slug may hold. The admin may
        # change it; the command validates whatever is sent.
        "suggested_slugs": {
            agent["id"]: _suggested_slug(agent["email"])
            for agent in roster["undeclared_athena_agents"]
        },
        "seat_kinds": fleet_roster.SEAT_KINDS,
        "unit_scopes": fleet_roster.UNIT_SCOPES,
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
    declared: str | None = Query(None),
    edited: str | None = Query(None),
    removed: str | None = Query(None),
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
    if declared:
        notice = f"Declared seat {declared}."
    elif edited:
        notice = f"Saved seat {edited}."
    elif removed:
        notice = (
            f"Removed seat {removed}. Its account, if it had one, is kept and "
            "now has no seat."
        )
    elif assigned:
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


#: How the cockpit answers a seat command's refusal kind.
_SEAT_STATUS_BY_KIND: dict[str, int] = {
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "invalid": 400,
}


def _suggested_slug(email: str | None) -> str:
    local = (email or "").split("@", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", local).strip("-")
    return slug[:40].rstrip("-")


def _seat_form_fields(
    name: str, email: str, kind: str, unit: str, unit_scope: str, buzz_pubkey: str
) -> dict[str, str]:
    return {
        "name": name,
        "email": email,
        "kind": kind,
        "unit": unit,
        "unit_scope": unit_scope,
        "buzz_pubkey": buzz_pubkey,
    }


def _seat_refused(
    request: Request,
    conn: sqlite3.Connection,
    exc: fleet_seat_commands.FleetSeatCommandError,
    *,
    typed: dict[str, str] | None = None,
) -> HTMLResponse:
    """The fleet page with the refusal at the top and, for a declare or an edit,
    the form that posted it holding what was typed (U11)."""
    context = _fleet_page_context(conn, error=exc.detail)
    context["typed_seat"] = typed
    return get_templates().TemplateResponse(
        request=request,
        name="admin/fleet.html",
        context=context,
        status_code=_SEAT_STATUS_BY_KIND[exc.kind],
    )


@router.post(
    "/admin/fleet/seats",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def declare_fleet_seat(
    request: Request,
    slug: str = Form(""),
    name: str = Form(""),
    email: str = Form(""),
    kind: str = Form("seat"),
    unit: str = Form(""),
    unit_scope: str = Form("user"),
    buzz_pubkey: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Declare a seat from the cockpit: the command REST's POST /fleet/seats
    calls. Empty optional fields mean none."""
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    fields = _seat_form_fields(name, email, kind, unit, unit_scope, buzz_pubkey)
    try:
        seat = fleet_seat_commands.declare_seat(conn, actor=user, slug=slug, **fields)
    except fleet_seat_commands.FleetSeatCommandError as exc:
        return _seat_refused(
            request, conn, exc, typed={"form": "declare", "slug": slug, **fields}
        )
    return RedirectResponse(
        f"/admin/fleet?declared={quote(seat['slug'], safe='')}", status_code=303
    )


@router.post(
    "/admin/fleet/seats/{slug}",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def edit_fleet_seat(
    request: Request,
    slug: str,
    name: str = Form(""),
    email: str = Form(""),
    kind: str = Form("seat"),
    unit: str = Form(""),
    unit_scope: str = Form("user"),
    buzz_pubkey: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save a seat's edit form. The form sends every field, so an empty one
    clears it; the slug is the seat's key and is not edited."""
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    fields = _seat_form_fields(name, email, kind, unit, unit_scope, buzz_pubkey)
    try:
        seat = fleet_seat_commands.update_seat(
            conn, actor=user, slug=slug, changes=fields
        )
    except fleet_seat_commands.FleetSeatCommandError as exc:
        return _seat_refused(
            request, conn, exc, typed={"form": "edit", "slug": slug, **fields}
        )
    return RedirectResponse(
        f"/admin/fleet?edited={quote(seat['slug'], safe='')}", status_code=303
    )


@router.post(
    "/admin/fleet/seats/{slug}/remove",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def remove_fleet_seat(
    request: Request,
    slug: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Remove a seat. The account it named is kept."""
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    try:
        removed = fleet_seat_commands.remove_seat(conn, actor=user, slug=slug)
    except fleet_seat_commands.FleetSeatCommandError as exc:
        return _seat_refused(request, conn, exc)
    return RedirectResponse(
        f"/admin/fleet?removed={quote(removed['seat']['slug'], safe='')}",
        status_code=303,
    )


@router.get("/admin/fleet.json")
def fleet_roster_json(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    user = getattr(request.state, "user", None)
    if user is None:
        return JSONResponse(
            {"detail": "sign in required", "code": "unauthenticated"}, status_code=401
        )
    if user.get("role") != "admin":
        return JSONResponse(
            {"detail": "admin only", "code": "forbidden"}, status_code=403
        )
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
    profile: str = Form(""),
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
            # Empty means the default; any other value is checked by the
            # command, exactly as the REST body and the MCP tool are.
            profile=profile.strip() or None,
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
                profile=result["profile"],
                command=athena_mcp_command(),
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
        return _agents_redirect(error=str(exc.detail))
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
            worker = worker_commands.cancel_kill(conn, actor=user, worker_id=worker_id)
        else:
            worker = worker_commands.request_kill(conn, actor=user, worker_id=worker_id)
    except worker_commands.WorkerCommandError as exc:
        return _agents_redirect(error=str(exc.detail))
    # Which worker, and whose: the page lists several, under several agents.
    owner = users.get_user(conn, worker["agent_id"])
    which = worker["node_label"] or worker["worker_key"]
    if owner is not None:
        which = f"{which} ({owner['name']})"
    notice = (
        f"Kill request for {which} withdrawn."
        if withdrawing
        else f"{which} asked to stop; it will be told on its next heartbeat."
    )
    return _agents_redirect(agent_id=worker["agent_id"], notice=notice)


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
    except approval_commands.ApprovalDecisionError as exc:
        return _agents_redirect(agent_id=user_id, error=exc.detail)
    except ValueError as exc:
        return _agents_redirect(agent_id=user_id, error=str(exc))
    return _agents_redirect(agent_id=user_id, gate_changed="1")


@router.post(
    "/admin/agents/{user_id}/budget",
    dependencies=[Depends(verify_csrf)],
)
def set_agent_budget(
    request: Request,
    user_id: int,
    action_limit: str = Form(""),
    window: str = Form("day"),
    coordination_limit: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Set an agent's durable action ceiling from the cockpit — the same command
    the REST endpoint and MCP tool call, so the three cannot drift. The
    coordination ceiling is optional: left empty, coordination is not limited."""
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"
    raw = action_limit.strip()
    if not raw.isdigit():
        return _agents_redirect(
            agent_id=user_id, error="Action limit must be a whole number."
        )
    coordination_raw = coordination_limit.strip()
    if coordination_raw and not coordination_raw.isdigit():
        return _agents_redirect(
            agent_id=user_id,
            error="Coordination limit must be a whole number or empty.",
        )
    try:
        budget_commands.set_budget(
            conn,
            actor_id=user["id"],
            target_user_id=user_id,
            window=window,
            action_limit=int(raw),
            coordination_limit=int(coordination_raw) if coordination_raw else None,
        )
    except budget_commands.BudgetCommandError as exc:
        return _agents_redirect(agent_id=user_id, error=exc.detail)
    except ValueError as exc:
        return _agents_redirect(agent_id=user_id, error=str(exc))
    return _agents_redirect(agent_id=user_id, budget_set="1")


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
    try:
        budget_commands.clear_budget(conn, actor_id=user["id"], target_user_id=user_id)
    except budget_commands.BudgetCommandError as exc:
        return _agents_redirect(agent_id=user_id, error=exc.detail)
    return _agents_redirect(agent_id=user_id, budget_cleared="1")


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
        return _agents_redirect(agent_id=user_id, error=str(exc))
    return _agents_redirect(agent_id=user_id, paused="1")


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
        return _agents_redirect(agent_id=user_id, error=str(exc))
    return _agents_redirect(agent_id=user_id, resumed="1")


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
        return _agents_redirect(agent_id=user_id, error=str(exc))
    return _agents_redirect(
        agent_id=user_id, revoked=str(result["revoked_token_count"])
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
        return _agents_redirect(agent_id=user_id, error=str(exc))
    return _agents_redirect(agent_id=user_id, offboarded="1")


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
        return _agents_redirect(agent_id=user_id, error=str(exc))
    # A removed agent has no section left to return to; the outcome names it.
    return _agents_redirect(agent_id=user_id, anchor=False, removed="1")


@router.post(
    "/admin/agents/{user_id}/restore",
    dependencies=[Depends(verify_csrf)],
)
def restore_agent(
    request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Bring a removed agent back: the command REST's POST /users/{id}/restore
    calls. It returns as a read-only viewer with no token, and its section says
    so; a role and a token are each their own act."""
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    try:
        agent_commands.restore_user(conn, actor=user, target_user_id=user_id)
    except agent_commands.AgentCommandError as exc:
        return _agents_redirect(error=str(exc))
    return _agents_redirect(agent_id=user_id, restored="1")


@router.post(
    "/admin/agents/{user_id}/tokens",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def mint_agent_token(
    request: Request,
    user_id: int,
    token_name: str = Form(""),
    scope_read: str | None = Form(None),
    scope_issue_write: str | None = Form(None),
    scope_docs_write: str | None = Form(None),
    scope_admin: str | None = Form(None),
    profile: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Mint a token for an existing agent (U5): the command REST's
    POST /users/{id}/tokens calls. Rendered inline, in the agent's section, and
    never redirected: the raw token is shown exactly once and a secret must
    never ride a query string."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = admin_required(user)
    if err is not None:
        return err
    assert user is not None, "admin_required accepted a missing user"

    def page(
        status_code: int, *, typed_mint: dict | None = None, **extra
    ) -> HTMLResponse:
        context = _agents_context(conn, user, **extra)
        context["typed_mint"] = typed_mint
        listed = {row["user"]["id"] for row in context["agents"]}
        context["acted_on_agent_id"] = user_id if user_id in listed else None
        return templates.TemplateResponse(
            request=request,
            name="admin/agents.html",
            context=context,
            status_code=status_code,
        )

    try:
        result = agent_commands.mint_agent_token(
            conn,
            actor=user,
            target_user_id=user_id,
            scopes=selected_scopes(
                scope_read, scope_issue_write, scope_docs_write, scope_admin
            ),
            name=token_name.strip() or None,
            profile=profile.strip() or None,
        )
    except agent_commands.AgentCommandError as exc:
        # The agent's mint form comes back holding what was typed (U11).
        typed_mint = {
            "agent_id": user_id,
            "token_name": token_name,
            "profile": profile,
            "scope_read": bool(scope_read),
            "scope_issue_write": bool(scope_issue_write),
            "scope_docs_write": bool(scope_docs_write),
            "scope_admin": bool(scope_admin),
        }
        return page(
            404 if exc.kind == "not_found" else 400,
            error=str(exc),
            typed_mint=typed_mint,
        )
    minted = {
        "user": result["user"],
        "token": result["token"],
        "config_json": json.dumps(
            claude_mcp_config(
                base_url=str(request.base_url).rstrip("/"),
                token=result["token"]["token"],
                profile=result["profile"],
                command=athena_mcp_command(),
            ),
            indent=2,
        ),
    }
    return page(201, minted=minted)


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
        return BrowserRefusal(
            f"<h1>Invalid active-work request</h1><p>{escape(exc.detail)}</p>",
            status_code=400,
            headers=ACTIVE_WORK_PRIVATE_HEADERS,
        )
    context = agents.agent_run_health(conn, agent_id=agent_id)
    # Each run row's derived state. The run-health read lives in core, which may
    # not import aegis, so the state is composed here, one per listed run. A
    # completed claim has left active work; this row is where "complete" shows.
    for checkin in context["latest_checkins"]:
        checkin["run_state"] = run_state.state_for_run(
            conn, agent_id=int(checkin["agent_id"]), run_id=checkin["run_id"]
        )["state"]
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
        return JSONResponse(
            {"detail": "no such agent run", "code": "not_found"}, status_code=404
        )
    try:
        artifact = run_replay.build_run_replay_artifact(conn, run_id)
    except ValueError as exc:
        return JSONResponse(
            {"detail": str(exc), "code": "validation_error"}, status_code=422
        )
    if artifact is None:
        return JSONResponse(
            {"detail": "no such agent run", "code": "not_found"}, status_code=404
        )
    return JSONResponse(artifact)
