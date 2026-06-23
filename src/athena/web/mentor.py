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

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.core.deps import get_conn
from athena.mentor import pages, spaces
from athena.web.router import get_templates

router = APIRouter()


def _signin_required(verb: str) -> HTMLResponse:
    """The 401 body shown when a logged-out browser tries to write."""
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


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


# --- Spaces -----------------------------------------------------------------


@router.get("/mentor", response_class=HTMLResponse)
def spaces_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """List every space with a New Space form (the form is gated; reading is open).
    Each space links to its page tree. Mirrors /aegis/projects."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    all_spaces = spaces.list_spaces(conn)
    # One page-count per space — cheap on the small lists Mentor holds, and it
    # comes from the real data layer (no cached counter to drift).
    counts = {
        s["id"]: len(pages.list_pages_in_space(conn, s["id"])) for s in all_spaces
    }
    return templates.TemplateResponse(
        request=request,
        name="mentor/spaces.html",
        context={"spaces": all_spaces, "counts": counts},
    )


@router.post("/mentor/spaces")
def create_space(
    request: Request,
    key: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create a space as the logged-in user. Mirrors the API's create_space rules:
    key normalized to UPPERCASE (so "eng" == "ENG"), key + name required, duplicate
    key → 409. Actor is the session, never a form field."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("create spaces")

    key = key.strip().upper()
    name = name.strip()
    if not key:
        return HTMLResponse('<div class="error">Space key is required.</div>', status_code=400)
    if not name:
        return HTMLResponse('<div class="error">Space name is required.</div>', status_code=400)
    if spaces.get_space_by_key(conn, key) is not None:
        return HTMLResponse('<div class="error">A space with that key already exists.</div>', status_code=409)

    space = spaces.create_space(
        conn, key=key, name=name, description=description.strip(), created_by=user["id"]
    )
    return RedirectResponse(f"/mentor/spaces/{space['id']}", status_code=303)


@router.get("/mentor/spaces/{space_id}", response_class=HTMLResponse)
def space_detail(
    request: Request, space_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """A space's page tree plus a New Page form (gated). 404-ish empty render if the
    space doesn't exist (consistent with the Aegis not-found handling)."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    space = spaces.get_space(conn, space_id)
    if space is None:
        return templates.TemplateResponse(
            request=request,
            name="mentor/spaces.html",
            context={"spaces": spaces.list_spaces(conn), "counts": {},
                     "error": f"Space #{space_id} not found"},
            status_code=404,
        )

    page_rows = pages.list_pages_in_space(conn, space_id)
    return templates.TemplateResponse(
        request=request,
        name="mentor/space_detail.html",
        context={
            "space": space,
            "tree": _tree_rows(page_rows),
            # Flat list (alpha) for the optional "nest under" parent select.
            "all_pages": page_rows,
        },
    )


@router.post("/mentor/spaces/{space_id}/pages")
def create_page(
    request: Request,
    space_id: int,
    title: str = Form(""),
    body: str = Form(""),
    parent_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create a page in a space as the logged-in user. Mirrors the API: 404 if the
    space is missing, title required, and an optional parent must be a real page IN
    THIS SAME SPACE (the cross-space tree rule the FK can't express). 303 to the new
    page on success."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("create pages")

    if spaces.get_space(conn, space_id) is None:
        return HTMLResponse('<div class="error">Space not found.</div>', status_code=404)

    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Page title is required.</div>', status_code=400)

    parent_id = parent_id.strip()
    if parent_id == "":
        parent: int | None = None
    else:
        if not parent_id.isdigit():
            return HTMLResponse('<div class="error">Invalid parent page.</div>', status_code=400)
        parent_page = pages.get_page(conn, int(parent_id))
        if parent_page is None or parent_page["space_id"] != space_id:
            return HTMLResponse(
                '<div class="error">Parent must be a page in this space.</div>',
                status_code=400,
            )
        parent = int(parent_id)

    page = pages.create_page(
        conn, space_id=space_id, title=title, body=body.strip() or "",
        parent_id=parent, created_by=user["id"],
    )
    return RedirectResponse(f"/mentor/pages/{page['id']}", status_code=303)


# --- Pages ------------------------------------------------------------------


@router.get("/mentor/pages/{page_id}", response_class=HTMLResponse)
def page_detail(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Show one page: its current title/body, the space it belongs to, an Edit link
    (logged-in only), and its version history (superseded revisions, newest first)."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    page = pages.get_page(conn, page_id)
    if page is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="mentor/page_detail.html",
        context={
            "page": page,
            "space": spaces.get_space(conn, page["space_id"]),
            "versions": pages.list_page_versions(conn, page_id),
        },
    )


@router.get("/mentor/pages/{page_id}/edit", response_class=HTMLResponse)
def edit_page_form(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the edit form prefilled with the page's current title/body. Editing is
    a write, so logged-out callers get a sign-in prompt rather than a dead form."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("edit pages")

    page = pages.get_page(conn, page_id)
    if page is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="mentor/page_edit.html",
        context={"page": page, "space": spaces.get_space(conn, page["space_id"])},
    )


@router.post("/mentor/pages/{page_id}/edit")
def edit_page(
    request: Request,
    page_id: int,
    title: str = Form(""),
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save edits to a page's title/body. Gated on the session user; empty title is
    rejected. update_page snapshots the prior revision into history before
    overwriting (see mentor/pages.py), then we 303 back to the page."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("edit pages")

    if pages.get_page(conn, page_id) is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Title is required.</div>', status_code=400)

    pages.update_page(conn, page_id, editor_id=user["id"], title=title, body=body.strip())
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)
