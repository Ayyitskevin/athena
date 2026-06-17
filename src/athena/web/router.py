"""HTML page routes (APIRouter).

The actual page rendering logic lives here. The Jinja2Templates instance
is configured in main.py (per wiring contract) and injected via init_templates.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

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
def aegis(request: Request):
    """Aegis module landing / dashboard stub."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    return _templates.TemplateResponse(
        request=request,
        name="aegis.html",
    )


# Temporary in-memory stub data for UI development only.
# Will be replaced by real data fetched from the backend REST API.
_next_issue_id = 4
SAMPLE_ISSUES = [
    {
        "id": 1,
        "title": "Bootstrap the web foundation",
        "status": "done",
        "created_at": "2026-06-17",
        "body": "Set up Jinja2, HTMX, base templates, and static assets for the web layer.",
    },
    {
        "id": 2,
        "title": "Define initial issue statuses and workflow",
        "status": "open",
        "created_at": "2026-06-17",
        "body": "",
    },
    {
        "id": 3,
        "title": "Add first board view (Aegis)",
        "status": "in_progress",
        "created_at": "2026-06-16",
        "body": "Visual board with columns for open / in progress / done.",
    },
]


@router.get("/aegis/issues", response_class=HTMLResponse)
def issues_list(request: Request):
    """Issues list view (Aegis). Currently renders stub data.

    When the REST API is ready this can be swapped to fetch from the backend
    or use HTMX to load fragments.
    """
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    status_filter = request.query_params.get("status")
    issues = SAMPLE_ISSUES
    if status_filter:
        issues = [i for i in SAMPLE_ISSUES if i["status"] == status_filter]

    return _templates.TemplateResponse(
        request=request,
        name="aegis/issues.html",
        context={
            "issues": issues,
            "status_filter": status_filter or "",
            "all_issues": SAMPLE_ISSUES,
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
    """Handle create issue form submission (HTMX).

    For now this is a demo that 'creates' the issue in the stub store
    and returns a success fragment. When the real API exists, this
    route can either proxy or be removed in favor of direct API calls from the form.
    """
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    form = await request.form()
    title = form.get("title", "").strip()
    body = form.get("body", "").strip()
    status = form.get("status", "open")

    if not title:
        return _templates.TemplateResponse(
            request=request,
            name="aegis/issue_form.html",
            context={"error": "Title is required"},
        )

    global _next_issue_id
    new_issue = {
        "id": _next_issue_id,
        "title": title,
        "status": status,
        "created_at": "2026-06-17",  # demo
        "body": body,
    }
    SAMPLE_ISSUES.append(new_issue)
    _next_issue_id += 1

    # Return success fragment (HTMX will swap it in)
    return _templates.TemplateResponse(
        request=request,
        name="partials/create_success.html",
        context={"issue": new_issue},
    )


@router.get("/aegis/issues/{issue_id}", response_class=HTMLResponse)
def issue_detail(request: Request, issue_id: int):
    """Show a single issue (stub)."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    issue = next((i for i in SAMPLE_ISSUES if i["id"] == issue_id), None)
    if not issue:
        return _templates.TemplateResponse(
            request=request,
            name="aegis/issues.html",
            context={"issues": SAMPLE_ISSUES, "error": f"Issue #{issue_id} not found"},
        )

    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_detail.html",
        context={"issue": issue},
    )


@router.get("/aegis/boards", response_class=HTMLResponse)
def boards(request: Request):
    """Boards view stub (Kanban style placeholder)."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/boards.html",
        context={"issues": SAMPLE_ISSUES},
    )
