"""HTML page routes (APIRouter).

The actual page rendering logic lives here. The Jinja2Templates instance
is configured in main.py (per wiring contract) and injected via init_templates.
"""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from athena import config
from athena.aegis import (
    comments,
    dependencies,
    issue_activity,
    issues,
    labels,
    projects,
    saved_filters,
    statuses,
)
from athena.core import activity, attachments, identity, links, notifications, search, users
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.render import render_body, render_comment, render_snippet

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


def _readonly_response() -> HTMLResponse:
    return HTMLResponse(
        '<div class="blocked">Viewer role is read-only.</div>',
        status_code=403,
    )


def _issues_url(
    *,
    status: str = "",
    label: str = "",
    project: str = "",
    search: str = "",
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    per_page: int = 20,
) -> str:
    """Build an encoded issue-list URL for HTMX links and pagination."""
    query = urlencode(
        {
            "status": status,
            "label": label,
            "project": project,
            "search": search,
            "sort": sort,
            "order": order,
            "page": page,
            "per_page": per_page,
        }
    )
    return f"/aegis/issues?{query}"


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


_SEARCH_PAGE = 20


@router.get("/find", response_class=HTMLResponse)
def find(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Human-facing search across issues and pages — the browser twin of the JSON
    API at /search (which serves the fleet). It runs the SAME core.search query, so
    the two never disagree on what matches or how it ranks; this route only adds the
    presentation: a scope filter (All/Issues/Pages), a result card per hit linking to
    its detail page with a highlighted snippet and a little context (issue key+status,
    page space), and Prev/Next paging. Reading is open, like every other web read; a
    blank box just shows the form."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    q = (request.query_params.get("q") or "").strip()
    # Scope to one kind, or all. An unrecognised value (e.g. a hand-edited URL) falls
    # back to "all" rather than erroring — the same forgiving rule the issue list uses
    # for its sort/order params.
    kind_raw = (request.query_params.get("kind") or "").strip().lower()
    kind = kind_raw if kind_raw in ("issue", "page") else None
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    # Fetch one extra hit to know whether a next page exists without a count query —
    # the same trick the activity feed uses. Trim it off before rendering.
    hits = (
        search.search(
            conn, q, kind=kind, limit=_SEARCH_PAGE + 1, offset=(page - 1) * _SEARCH_PAGE
        )
        if q
        else []
    )
    has_next = len(hits) > _SEARCH_PAGE
    hits = hits[:_SEARCH_PAGE]
    for h in hits:
        # render_snippet escapes then turns search's [..] match markers into <mark>;
        # the href maps a hit's kind to where it lives in the web UI.
        h["snippet_html"] = render_snippet(h.get("snippet"))
        h["href"] = (
            f"/aegis/issues/{h['source_id']}" if h["kind"] == "issue"
            else f"/mentor/pages/{h['source_id']}"
        )

    def find_url(*, scope: str | None, page_num: int) -> str:
        return "/find?" + urlencode(
            {"q": q, "kind": scope or "", "page": page_num}
        )

    return _templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "q": q,
            "hits": hits,
            "kind": kind,
            "page": page,
            "has_next": has_next,
            "scope_urls": {
                "all": find_url(scope=None, page_num=1),
                "issue": find_url(scope="issue", page_num=1),
                "page": find_url(scope="page", page_num=1),
            },
            "prev_url": find_url(scope=kind, page_num=page - 1) if page > 1 else None,
            "next_url": find_url(scope=kind, page_num=page + 1) if has_next else None,
        },
    )


@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The signed-in user's notification inbox — what changed on things they watch."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to see your inbox.</div>',
            status_code=401,
        )
    items = notifications.list_notifications(conn, user["id"], limit=100)
    for it in items:
        it["href"] = (
            f"/aegis/issues/{it['target_id']}"
            if it["target_kind"] == "issue"
            else f"/mentor/pages/{it['target_id']}"
        )
    return _templates.TemplateResponse(
        request=request, name="inbox.html", context={"items": items}
    )


@router.post("/inbox/{notification_id}/read", dependencies=[Depends(verify_csrf)])
def mark_inbox_read(
    request: Request, notification_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Mark one notification read, then back to the inbox."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    notifications.mark_read(conn, user["id"], notification_id)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/read-all", dependencies=[Depends(verify_csrf)])
