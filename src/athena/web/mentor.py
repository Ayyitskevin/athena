"""Mentor web surface: the browser-facing thin client over the spaces/pages API.

Mirrors web/auth.py's split-router shape (its own APIRouter, templates fetched
via web.router.get_templates) and web/router.py's Aegis conventions. It owns NO
data: every route reads through mentor.spaces / mentor.pages (the same data-access
the REST API uses) and every write is gated on the browser session
(request.state.user), never a form field — the cardinal AGENTS.md rule.

Mentor's authorization is deliberately simpler than Aegis's: reads are open and
writes are open to ANY authenticated actor (a page has no creator-only lock — it's
a shared wiki and every edit is snapshotted into history), so the only gate is
"are you signed in?" — there is no creator-or-assignee check like issues have.
"""

from __future__ import annotations


from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from athena.core import (
    access,
    identity,
)
from athena.mentor import (
    pages,
)

from athena.web.router import _readonly_response

# Attachment-command refusal codes this browser adapter answers with.
_ATTACHMENT_STATUS_BY_KIND = {"invalid": 422, "not_found": 404, "forbidden": 403}

router = APIRouter()


def _signin_required(verb: str) -> HTMLResponse:
    """The 401 body shown when a logged-out browser tries to write."""
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


def _write_required(user: dict | None, verb: str) -> HTMLResponse | None:
    if user is None:
        return _signin_required(verb)
    if not identity.can_write(user):
        return _readonly_response()
    return None


def _page_visible_or_response(conn, page_id, user):
    """Return (page, None) if the user may SEE this page (its space is visible to them),
    else (None, 404 response). The web write-side visibility gate — a page in a private
    space the user can't read is "not found", so its existence and content never leak
    through a write path. The browser twin of the API's _page_for_read; every page write
    funnels through here so visibility can't be forgotten on one."""
    page = pages.get_page(conn, page_id)
    if page is None or not access.can_see_space(conn, user, page["space_id"]):
        return None, HTMLResponse(
            '<div class="error">Page not found.</div>', status_code=404
        )
    return page, None


def _tree_rows(page_rows: list[dict]) -> list[dict]:
    """Flatten a space's pages into display order with a nesting depth on each.

    The data layer hands us a flat list (alphabetical by title, each row carrying
    its parent_id). Shaping that into a tree is a presentation concern, so it lives
    here, not in SQL. We do a depth-first walk: each parent is immediately followed
    by its children (already alphabetical because the source list is), and every
    row gets a `depth` the template indents by. A page whose parent isn't in this
    set is treated as a root so nothing can silently vanish from the tree.
    """
    children: dict[int | None, list[dict]] = {}
    ids = {p["id"] for p in page_rows}
    for p in page_rows:
        parent = p["parent_id"] if p["parent_id"] in ids else None
        children.setdefault(parent, []).append(p)

    ordered: list[dict] = []

    def walk(parent_id: int | None, depth: int) -> None:
        for child in children.get(parent_id, []):
            ordered.append({**child, "depth": depth})
            walk(child["id"], depth + 1)

    walk(None, 0)
    return ordered


# --- The knowledge graph ----------------------------------------------------


# --- Spaces -----------------------------------------------------------------


# --- Space access: privacy toggle + member management (web) ----------------
#
# The Mentor twin of the project access page. Managing access is creator-OR-admin
# (wider than delete, creator-only), so it uses its own gate rather than _write_required.


# --- Pages ------------------------------------------------------------------


# --- Page comments ----------------------------------------------------------


# --- Page labels ------------------------------------------------------------
