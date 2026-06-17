"""Web layer: server-rendered HTML pages (Jinja + HTMX) over the API.

This package is intentionally separate from future REST api/ routes.
"""
from .router import init_templates, router

__all__ = ["init_templates", "router"]
