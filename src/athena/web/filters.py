"""Web routes for saved filters — a user's named, reusable issue queries.

Split out of web/router.py (the god-file) to keep each web surface navigable,
following the same one-module-per-area pattern as web/projects.py and friends. Its
own APIRouter, mounted by main.py. A thin client over the saved_filters/issue_search
data layers — it owns no data. The shared label helper and template accessor are
imported from web.router.
"""
from __future__ import annotations

import html
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import issue_search, issues, projects, saved_filters
from athena.core import access, labels, users
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.render import render_snippet
from athena.web.router import _attach_labels, get_templates

router = APIRouter()


def _describe_criteria(conn, criteria: dict, actor: dict | None) -> str:
    """Describe canonical, validated criteria for the list/detail views,
    resolving ids to names (assignee → display name, project id → project name).
    Empty criteria reads as "all issues" — an unconstrained filter is still a view.

    The project NAME is resolved only when `actor` may see that project: a private
    project the viewer isn't in renders as its id (#N), never its name. Otherwise any
    user could save a filter with `project: <private_id>` and read a hidden project's
    name back here (validate_criteria accepts any numeric id) — the same hidden==missing
    rule the rest of the app enforces."""
    parts: list[str] = []
    if criteria.get("status"):
        parts.append(f"status: {criteria['status']}")
    if criteria.get("priority"):
        parts.append(f"priority: {criteria['priority']}")
    if criteria.get("assignee_id") is not None:
        user = users.get_user(conn, criteria["assignee_id"])
        parts.append(f"assignee: {user['name'] if user else '#' + str(criteria['assignee_id'])}")
    if criteria.get("label"):
        parts.append(f"label: {criteria['label']}")
    if criteria.get("project"):
        value = criteria["project"]
        if value.lower() == "none":
            parts.append("project: backlog")
        elif value.isdigit():
            pid = int(value)
            if access.can_see_project_or_backlog(conn, actor, pid):
                project = projects.get_project(conn, pid)
                parts.append(f"project: {project['name'] if project else '#' + value}")
            else:
                parts.append(f"project: #{value}")  # id only — never the hidden name
        else:
            parts.append(f"project: {value}")
    if criteria.get("search"):
        parts.append(f"search: “{criteria['search']}”")
    return ", ".join(parts) if parts else "all issues"


@router.get("/aegis/filters", response_class=HTMLResponse)
def filters_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The signed-in user's saved filters, plus a form to add one. Filters are
    PERSONAL (each user sees only their own), the same scoping as the inbox, so this
    view requires a session. The create form can be pre-filled from query params
    (the "Save current view" link on the issues list passes the active filters),
    so building a saved filter from an ad-hoc search is one click."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to save filters.</div>',
            status_code=401,
        )
    my_filters = saved_filters.list_filters(conn, user["id"])
    for flt in my_filters:
        criteria = saved_filters.normalized_valid_criteria(flt["criteria"])
        flt["summary"] = (
            _describe_criteria(conn, criteria, user)
            if criteria is not None
            else "invalid filter"
        )
    # Pre-fill values for the create form, taken from the query string (all blank by
    # default). project/label/assignee dropdowns echo the chosen option as selected.
    prefill = {
        "name": (request.query_params.get("name") or "").strip(),
        "status": (request.query_params.get("status") or "").strip(),
        "priority": (request.query_params.get("priority") or "").strip(),
        "assignee_id": (request.query_params.get("assignee_id") or "").strip(),
        "label": (request.query_params.get("label") or "").strip(),
        "project": (request.query_params.get("project") or "").strip(),
        "search": (request.query_params.get("search") or "").strip(),
    }
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/filters.html",
        context={
            "filters": my_filters,
            "prefill": prefill,
            "priorities": issues.PRIORITIES,
            "all_labels": labels.list_labels(conn),
            "all_projects": projects.list_projects(conn, access.visible_project_filter(conn, user)),
            "all_users": users.list_users(conn),
        },
    )


