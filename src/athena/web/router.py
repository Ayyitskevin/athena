"""HTML page routes (APIRouter).

The actual page rendering logic lives here. The Jinja2Templates instance
is configured in main.py (per wiring contract) and injected via init_templates.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from athena.aegis import comments, issues, labels, projects
from athena.core import links, search, users
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.render import render_body, render_snippet

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


def _attach_labels(conn, rows: list[dict]) -> list[dict]:
    """Merge each issue's labels onto it under a "labels" key, in one bulk query
    (no N+1). Mirrors the API's _with_labels_many so list/board cards can render
    their label chips. Issues with no labels get an empty list."""
    by_issue = labels.labels_for_issues(conn, [r["id"] for r in rows])
    for r in rows:
        r["labels"] = by_issue.get(r["id"], [])
    return rows


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


@router.get("/find", response_class=HTMLResponse)
def find(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Human-facing search across issues and pages — the browser twin of the JSON
    API at /search (which serves the fleet). It runs the SAME core.search query, so
    the two never disagree on what matches or how it ranks; this route only adds the
    presentation: an <a> per hit to its detail page and a highlighted snippet.
    Reading is open, like every other web read; a blank box just shows the form."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    q = (request.query_params.get("q") or "").strip()
    hits = search.search(conn, q) if q else []
    for h in hits:
        # render_snippet escapes then turns search's [..] match markers into <mark>;
        # the href maps a hit's kind to where it lives in the web UI.
        h["snippet_html"] = render_snippet(h.get("snippet"))
        h["href"] = (
            f"/aegis/issues/{h['source_id']}" if h["kind"] == "issue"
            else f"/mentor/pages/{h['source_id']}"
        )
    return _templates.TemplateResponse(
        request=request,
        name="search.html",
        context={"q": q, "hits": hits},
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
    label_filter = (request.query_params.get("label") or "").strip()
    project_raw = (request.query_params.get("project") or "").strip()
    project_id = int(project_raw) if project_raw.isdigit() else None
    search = (request.query_params.get("search") or "").strip().lower()
    sort = request.query_params.get("sort", "created_at")
    order = request.query_params.get("order", "desc")

    # Filtering goes through the shared data-layer path (same one the API uses),
    # so the list and the API never disagree on what matches. The web layer then
    # only does the presentation concerns — sort + pagination — on the result.
    ids = labels.issue_ids_for_label(conn, label_filter) if label_filter else None
    filtered = issues.list_issues(
        conn, status=status_filter, search=search, project_id=project_id, ids=ids
    )
    _attach_labels(conn, filtered)  # one bulk query; paged slice carries its chips

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
    paged = filtered[start : start + per_page]  # labels already attached

    template = "aegis/partials/issues_table.html" if request.headers.get("HX-Request") else "aegis/issues.html"
    return _templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "issues": paged,
            "status_filter": status_filter or "",
            "label_filter": label_filter,
            "all_labels": labels.list_labels(conn),
            "project_filter": project_raw,
            "all_projects": projects.list_projects(conn),
            "search": search,
            "sort": sort,
            "order": order,
            "page": page,
            "per_page": per_page,
            "total": total,
        },
    )


@router.get("/aegis/issues/new", response_class=HTMLResponse)
def new_issue_form(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Render the new issue creation form."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_form.html",
        context={"all_projects": projects.list_projects(conn)},
    )


@router.post("/aegis/issues", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def create_issue(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    status: str = Form("open"),
    priority: str = Form("medium"),
    project_id: str = Form(""),
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
    if priority not in issues.PRIORITIES:
        return HTMLResponse('<div class="error">Unknown priority.</div>', status_code=400)
    # Project is optional ("" = no project); if given it must be a real project.
    project_id = project_id.strip()
    if project_id == "":
        project: int | None = None
    else:
        if not project_id.isdigit() or projects.get_project(conn, int(project_id)) is None:
            return HTMLResponse('<div class="error">No such project.</div>', status_code=400)
        project = int(project_id)
    body = body.strip()

    issue = issues.create_issue(
        conn, title=title, body=body, status=status, priority=priority,
        project_id=project, created_by=user["id"],
    )
    # HTMX swaps this into #create-result; nudge the browser to the new issue.
    return HTMLResponse(
        f'<div class="success">Created issue #{issue["id"]}.</div>'
        f'<script>window.location.href="/aegis/issues/{issue["id"]}";</script>'
    )


def _authorize_issue_write(conn, issue_id, user):
    """Return (issue, None) if the session user may modify this issue, else
    (None, HTMLResponse) with the right status. 404 if no such issue, 403 if the
    user is neither its creator nor its current assignee. Mirrors the API's
    _issue_for_write so the browser paths enforce the same creator-or-assignee
    rule. The 401 (logged-out) check stays at each call site, before this."""
    issue = issues.get_issue(conn, issue_id)
    if not issue:
        return None, HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    if not issues.can_modify(issue, user["id"]):
        return None, HTMLResponse(
            '<div class="error">Only the issue creator or assignee may modify it.</div>',
            status_code=403,
        )
    return issue, None


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
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_edit.html",
        context={"issue": issue},
    )


@router.post("/aegis/issues/{issue_id}/edit", dependencies=[Depends(verify_csrf)])
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
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Title is required.</div>', status_code=400)

    issues.update_issue(conn, issue_id, title=title, body=body.strip())
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/status", dependencies=[Depends(verify_csrf)])
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
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    if status not in issues.STATUSES:
        return HTMLResponse('<div class="error">Unknown status.</div>', status_code=400)

    issues.update_status(conn, issue_id, status)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/priority", dependencies=[Depends(verify_csrf)])
def change_issue_priority(
    request: Request,
    issue_id: int,
    priority: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Change an issue's priority from the detail page. Same gate as status:
    logged in (401) and creator-or-assignee (404/403), validated against
    PRIORITIES, then 303 back to the issue."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to change priority.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    if priority not in issues.PRIORITIES:
        return HTMLResponse('<div class="error">Unknown priority.</div>', status_code=400)

    issues.update_issue(conn, issue_id, priority=priority)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.get("/aegis/issues/{ref}", response_class=HTMLResponse)
def issue_detail(request: Request, ref: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Show a single issue, addressable by numeric id ("12") or project key
    ("ATH-12"). get_by_ref resolves either form; everything past it keys off the
    issue's real numeric id (backlinks/comments/labels stay numeric)."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    issue = issues.get_by_ref(conn, ref)
    if not issue:
        # not-found state: render empty list page with error (minimal)
        return _templates.TemplateResponse(
            request=request,
            name="aegis/issues.html",
            context={"issues": [], "status_filter": "", "search": "", "error": f"Issue {ref} not found"},
        )

    issue_id = issue["id"]
    user = getattr(request.state, "user", None)
    can_modify = bool(user) and issues.can_modify(issue, user["id"])
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_detail.html",
        context={
            "issue": issue,
            "body_html": render_body(conn, issue["body"]),
            "backlinks": links.backlinks(conn, "issue", issue_id),
            "comments": comments.list_comments(conn, issue_id),
            "users": users.list_users(conn),
            "issue_labels": labels.labels_for_issue(conn, issue_id),
            "all_labels": labels.list_labels(conn),
            "all_projects": projects.list_projects(conn),
            "can_modify": can_modify,
        },
    )


@router.post("/aegis/issues/{issue_id}/assignee", dependencies=[Depends(verify_csrf)])
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
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

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

    issues.set_assignee(conn, issue_id, target)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/project", dependencies=[Depends(verify_csrf)])
def change_issue_project(
    request: Request,
    issue_id: int,
    project_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Move an issue into a project, or remove it, from the detail page. Same gate
    as status/assign (a write). An empty form value means "no project" (None);
    otherwise the value must be a real project id."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to change project.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

    project_id = project_id.strip()
    if project_id == "":
        target: int | None = None
    else:
        if not project_id.isdigit() or projects.get_project(conn, int(project_id)) is None:
            return HTMLResponse('<div class="error">No such project.</div>', status_code=400)
        target = int(project_id)

    issues.set_project(conn, issue_id, target)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/labels", dependencies=[Depends(verify_csrf)])
def add_issue_label(
    request: Request,
    issue_id: int,
    name: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Attach a label to an issue by typing its name. Find-or-create so the user
    doesn't manage a separate vocabulary first. Same gate as status/assign — a
    label change is a write. Empty name → 400."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to label issues.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Label name is required.</div>', status_code=400)
    label = labels.get_or_create_label(conn, name=name)
    labels.add_label_to_issue(conn, issue_id, label["id"])  # idempotent
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/labels/{label_id}/delete", dependencies=[Depends(verify_csrf)])
def remove_issue_label(
    request: Request,
    issue_id: int,
    label_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Detach a label from an issue. Same write gate. POST (not DELETE) because
    HTML forms can't issue DELETE."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to label issues.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    labels.remove_label_from_issue(conn, issue_id, label_id)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/comments", dependencies=[Depends(verify_csrf)])
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


@router.post("/aegis/issues/{issue_id}/comments/{comment_id}/edit", dependencies=[Depends(verify_csrf)])
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


@router.post("/aegis/issues/{issue_id}/comments/{comment_id}/delete", dependencies=[Depends(verify_csrf)])
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

    _attach_labels(conn, filtered)
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


@router.get("/aegis/projects", response_class=HTMLResponse)
def projects_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """List all projects, each with a count of its issues and a link to the issue
    list filtered to it. Reading is open; the create form below is gated."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    all_projects = projects.list_projects(conn)
    # One count per project, cheap on the small lists we have. NULL-project issues
    # (the backlog) are simply not counted under any project.
    counts = {
        p["id"]: len(issues.list_issues(conn, project_id=p["id"]))
        for p in all_projects
    }
    return _templates.TemplateResponse(
        request=request,
        name="aegis/projects.html",
        context={"projects": all_projects, "counts": counts},
    )


