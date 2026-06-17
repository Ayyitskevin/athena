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


# Temporary stub data. Will be replaced by real data from the API layer.
SAMPLE_ISSUES = [
    {
        "id": 1,
        "title": "Bootstrap the web foundation",
        "status": "done",
        "created_at": "2026-06-17",
    },
    {
        "id": 2,
        "title": "Define initial issue statuses and workflow",
        "status": "open",
        "created_at": "2026-06-17",
    },
    {
        "id": 3,
        "title": "Add first board view (Aegis)",
        "status": "in_progress",
        "created_at": "2026-06-16",
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
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issues.html",
        context={"issues": SAMPLE_ISSUES},
    )