@router.post("/aegis/filters", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def create_filter(
    request: Request,
    name: str = Form(""),
    status: str = Form(""),
    priority: str = Form(""),
    assignee_id: str = Form(""),
    label: str = Form(""),
    project: str = Form(""),
    search: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save a filter for the logged-in user. The owner is the session, never a form
    field — the same rule the REST API enforces. Validation goes through the SAME
    normalize/validate path the API uses, so the two surfaces reject the same input.
    Empty name → 400, bad priority/project → 400, duplicate name → 409; otherwise
    303 back to the filters list."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to save filters.</div>',
            status_code=401,
        )
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Filter name is required.</div>', status_code=400)
    try:
        criteria = saved_filters.normalize_criteria(
            {
                "status": status,
                "priority": priority,
                "assignee_id": assignee_id,
                "label": label,
                "project": project,
                "search": search,
            }
        )
    except saved_filters.InvalidFilterCriteria as exc:
        return HTMLResponse(
            f'<div class="error">{html.escape(str(exc))}.</div>', status_code=400
        )
    reason = saved_filters.validate_criteria(criteria)
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}.</div>', status_code=400)
    try:
        saved_filters.create_filter(conn, owner_id=user["id"], name=name, criteria=criteria)
    except sqlite3.IntegrityError:
        return HTMLResponse(
            '<div class="error">You already have a filter with that name.</div>',
            status_code=409,
        )
    return RedirectResponse("/aegis/filters", status_code=303)


_FILTER_SEARCH_LIMIT = 100


@router.get("/aegis/filters/{filter_id}", response_class=HTMLResponse)
def filter_detail(
    request: Request, filter_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Run one saved filter and show the matching issues. Owner-only — a filter that
    isn't yours reads as 404, never revealing it exists.

    With a ?q= present this becomes "search WITHIN this filter": the filter's full
    criteria are handed to issue_search.search_issues along with the query, so the
    results are the filter's issues narrowed to those matching the text, ranked by
    relevance with snippets — the same intersection (filter ∩ text) the /find page
    runs, but anchored to a saved filter's exact criteria. Without a query it's the
    plain run (run_filter), the same path the REST /filters/{id}/issues uses, so the
    browser and the API can't disagree on what a saved filter returns."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to run filters.</div>',
            status_code=401,
        )
    flt = saved_filters.get_filter(conn, filter_id)
    if flt is None or flt["owner_id"] != user["id"]:
        return HTMLResponse('<div class="error">No such filter.</div>', status_code=404)
    crit = saved_filters.normalized_valid_criteria(flt["criteria"])
    flt["summary"] = (
        _describe_criteria(conn, crit, user) if crit is not None else "invalid filter"
    )
    q = (request.query_params.get("q") or "").strip()
    context: dict = {"filter": flt, "q": q, "searching": bool(q)}
    if q:
        # Search within the filter: text query intersected with the filter's criteria.
        hits = (
            issue_search.search_issues(
                conn,
                q,
                status=crit.get("status"),
                priority=crit.get("priority"),
                assignee_id=crit.get("assignee_id"),
                label=crit.get("label"),
                project=crit.get("project"),
                limit=_FILTER_SEARCH_LIMIT,
                actor=user,
            )
            if crit is not None
            else []
        )
        for h in hits:
            h["snippet_html"] = render_snippet(h.get("snippet"))
            h["href"] = f"/aegis/issues/{h['source_id']}"
        context.update(hits=hits, total=len(hits))
    else:
        matches = (
            saved_filters.run_filter(
                conn,
                crit,
                visible_project_ids=access.visible_project_filter(conn, user),
            )
            if crit is not None
            else []
        )
        _attach_labels(conn, matches)
        context.update(issues=matches, total=len(matches))
    return get_templates().TemplateResponse(
        request=request, name="aegis/filter_detail.html", context=context
    )


@router.post(
    "/aegis/filters/{filter_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def delete_filter(
    request: Request, filter_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Delete one of the logged-in user's filters, then back to the list. Owner-only
    (404 if it isn't theirs), so a user can't delete another's saved query."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    flt = saved_filters.get_filter(conn, filter_id)
    if flt is None or flt["owner_id"] != user["id"]:
        return HTMLResponse('<div class="error">No such filter.</div>', status_code=404)
    saved_filters.delete_filter(conn, filter_id)
    return RedirectResponse("/aegis/filters", status_code=303)
