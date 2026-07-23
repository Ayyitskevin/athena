"""Web layer: server-rendered HTML pages (Jinja + HTMX) over the API.

A thin client only: routes here read and write through the same data-access
modules the per-module REST surfaces (aegis/api.py, mentor/api.py, core/*_api.py)
are built on, and never own data of their own.
"""

from .router import init_templates, router

__all__ = ["init_templates", "router"]