@router.post("/aegis/projects", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def create_project(
    request: Request,
    name: str = Form(""),
    key: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create a project as the logged-in user (the actor is the session, never a
    form field — same rule as the REST API). Empty name → 400, bad key → 400,
    duplicate name or key → 409; otherwise 303 back to the projects list."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to create projects.</div>',
            status_code=401,
        )
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Project name is required.</div>', status_code=400)
    normalized_key = projects.normalize_key(key)
    if normalized_key is None:
        return HTMLResponse(
            '<div class="error">Key must start with a letter and be 1–10 letters/digits.</div>',
            status_code=400,
        )
    if projects.get_project_by_name(conn, name) is not None:
        return HTMLResponse('<div class="error">A project with that name already exists.</div>', status_code=409)
    if projects.get_project_by_key(conn, normalized_key) is not None:
        return HTMLResponse('<div class="error">That project key is already in use.</div>', status_code=409)
    projects.create_project(
        conn,
        name=name,
        key=normalized_key,
        description=description.strip(),
        created_by=user["id"],
    )
    return RedirectResponse("/aegis/projects", status_code=303)


def _authorize_project_write(conn, project_id: int, user: dict):
    """Resolve a project the logged-in user may edit/delete, or an error response.
    Returns (project, None) on success, or (None, HTMLResponse) with the right
    status: 404 if no such project, 403 if the user isn't its creator. Edit/delete
    is creator-only, the same rule the REST API enforces. The 401 (logged-out)
    check stays at each call site, before this."""
    project = projects.get_project(conn, project_id)
    if project is None:
        return None, HTMLResponse(
            '<div class="error">No such project.</div>', status_code=404
        )
    if project["created_by"] != user["id"]:
        return None, HTMLResponse(
            '<div class="blocked">Only the project creator may edit it.</div>',
            status_code=403,
        )
    return project, None


@router.get("/aegis/projects/{project_id}/edit", response_class=HTMLResponse)
def project_edit_form(
    request: Request, project_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """The prefilled edit form for a project. Creator-only, like the save below:
    a logged-out user gets 401, a non-creator 403, a missing project 404. The
    Delete control lives here too, disabled with an explanation while the project
    still owns issues (the API would refuse that delete with a 409)."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit projects.</div>',
            status_code=401,
        )
    project, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    return _templates.TemplateResponse(
        request=request,
        name="aegis/project_edit.html",
        context={
            "project": project,
            "issue_count": issues.count_issues_in_project(conn, project_id),
        },
    )


@router.post("/aegis/projects/{project_id}/edit", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def project_edit_save(
    request: Request,
    project_id: int,
    name: str = Form(""),
    key: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save an edit to a project's name/key/description. Creator-only (401/403/404),
    empty name → 400, bad key → 400, a name OR key that collides with ANOTHER
    project → 409; otherwise 303 back to the projects list."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit projects.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Project name is required.</div>', status_code=400)
    normalized_key = projects.normalize_key(key)
    if normalized_key is None:
        return HTMLResponse(
            '<div class="error">Key must start with a letter and be 1–10 letters/digits.</div>',
            status_code=400,
        )
    clash = projects.get_project_by_name(conn, name)
    if clash is not None and clash["id"] != project_id:
        return HTMLResponse('<div class="error">A project with that name already exists.</div>', status_code=409)
    key_clash = projects.get_project_by_key(conn, normalized_key)
    if key_clash is not None and key_clash["id"] != project_id:
        return HTMLResponse('<div class="error">That project key is already in use.</div>', status_code=409)
    projects.update_project(
        conn, project_id, name=name, key=normalized_key, description=description.strip()
    )
    return RedirectResponse("/aegis/projects", status_code=303)


@router.post("/aegis/projects/{project_id}/delete", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def project_delete(
    request: Request, project_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Delete a project from the edit page. Creator-only (401/403/404). Refused
    with 409 if the project still owns issues — we don't cascade or detach, so the
    issues must be reassigned/deleted first (same rule as the REST API)."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to delete projects.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    if issues.count_issues_in_project(conn, project_id) > 0:
        return HTMLResponse(
            '<div class="error">Reassign or delete this project\'s issues first.</div>',
            status_code=409,
        )
    projects.delete_project(conn, project_id)
    return RedirectResponse("/aegis/projects", status_code=303)