def mark_inbox_all_read(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """Mark every notification read, then back to the inbox."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    notifications.mark_all_read(conn, user["id"])
    return RedirectResponse("/inbox", status_code=303)


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

    user = getattr(request.state, "user", None)
    can_write = user is not None and identity.can_write(user)
    return _templates.TemplateResponse(
        request=request,
        name="aegis.html",
        context={
            "status_counts": dict(status_counts),
            "recent_issues": recent_issues,
            "total_issues": len(all_issues),
            "can_write": can_write,
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
    # "none" selects the backlog (issues with no project); a number selects that
    # project; anything else is "all projects". The exact same parser the API
    # uses (issues.parse_project_filter) so the dropdown value can never mean one
    # thing here and another there. A garbled value is rejected (400), not
    # silently widened to "all" — the web mirror of the API's 422.
    parsed = issues.parse_project_filter(project_raw)
    if parsed is None:
        return HTMLResponse("<h1>Invalid project filter</h1>", status_code=400)
    project_id, backlog = parsed
    # Do NOT pre-lower the needle: SQLite LIKE is already case-insensitive for
    # ASCII, and lowering here would diverge from the API (which passes the raw
    # search straight to list_issues) on non-ASCII text. Let LIKE own casing.
    search = (request.query_params.get("search") or "").strip()
    # Sort/order are presentation concerns the web layer owns, but only over a
    # whitelist: an unknown column falls back to created_at rather than KeyError-ing
    # or letting a caller sort by an arbitrary attribute.
    sort = request.query_params.get("sort", "created_at")
    if sort not in {"id", "title", "status", "priority", "created_at"}:
        sort = "created_at"
    order = request.query_params.get("order", "desc")
    if order not in {"asc", "desc"}:
        order = "desc"

    # Filtering goes through the shared data-layer path (same one the API uses),
    # so the list and the API never disagree on what matches. The web layer then
    # only does the presentation concerns — sort + pagination — on the result.
    ids = labels.issue_ids_for_label(conn, label_filter) if label_filter else None
    filtered = issues.list_issues(
        conn,
        status=status_filter,
        search=search,
        project_id=project_id,
        backlog=backlog,
        ids=ids,
    )
    _attach_labels(conn, filtered)  # one bulk query; paged slice carries its chips

    # Sort in web layer (presentation concern) – safe since we don't own data.
    # sort is whitelisted above, so x.get(sort) is a real column; coalesce to ""
    # so a NULL (e.g. an unset priority) sorts as empty rather than raising on the
    # str/None comparison Python 3 forbids.
    reverse = order == "desc"
    filtered = sorted(filtered, key=lambda x: x.get(sort) or "", reverse=reverse)

    # Simple pagination (server-side slice). A non-numeric page/per_page in the
    # query string is the only failure here; fall back to the defaults for that.
    try:
        page = max(1, int(request.query_params.get("page", 1)))
        per_page = max(5, min(50, int(request.query_params.get("per_page", 20))))
    except (TypeError, ValueError):
        page, per_page = 1, 20

    total = len(filtered)
    start = (page - 1) * per_page
    paged = filtered[start : start + per_page]  # labels already attached

    def page_url(page_num: int, *, sort_by: str = sort, order_by: str = order) -> str:
        return _issues_url(
            status=status_filter or "",
            label=label_filter,
            project=project_raw,
            search=search,
            sort=sort_by,
            order=order_by,
            page=page_num,
            per_page=per_page,
        )

    sort_urls = {
        column: page_url(
            1,
            sort_by=column,
            order_by="asc" if sort == column and order == "desc" else "desc",
        )
        for column in ("id", "title", "status", "priority", "created_at")
    }

    user = getattr(request.state, "user", None)
    can_write = user is not None and identity.can_write(user)

    # Pre-fill link for "Save current view" — carries the active filters to the
    # saved-filters create form so an ad-hoc search becomes a named filter in one
    # click. Only the dimensions a saved filter persists are passed along.
    save_filter_url = "/aegis/filters?" + urlencode(
        {
            "status": status_filter or "",
            "label": label_filter,
            "project": project_raw,
            "search": search,
        }
    )

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
            "sort_urls": sort_urls,
            "prev_page_url": page_url(page - 1),
            "next_page_url": page_url(page + 1),
            "can_write": can_write,
            "save_filter_url": save_filter_url,
        },
    )


_FEED_PAGE = 50
_EXPORT_LIMIT = 1000


def _int_or_none(raw: str | None) -> int | None:
    """A query param parsed to int, or None if absent/blank/garbage — a filter the
    user can't set to a bad value just by editing the URL."""
    if raw is None or raw.strip() == "" or not raw.strip().lstrip("-").isdigit():
        return None
    return int(raw)


def _activity_export_url(
    *,
    actor_id: int | None,
    verb: str | None,
    kind: str | None,
    target_id: int | None,
    q: str,
) -> str:
    query = {
        "actor": actor_id or "",
        "verb": verb or "",
        "kind": kind or "",
        "target": target_id or "",
        "q": q,
    }
    return f"/aegis/activity.csv?{urlencode(query)}"


@router.get("/aegis/activity", response_class=HTMLResponse)
def activity_feed(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The global activity timeline — the browser twin of GET /activity. Reading
    is open, like every other web read (the JSON API gates the feed for the fleet;
    the human view follows the rest of the site's open-read convention). It runs
    the SAME data-layer query the API serves, so the two never disagree.

    Supports the same filters (actor / verb / target kind) and a cursor (?before=)
    for paging back through history one page at a time."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    actor_id = _int_or_none(request.query_params.get("actor"))
    before_id = _int_or_none(request.query_params.get("before"))
    target_id = _int_or_none(request.query_params.get("target"))
    verb = (request.query_params.get("verb") or "").strip() or None
    kind = (request.query_params.get("kind") or "").strip() or None
    q = (request.query_params.get("q") or "").strip()
    if target_id is not None and kind is None:
        return HTMLResponse("<h1>Invalid activity target filter</h1>", status_code=400)

    # Fetch one extra row to know whether an older page exists without a count
    # query; if we got it, there's more — trim it off and remember the cursor.
    rows = activity.list_activity(
        conn,
        target_kind=kind,
        target_id=target_id,
        actor_id=actor_id,
        verb=verb,
        search=q,
        before_id=before_id,
        limit=_FEED_PAGE + 1,
    )
    has_more = len(rows) > _FEED_PAGE
    events = rows[:_FEED_PAGE]
    next_before = events[-1]["id"] if has_more and events else None

    return _templates.TemplateResponse(
        request=request,
        name="aegis/activity.html",
        context={
            "events": events,
            "all_users": users.list_users(conn),
            "all_verbs": activity.distinct_verbs(conn),
            "all_kinds": activity.distinct_target_kinds(conn),
            "f_actor": actor_id,
            "f_verb": verb,
            "f_kind": kind,
            "f_target": target_id,
            "f_q": q,
            "next_before": next_before,
            "export_url": _activity_export_url(
                actor_id=actor_id,
                verb=verb,
                kind=kind,
                target_id=target_id,
                q=q,
            ),
        },
    )


@router.get("/aegis/activity.csv")
def activity_export_csv(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Download the current activity filters as a CSV audit export."""
    actor_id = _int_or_none(request.query_params.get("actor"))
    target_id = _int_or_none(request.query_params.get("target"))
    verb = (request.query_params.get("verb") or "").strip() or None
    kind = (request.query_params.get("kind") or "").strip() or None
    q = (request.query_params.get("q") or "").strip()
    if target_id is not None and kind is None:
        return HTMLResponse("<h1>Invalid activity target filter</h1>", status_code=400)

    rows = activity.list_activity(
        conn,
        target_kind=kind,
        target_id=target_id,
        actor_id=actor_id,
        verb=verb,
        search=q,
        limit=_EXPORT_LIMIT,
    )
    return Response(
        activity.to_csv(rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="athena-activity.csv"'
        },
    )


@router.get("/aegis/issues/new", response_class=HTMLResponse)
def new_issue_form(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Render the new issue creation form."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is not None and not identity.can_write(user):
        return _readonly_response()
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
    priority: str = Form("medium"),
    project_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create an issue as the logged-in user. The actor is the browser session
    (request.state.user), never a field in the form — same rule as the REST API.
    Logged-out callers get a prompt to sign in instead of a write. A new issue
    starts at its project's first status (statuses are per-project now, and the
    project is chosen on this same form), so the form doesn't pick a status."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to create issues.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return _readonly_response()

    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Title is required.</div>', status_code=400)
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
    # New issues start at the chosen project's first status (the backlog uses the
    # default set).
    status = statuses.first_status(conn, project)

    issue = issues.create_issue(
        conn, title=title, body=body, status=status, priority=priority,
        project_id=project, created_by=user["id"],
    )
    # Record onto the audit trail — same fact the REST create records, so a
    # browser-created issue and an API-created one read identically in the feed.
    issue_activity.record_created(
        conn, actor_id=user["id"], issue_id=issue["id"], body=issue["body"]
    )
    # HTMX follows HX-Redirect after the successful create without an inline script.
    return HTMLResponse(
        f'<div class="success">Created issue #{issue["id"]}.</div>',
        headers={"HX-Redirect": f'/aegis/issues/{issue["id"]}'},
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
    if not identity.can_write(user):
        return None, _readonly_response()
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
    before, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Title is required.</div>', status_code=400)

    updated = issues.update_issue(conn, issue_id, title=title, body=body.strip())
    # Record the content edit (helper no-ops if title+body are unchanged),
    # attributed to the session user — the browser path leaves the same trail
    # as the API.
    issue_activity.record_edited(
        conn, actor_id=user["id"], issue_id=issue_id, before=before, after=updated
    )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/status", dependencies=[Depends(verify_csrf)])
def change_issue_status(
    request: Request,
    issue_id: int,
    status: str = Form(...),
    confirm: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Move an issue to a new status from the detail page. Gated on the session
    user (same actor rule as create), validates against the lifecycle, then
    303-redirects back to the issue so the page reloads with the new state.

    Closing (status -> done) an issue that still has OPEN blockers re-renders the
    page with an advisory warning instead of applying the change, unless the form
    carries confirm=1 ("Mark done anyway"). The warning is a nudge, not a lock —
    dependencies in this product are advisory — so a second submit goes through."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to change status.</div>',
            status_code=401,
        )
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    if not statuses.is_valid(conn, issue["project_id"], status):
        return HTMLResponse('<div class="error">Unknown status.</div>', status_code=400)

    if statuses.is_done(conn, issue["project_id"], status) and not confirm.strip():
        blockers = dependencies.open_blockers(conn, issue_id)
        if blockers:
            # Don't apply the close — show the warning and let the user confirm.
            return _render_issue_detail(
                request, conn, issue, extra={"blocked_warning": blockers}
            )

    issues.update_status(conn, issue_id, status)
    # Record the transition (helper no-ops if it didn't actually move), attributed
    # to the session user — the browser path now leaves the same trail as the API.
    issue_activity.record_status_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["status"],
        after=status,
    )
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
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    if priority not in issues.PRIORITIES:
        return HTMLResponse('<div class="error">Unknown priority.</div>', status_code=400)

    issues.update_issue(conn, issue_id, priority=priority)
    issue_activity.record_priority_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["priority"],
        after=priority,
    )
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
        # not-found state: render empty list page with error (minimal). Carry a
        # real 404 status — a missing issue is not a 200, and the API surface for
        # the same id returns 404, so the browser path must not disagree.
        return _templates.TemplateResponse(
            request=request,
            name="aegis/issues.html",
            context={"issues": [], "status_filter": "", "search": "", "error": f"Issue {ref} not found"},
            status_code=404,
        )

    return _render_issue_detail(request, conn, issue)


def _render_issue_detail(
    request: Request,
    conn: sqlite3.Connection,
    issue: dict,
    *,
    extra: dict | None = None,
    status_code: int = 200,
):
    """Assemble the issue-detail page. One place builds the context so the normal
    view and the warn-on-close re-render can never drift on what the page needs.
    `extra` overlays warning state (e.g. the open blockers banner) without the
    caller re-listing every base key."""
    issue_id = issue["id"]
    user = getattr(request.state, "user", None)
    can_write = user is not None and identity.can_write(user)
    can_modify = can_write and issues.can_modify(issue, user["id"])
    comment_rows = comments.list_comments(conn, issue_id)
    for comment in comment_rows:
        comment["body_html"] = render_comment(conn, comment["body"])
    children = issues.list_children(conn, issue_id)

    context = {
        "issue": issue,
        "body_html": render_body(conn, issue["body"]),
        "backlinks": links.backlinks(conn, "issue", issue_id),
        "links": dependencies.list_links(conn, issue_id),
        "comments": comment_rows,
        "attachments": attachments.list_for(conn, "issue", issue_id),
        "is_watching": user is not None
        and notifications.is_watching(conn, user["id"], "issue", issue_id),
        "users": users.list_users(conn),
        "issue_labels": labels.labels_for_issue(conn, issue_id),
        "all_labels": labels.list_labels(conn),
        "all_projects": projects.list_projects(conn),
        "issue_statuses": statuses.list_statuses(conn, issue["project_id"]),
        "parent": issues.get_issue(conn, issue["parent_id"]) if issue.get("parent_id") else None,
        "children": children,
        "children_done": sum(
            1 for c in children if statuses.is_done(conn, c["project_id"], c["status"])
        ),
        "can_modify": can_modify,
        "can_write": can_write,
        # This issue's own audit trail (newest first) — the same data-layer read
        # the REST feed serves, scoped to this target.
        "activity": activity.list_activity(
            conn, target_kind="issue", target_id=issue_id
        ),
    }
    if extra:
        context.update(extra)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_detail.html",
        context=context,
        status_code=status_code,
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
    issue, err = _authorize_issue_write(conn, issue_id, user)
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
    # Record the assignment change (helper no-ops if unchanged), attributed to the
    # session user — same trail the REST assignee endpoint leaves.
    issue_activity.record_assignee_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["assignee_id"],
        after=target,
    )
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
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

    project_id = project_id.strip()
    if project_id == "":
        target: int | None = None
    else:
        if not project_id.isdigit() or projects.get_project(conn, int(project_id)) is None:
            return HTMLResponse('<div class="error">No such project.</div>', status_code=400)
        target = int(project_id)

    updated = issues.set_project(conn, issue_id, target)
    issue_activity.record_project_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["project_id"],
        after=updated["project_id"],
    )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/parent", dependencies=[Depends(verify_csrf)])
def change_issue_parent(
    request: Request,
    issue_id: int,
    parent_ref: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Nest an issue under a parent (by id or key), or clear it (empty value). Same
    write gate as status/labels. Self/cycle/unknown parents are rejected with a 400."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to set a parent.</div>',
            status_code=401,
        )
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    parent_ref = parent_ref.strip()
    if parent_ref == "":
        parent_id: int | None = None
    else:
        parent = issues.get_by_ref(conn, parent_ref)
        if parent is None:
            return HTMLResponse('<div class="error">No such parent issue.</div>', status_code=400)
        parent_id = parent["id"]
    reason = issues.validate_parent(conn, issue_id, parent_id)
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}</div>', status_code=400)
    issues.set_parent(conn, issue_id, parent_id)
    issue_activity.record_parent_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["parent_id"],
        after=parent_id,
    )
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
    if labels.add_label_to_issue(conn, issue_id, label["id"]):  # idempotent
        issue_activity.record_label_added(
            conn, actor_id=user["id"], issue_id=issue_id, label_id=label["id"]
        )
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
    if labels.remove_label_from_issue(conn, issue_id, label_id):
        issue_activity.record_label_removed(
            conn, actor_id=user["id"], issue_id=issue_id, label_id=label_id
        )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/links", dependencies=[Depends(verify_csrf)])
def add_issue_link(
    request: Request,
    issue_id: int,
    target_ref: str = Form(""),
    relation: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Declare a relationship from this issue to another (addressed by id or key).
    Same write gate as labels/status. The other issue is resolved from its ref
    (400 if unknown); add_link enforces shape (self-ref, contradiction) and
    returns a reason we surface as a 400."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to link issues.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    target = issues.get_by_ref(conn, target_ref.strip())
    if target is None:
        return HTMLResponse('<div class="error">No such target issue.</div>', status_code=400)
    reason = dependencies.add_link(
        conn,
        from_id=issue_id,
        to_id=target["id"],
        relation=relation,
        created_by=user["id"],
    )
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}</div>', status_code=400)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/links/{relation}/{target_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_issue_link(
    request: Request,
    issue_id: int,
    relation: str,
    target_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Remove a relationship. Same write gate. POST (not DELETE) because HTML forms
    can't issue DELETE. relation is the user-facing form used to create it."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to link issues.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    dependencies.remove_link(conn, from_id=issue_id, to_id=target_id, relation=relation)
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
    if not identity.can_write(user):
        return _readonly_response()
    if issues.get_issue(conn, issue_id) is None:
        return HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    body = body.strip()
    if not body:
        return HTMLResponse('<div class="error">Comment cannot be empty.</div>', status_code=400)

    comments.add_comment(conn, issue_id=issue_id, author_id=user["id"], body=body)
    issue_activity.record_commented(
        conn, actor_id=user["id"], issue_id=issue_id, body=body
    )
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
    if not identity.can_write(user):
        return _readonly_response()
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
    if not identity.can_write(user):
        return _readonly_response()
    _, err = _own_comment_or_response(conn, issue_id, comment_id, user)
    if err is not None:
        return err
    comments.delete_comment(conn, comment_id)
    issue_activity.record_comment_deleted(conn, actor_id=user["id"], issue_id=issue_id)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/attachments", dependencies=[Depends(verify_csrf)])
def add_issue_attachment(
    request: Request,
    issue_id: int,
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Attach a file to an issue from the detail page. Same write gate as comments.
    Empty file → 400, oversize → 413; otherwise 303 back to the issue."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to attach files.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return _readonly_response()
    if issues.get_issue(conn, issue_id) is None:
        return HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    data = file.file.read()
    if not data:
        return HTMLResponse('<div class="error">File is empty.</div>', status_code=400)
    if len(data) > config.ATTACH_MAX_BYTES:
        return HTMLResponse('<div class="error">File is too large.</div>', status_code=413)
    att = attachments.store(
        conn,
        target_kind="issue",
        target_id=issue_id,
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        uploaded_by=user["id"],
        attach_dir=config.ATTACH_DIR,
    )
    activity.record(
        conn,
        actor_id=user["id"],
        verb="added_attachment",
        target_kind="issue",
        target_id=issue_id,
        detail=att["filename"],
    )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/attachments/{attachment_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_issue_attachment(
    request: Request,
    issue_id: int,
    attachment_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delete an attachment from the issue detail page. Uploader-only (mirrors
    comment ownership). POST, not DELETE, because HTML forms can't issue DELETE."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to remove files.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return _readonly_response()
    att = attachments.get(conn, attachment_id)
    if att is None or att["target_kind"] != "issue" or att["target_id"] != issue_id:
        return HTMLResponse('<div class="error">Attachment not found.</div>', status_code=404)
    if att["uploaded_by"] != user["id"]:
        return HTMLResponse(
            '<div class="error">Only the uploader may remove this file.</div>',
            status_code=403,
        )
    attachments.delete(conn, attachment_id, config.ATTACH_DIR)
    activity.record(
        conn,
        actor_id=user["id"],
        verb="removed_attachment",
        target_kind="issue",
        target_id=issue_id,
        detail=att["filename"],
    )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/watch", dependencies=[Depends(verify_csrf)])
def watch_issue(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Start watching an issue (any signed-in user, including viewers — it's a
    personal subscription, not a write to shared state)."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to watch.</div>',
            status_code=401,
        )
    notifications.watch(conn, user["id"], "issue", issue_id)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/unwatch", dependencies=[Depends(verify_csrf)])
def unwatch_issue(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Stop watching an issue."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    notifications.unwatch(conn, user["id"], "issue", issue_id)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


# --- Saved filters: a user's named, reusable issue queries ------------------


def _describe_criteria(conn, criteria: dict) -> str:
    """A short human description of a filter's criteria for the list/detail views,
    resolving ids to names (assignee → display name, project id → project name).
    Empty criteria reads as "all issues" — an unconstrained filter is still a view."""
    parts: list[str] = []
    if criteria.get("status"):
        parts.append(f"status: {criteria['status']}")
    if criteria.get("priority"):
        parts.append(f"priority: {criteria['priority']}")
    if criteria.get("assignee_id") is not None:
        user = users.get_user(conn, criteria["assignee_id"])
        parts.append(f"assignee: {user['name'] if user else '#' + str(criteria['assignee_id'])}")
    if criteria.get("label"):
        parts.append(f"label: {criteria['label']}")
    if criteria.get("project"):
        value = criteria["project"]
        if value.lower() == "none":
            parts.append("project: backlog")
        elif value.isdigit():
            project = projects.get_project(conn, int(value))
            parts.append(f"project: {project['name'] if project else '#' + value}")
        else:
            parts.append(f"project: {value}")
    if criteria.get("search"):
        parts.append(f"search: “{criteria['search']}”")
    return ", ".join(parts) if parts else "all issues"


@router.get("/aegis/filters", response_class=HTMLResponse)
def filters_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The signed-in user's saved filters, plus a form to add one. Filters are
    PERSONAL (each user sees only their own), the same scoping as the inbox, so this
    view requires a session. The create form can be pre-filled from query params
    (the "Save current view" link on the issues list passes the active filters),
    so building a saved filter from an ad-hoc search is one click."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to save filters.</div>',
            status_code=401,
        )
    my_filters = saved_filters.list_filters(conn, user["id"])
    for flt in my_filters:
        flt["summary"] = _describe_criteria(conn, flt["criteria"])
    # Pre-fill values for the create form, taken from the query string (all blank by
    # default). project/label/assignee dropdowns echo the chosen option as selected.
    prefill = {
        "name": (request.query_params.get("name") or "").strip(),
        "status": (request.query_params.get("status") or "").strip(),
        "priority": (request.query_params.get("priority") or "").strip(),
        "assignee_id": (request.query_params.get("assignee_id") or "").strip(),
        "label": (request.query_params.get("label") or "").strip(),
        "project": (request.query_params.get("project") or "").strip(),
        "search": (request.query_params.get("search") or "").strip(),
    }
    return _templates.TemplateResponse(
        request=request,
        name="aegis/filters.html",
        context={
            "filters": my_filters,
            "prefill": prefill,
            "priorities": issues.PRIORITIES,
            "all_labels": labels.list_labels(conn),
            "all_projects": projects.list_projects(conn),
            "all_users": users.list_users(conn),
        },
    )


@router.post("/aegis/filters", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def create_filter(
    request: Request,
    name: str = Form(""),
    status: str = Form(""),
    priority: str = Form(""),
    assignee_id: str = Form(""),
    label: str = Form(""),
    project: str = Form(""),
    search: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save a filter for the logged-in user. The owner is the session, never a form
    field — the same rule the REST API enforces. Validation goes through the SAME
    normalize/validate path the API uses, so the two surfaces reject the same input.
    Empty name → 400, bad priority/project → 400, duplicate name → 409; otherwise
    303 back to the filters list."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to save filters.</div>',
            status_code=401,
        )
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Filter name is required.</div>', status_code=400)
    criteria = saved_filters.normalize_criteria(
        {
            "status": status,
            "priority": priority,
            "assignee_id": assignee_id,
            "label": label,
            "project": project,
            "search": search,
        }
    )
    reason = saved_filters.validate_criteria(criteria)
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}.</div>', status_code=400)
    try:
        saved_filters.create_filter(conn, owner_id=user["id"], name=name, criteria=criteria)
    except sqlite3.IntegrityError:
        return HTMLResponse(
            '<div class="error">You already have a filter with that name.</div>',
            status_code=409,
        )
    return RedirectResponse("/aegis/filters", status_code=303)


@router.get("/aegis/filters/{filter_id}", response_class=HTMLResponse)
def filter_detail(
    request: Request, filter_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Run one saved filter and show the matching issues. Owner-only — a filter that
    isn't yours reads as 404, never revealing it exists. Runs through run_filter (the
    same path the REST /filters/{id}/issues endpoint uses), so the browser and the
    API can't disagree on what a saved filter returns."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to run filters.</div>',
            status_code=401,
        )
    flt = saved_filters.get_filter(conn, filter_id)
    if flt is None or flt["owner_id"] != user["id"]:
        return HTMLResponse('<div class="error">No such filter.</div>', status_code=404)
    matches = saved_filters.run_filter(conn, flt["criteria"])
    _attach_labels(conn, matches)
    flt["summary"] = _describe_criteria(conn, flt["criteria"])
    return _templates.TemplateResponse(
        request=request,
        name="aegis/filter_detail.html",
        context={"filter": flt, "issues": matches, "total": len(matches)},
    )


@router.post(
    "/aegis/filters/{filter_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def delete_filter(
    request: Request, filter_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Delete one of the logged-in user's filters, then back to the list. Owner-only
    (404 if it isn't theirs), so a user can't delete another's saved query."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    flt = saved_filters.get_filter(conn, filter_id)
    if flt is None or flt["owner_id"] != user["id"]:
        return HTMLResponse('<div class="error">No such filter.</div>', status_code=404)
    saved_filters.delete_filter(conn, filter_id)
    return RedirectResponse("/aegis/filters", status_code=303)


@router.get("/aegis/boards", response_class=HTMLResponse)
def boards(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Boards view (Aegis) using real list_issues with search/filter."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    # Same data-layer path as the issues list and the API: filtering (status +
    # search) is done by list_issues, NOT re-implemented in Python here. The old
    # code re-filtered a full table scan with its own pre-lowered substring match,
    # which both diverged from the API's casing and duplicated the filter logic.
    search = (request.query_params.get("search") or "").strip()
    status_filter = request.query_params.get("status")
    filtered = issues.list_issues(conn, status=status_filter, search=search)
    _attach_labels(conn, filtered)

    # Statuses are per-project now, so the board's columns are dynamic: the union of
    # statuses present, ordered by category (todo → doing → done) then name, so a
    # board that mixes projects with different status sets still reads coherently.
    from collections import defaultdict

    grouped: dict[str, list] = defaultdict(list)
    for issue in filtered:
        grouped[issue.get("status", "")].append(issue)

    cat_rank = {"todo": 0, "doing": 1, "done": 2}

    def _sort_key(name: str) -> tuple[int, str]:
        return (cat_rank.get(statuses.global_category(conn, name), 1), name)

    board_columns = [
        {"name": name, "label": name.replace("_", " ").title(), "issues": grouped[name]}
        for name in sorted(grouped, key=_sort_key)
    ]
    # The status filter offers every status currently in use (across all issues).
    all_statuses = sorted(
        {i["status"] for i in issues.list_issues(conn)}, key=_sort_key
    )

    template = "aegis/partials/boards_content.html" if request.headers.get("HX-Request") else "aegis/boards.html"
    return _templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "board_columns": board_columns,
            "all_statuses": all_statuses,
            "search": search,
            "status_filter": status_filter or "",
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
    user = getattr(request.state, "user", None)
    can_write = user is not None and identity.can_write(user)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/projects.html",
        context={"projects": all_projects, "counts": counts, "can_write": can_write},
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
    if not identity.can_write(user):
        return _readonly_response()
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
    if not identity.can_write(user):
        return None, _readonly_response()
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
    proj_statuses = statuses.list_statuses(conn, project_id)
    # How many issues use each status, so the template can disable removing one
    # that's still in use (the API/data layer refuses it too).
    status_usage = {
        s["name"]: len(
            issues.list_issues(conn, project_id=project_id, status=s["name"])
        )
        for s in proj_statuses
    }
    return _templates.TemplateResponse(
        request=request,
        name="aegis/project_edit.html",
        context={
            "project": project,
            "issue_count": issues.count_issues_in_project(conn, project_id),
            "statuses": proj_statuses,
            "status_usage": status_usage,
            "status_categories": statuses.CATEGORIES,
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


@router.post("/aegis/projects/{project_id}/statuses", dependencies=[Depends(verify_csrf)])
def add_project_status_web(
    request: Request,
    project_id: int,
    name: str = Form(""),
    category: str = Form("todo"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Add a status to a project from its edit page. Creator-only, like editing the
    project. The data layer rejects duplicates/bad categories; we surface the reason."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    reason = statuses.add_status(conn, project_id, name, category)
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}</div>', status_code=400)
    return RedirectResponse(f"/aegis/projects/{project_id}/edit", status_code=303)


@router.post(
    "/aegis/projects/{project_id}/statuses/{name}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_project_status_web(
    request: Request,
    project_id: int,
    name: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Remove a status from a project. Creator-only. Refused (with a reason) if it's
    the last status or still in use by issues."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    reason = statuses.remove_status(conn, project_id, name)
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}</div>', status_code=400)
    return RedirectResponse(f"/aegis/projects/{project_id}/edit", status_code=303)
