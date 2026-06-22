"""HTML page routes (APIRouter).

The actual page rendering logic lives here. The Jinja2Templates instance
is configured in main.py (per wiring contract) and injected via init_templates.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from athena.aegis import comments, issues
from athena.core import users
from athena.core.deps import get_conn

router = APIRouter()

# Populated at app startup from main.py wiring.
_templates: Jinja2Templates | None = None


def init_templates(templates: Jinja2Templates) -> None:
    """Receive the configured Jinja2Templates from the app factory."""
    global _templates
    _templates = templates


def get_templates() -> Jinja2Templates | None:
    """The configured templates instance, for other web routers (e.g. auth)."""
    return _templates


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

    # Simple pagination (server-side slice)
    try:
        page = max(1, int(request.query_params.get("page", 1)))
        per_page = max(5, min(50, int(request.query_params.get("per_page", 20))))
    except:
        page, per_page = 1, 20

    total = len(filtered)
    start = (page - 1) * per_page
    paged = filtered[start : start + per_page]

    template = "aegis/partials/issues_table.html" if request.headers.get("HX-Request") else "aegis/issues.html"
    return _templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "issues": paged,
            "status_filter": status_filter or "",
            "search": search,
            "sort": sort,
            "order": order,
            "page": page,
            "per_page": per_page,
            "total": total,
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
def create_issue(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    status: str = Form("open"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create an issue as the logged-in user. The actor is the browser session
    (request.state.user), never a field in the form — same rule as the REST API.
    Logged-out callers get a prompt to sign in instead of a write."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to create issues.</div>',
            status_code=401,
        )

    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Title is required.</div>', status_code=400)
    if status not in issues.STATUSES:
        return HTMLResponse('<div class="error">Unknown status.</div>', status_code=400)
    body = body.strip()

    issue = issues.create_issue(
        conn, title=title, body=body, status=status, created_by=user["id"]
    )
    # HTMX swaps this into #create-result; nudge the browser to the new issue.
    return HTMLResponse(
        f'<div class="success">Created issue #{issue["id"]}.</div>'
        f'<script>window.location.href="/aegis/issues/{issue["id"]}";</script>'
    )


@router.get("/aegis/issues/{issue_id}/edit", response_class=HTMLResponse)
def edit_issue_form(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the edit form for an issue, prefilled with its current title/body.
    Gated on the session user — editing is a write, so logged-out callers get a
    sign-in prompt rather than a form they can't submit."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    issue = issues.get_issue(conn, issue_id)
    if not issue:
        return HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_edit.html",
        context={"issue": issue},
    )


@router.post("/aegis/issues/{issue_id}/edit")
def edit_issue(
    request: Request,
    issue_id: int,
    title: str = Form(""),
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save edits to an issue's title and body from the edit form. Gated on the
    session user (same actor rule as every write), rejects an empty title, then
    303-redirects back to the issue so it reloads with the new content."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Title is required.</div>', status_code=400)

    updated = issues.update_issue(conn, issue_id, title=title, body=body.strip())
    if updated is None:
        return HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/status")
def change_issue_status(
    request: Request,
    issue_id: int,
    status: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Move an issue to a new status from the detail page. Gated on the session
    user (same actor rule as create), validates against the lifecycle, then
    303-redirects back to the issue so the page reloads with the new state."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to change status.</div>',
            status_code=401,
        )
    if status not in issues.STATUSES:
        return HTMLResponse('<div class="error">Unknown status.</div>', status_code=400)

    updated = issues.update_status(conn, issue_id, status)
    if updated is None:
        return HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


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
        context={
            "issue": issue,
            "comments": comments.list_comments(conn, issue_id),
            "users": users.list_users(conn),
        },
    )


@router.post("/aegis/issues/{issue_id}/assignee")
def change_issue_assignee(
    request: Request,
    issue_id: int,
    assignee_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Assign or unassign an issue from the detail page. Gated on the session
    user (same actor rule as status/comments). An empty form value means
    "Unassigned" (None); otherwise the value must be a real user id."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to assign.</div>',
            status_code=401,
        )

    assignee_id = assignee_id.strip()
    if assignee_id == "":
        target: int | None = None
    else:
        try:
            target = int(assignee_id)
        except ValueError:
            return HTMLResponse('<div class="error">Invalid user.</div>', status_code=400)
        if users.get_user(conn, target) is None:
            return HTMLResponse('<div class="error">No such user.</div>', status_code=400)

    updated = issues.set_assignee(conn, issue_id, target)
    if updated is None:
        return HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/comments")
def add_issue_comment(
    request: Request,
    issue_id: int,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Post a comment from the detail page. Gated on the session user (the
    author is the session, never a form field), then 303-redirects back to the
    issue so the new comment shows."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to comment.</div>',
            status_code=401,
        )
    if issues.get_issue(conn, issue_id) is None:
        return HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    body = body.strip()
    if not body:
        return HTMLResponse('<div class="error">Comment cannot be empty.</div>', status_code=400)

    comments.add_comment(conn, issue_id=issue_id, author_id=user["id"], body=body)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


def _own_comment_or_response(conn, issue_id, comment_id, user):
    """Return the comment if it belongs to this issue and the session user is its
    author; otherwise an HTMLResponse (404/403) to return as-is. Mirrors the
    API's author-ownership rule on the web write paths."""
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
        return None, HTMLResponse('<div class="error">Comment not found.</div>', status_code=404)
    if existing["author_id"] != user["id"]:
        return None, HTMLResponse('<div class="error">You can only change your own comments.</div>', status_code=403)
    return existing, None


@router.post("/aegis/issues/{issue_id}/comments/{comment_id}/edit")
def edit_issue_comment(
    request: Request,
    issue_id: int,
    comment_id: int,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Edit a comment from the detail page. Gated on the session user AND on
    author-ownership (you may only edit your own), then 303 back to the issue."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit comments.</div>',
            status_code=401,
        )
    _, err = _own_comment_or_response(conn, issue_id, comment_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse('<div class="error">Comment cannot be empty.</div>', status_code=400)
    comments.update_comment(conn, comment_id, body=body)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/comments/{comment_id}/delete")
def delete_issue_comment(
    request: Request,
    issue_id: int,
    comment_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delete a comment from the detail page. Same author-ownership rule as edit.
    Uses POST (not DELETE) because HTML forms can't issue DELETE."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to delete comments.</div>',
            status_code=401,
        )
    _, err = _own_comment_or_response(conn, issue_id, comment_id, user)
    if err is not None:
        return err
    comments.delete_comment(conn, comment_id)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.get("/aegis/boards", response_class=HTMLResponse)
def boards(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Boards view (Aegis) using real list_issues with search/filter."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    search = (request.query_params.get("search") or "").strip().lower()
    status_filter = request.query_params.get("status")

    all_issues = issues.list_issues(conn)
    filtered = all_issues
    if status_filter:
        filtered = [i for i in filtered if i.get("status") == status_filter]
    if search:
        filtered = [
            i for i in filtered
            if search in i.get("title", "").lower() or search in (i.get("body") or "").lower()
        ]

    from collections import defaultdict
    columns = defaultdict(list)
    for issue in filtered:
        columns[issue.get("status", "open")].append(issue)

    template = "aegis/partials/boards_content.html" if request.headers.get("HX-Request") else "aegis/boards.html"
    return _templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "columns": dict(columns),
            "search": search,
            "status_filter": status_filter or "",
            "all_issues": all_issues,  # for counts if wanted
        },
    )
