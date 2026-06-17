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
