"""Shared Mentor browser gates.

The page, space, and graph routes live in mentor_pages, mentor_spaces, and
mentor_graph. This module owns the sign-in, write, and visibility checks those
routes share, so they do not import private names from each other. It owns no
data: every check reads through mentor.pages and core.access.

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

from athena.web.browsing import readonly_response

router = APIRouter()


def signin_required(verb: str) -> HTMLResponse:
    """The 401 body shown when a logged-out browser tries to write."""
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


def write_required(user: dict | None, verb: str) -> HTMLResponse | None:
    if user is None:
        return signin_required(verb)
    if not identity.can_write(user):
        return readonly_response()
    return None


def page_visible_or_response(conn, page_id, user):
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


def tree_rows(page_rows: list[dict]) -> list[dict]:
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
