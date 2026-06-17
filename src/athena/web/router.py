"""HTML page routes (APIRouter).

The actual page rendering logic lives here. The Jinja2Templates instance
is configured in main.py (per wiring contract) and injected via init_templates.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from athena.aegis import issues
from athena.core.deps import get_conn

router = APIRouter()

# Populated at app startup from main.py wiring.
_templates: Jinja2Templates | None = None


def init_templates(templates: Jinja2Templates) -> None:
    """Receive the configured Jinja2Templates from the app factory."""
    global _templates
    _templates = templates


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Simple landing page. No dynamic data yet — just the foundation."""
    if _templates is None:
        # Should never happen in normal operation (wired in lifespan).
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    return _templates.TemplateResponse(
        request=request,
        name="home.html",
    )


@router.get("/aegis", response_class=HTMLResponse)
def aegis(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Aegis dashboard using real data from list_issues."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    all_issues = issues.list_issues(conn)
    from collections import Counter
    status_counts = Counter(issue["status"] for issue in all_issues)
    # Recent issues (newest first)
    recent_issues = sorted(
        all_issues, key=lambda x: x.get("created_at", ""), reverse=True
    )[:5]

    return _templates.TemplateResponse(
        request=request,
        name="aegis.html",
        context={
            "status_counts": dict(status_counts),
            "recent_issues": recent_issues,
            "total_issues": len(all_issues),
        },
    )


@router.get("/aegis/issues", response_class=HTMLResponse)
def issues_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Issues list view (Aegis) wired to real DB via data-access layer (list_issues)."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    status_filter = request.query_params.get("status")
    search = (request.query_params.get("search") or "").strip().lower()
    sort = request.query_params.get("sort", "created_at")
    order = request.query_params.get("order", "desc")

    all_issues = issues.list_issues(conn)
    filtered = all_issues
    if status_filter:
        filtered = [i for i in filtered if i["status"] == status_filter]
    if search:
        filtered = [
            i
            for i in filtered
            if search in i["title"].lower() or search in (i.get("body") or "").lower()
        ]

    # Sort in web layer (presentation concern) – safe since we don't own data
    reverse = order == "desc"
    try:
        filtered = sorted(filtered, key=lambda x: x.get(sort) or "", reverse=reverse)
    except Exception:
        pass  # fallback if sort key bad

    template = "aegis/partials/issues_table.html" if request.headers.get("HX-Request") else "aegis/issues.html"
    return _templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "issues": filtered,
            "status_filter": status_filter or "",
            "search": search,
            "sort": sort,
            "order": order,
        },
    )


@router.get("/aegis/issues/new", response_class=HTMLResponse)
def new_issue_form(request: Request):
    """Render the new issue creation form."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_form.html",
    )


@router.post("/aegis/issues", response_class=HTMLResponse)
async def create_issue(request: Request):
    """Create is currently blocked: requires user accounts (auth in core).

    See AGENTS.md cardinal rule and this task's blocker note.
    """
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    # Return blocked state into the form's result target. Do not write.
    return HTMLResponse(
        "<div class=\"blocked\">issue creation needs user accounts (coming in core/auth)</div>"
    )


@router.get("/aegis/issues/{issue_id}", response_class=HTMLResponse)
def issue_detail(request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """Show a single issue from real DB via get_issue."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    issue = issues.get_issue(conn, issue_id)
    if not issue:
        # not-found state: render empty list page with error (minimal)
        return _templates.TemplateResponse(
            request=request,
            name="aegis/issues.html",
            context={"issues": [], "status_filter": "", "search": "", "error": f"Issue #{issue_id} not found"},
        )

    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_detail.html",
        context={"issue": issue},
    )


@router.get("/aegis/boards", response_class=HTMLResponse)
def boards(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Boards view (Aegis) using real list_issues."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    all_issues = issues.list_issues(conn)
    from collections import defaultdict
    columns = defaultdict(list)
    for issue in all_issues:
        columns[issue.get("status", "open")].append(issue)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/boards.html",
        context={"columns": dict(columns)},
    )
