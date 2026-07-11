"""HTML page routes (APIRouter).

The actual page rendering logic lives here. The Jinja2Templates instance
is configured in main.py (per wiring contract) and injected via init_templates.
"""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from athena import config
from athena.aegis import (
    comments,
    contributors,
    dashboard,
    dependencies,
    issue_activity,
    issue_history,
    issue_search,
    issues,
    projects,
    sprints,
    statuses,
)
from athena.core import (
    access,
    activity,
    attachments,
    identity,
    labels,
    links,
    notifications,
    search,
    users,
)
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
    priority: str = "",
    assignee: str = "",
    label: str = "",
    project: str = "",
    sprint: str = "",
    search: str = "",
    archived: str = "",
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    per_page: int = 20,
) -> str:
    """Build an encoded issue-list URL for HTMX links and pagination."""
    query = urlencode(
        {
            "status": status,
            "priority": priority,
            "assignee": assignee,
            "label": label,
            "project": project,
            "sprint": sprint,
            "search": search,
            "archived": archived,
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


def _statuses_in_use(conn, visible_project_ids: set[int] | None = None) -> list[str]:
    """The distinct statuses currently on any issue the viewer MAY SEE, ordered todo →
    doing → done then name. This is the option set BOTH status filters (the issue list
    and the board) offer, so a filter only ever lists statuses that really exist on
    visible issues — including a project's custom statuses — instead of a hardcoded
    open/in_progress/done trio. (Empty when there are none; the "All statuses" option
    always remains, so the control still renders.)

    visible_project_ids is what the caller gets from access.visible_project_filter: None
    means no gating (an admin's god view), a set restricts to those projects (plus the
    backlog). Without it a private project's CUSTOM status name would leak into the
    dropdown for someone who can't see that project (the rows are gated, but the option
    set was not)."""
    cat_rank = {"todo": 0, "doing": 1, "done": 2}
    names = {
        i["status"]
        for i in issues.list_issues(conn, visible_project_ids=visible_project_ids)
    }
    return sorted(
        names, key=lambda n: (cat_rank.get(statuses.global_category(conn, n), 1), n)
    )


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


@router.get("/aegis/dashboard", response_class=HTMLResponse)
def aegis_dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """A live overview of the work: headline totals, the issue distribution by status
    and priority, per-project and backlog counts, what's in flight (active sprints),
    the signed-in user's open plate, and the latest activity. Read-only — every
    number comes from aegis.dashboard (the one home for the counting SQL), so this
    route only lays them out. Open to read, like the issue list."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    # Every number is counted only over the projects this viewer may see (admins all;
    # the backlog is always in). recent_activity is gated the same way: actor=user
    # makes list_activity drop events whose target the viewer can't see.
    vis = access.visible_project_filter(conn, user)
    return _templates.TemplateResponse(
        request=request,
        name="aegis/dashboard.html",
        context={
            "totals": dashboard.totals(conn, vis),
            "status_counts": dashboard.status_counts(conn, vis),
            "priority_counts": dashboard.priority_counts(conn, vis),
            "projects": dashboard.project_open_counts(conn, vis),
            "backlog_count": dashboard.backlog_count(conn),
            "active_sprints": dashboard.active_sprints(conn, vis),
            # The user's own plate only makes sense when we know who they are.
            "my_issues": dashboard.my_open_issues(conn, user["id"], visible_project_ids=vis) if user else [],
            "recent_activity": activity.list_activity(conn, limit=10, actor=user),
        },
    )


_SEARCH_PAGE = 20


@router.get("/find", response_class=HTMLResponse)
def find(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Human-facing search across issues and pages — the browser twin of the JSON
    search APIs. It runs the SAME queries (core.search for the cross-kind case,
    aegis.issue_search for the filtered case), so the page never disagrees with the
    API on what matches or how it ranks; this route only adds presentation: a scope
    filter (All/Issues/Pages), structured issue filters (status/label/project), a
    result card per hit with a highlighted snippet and context, and Prev/Next paging.

    Filters are issue-only (a page has no status/label/project), so setting any of
    them switches into FILTERED ISSUE-SEARCH mode: results are ranked issues that also
    satisfy the filters, and pages drop out. With no filter set it is the plain
    cross-kind search, honouring the scope tab. Reading is open, like every other web
    read; a blank box just shows the form."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    q = (request.query_params.get("q") or "").strip()
    # Scope to one kind, or all. An unrecognised value (e.g. a hand-edited URL) falls
    # back to "all" rather than erroring — the same forgiving rule the issue list uses
    # for its sort/order params.
    kind_raw = (request.query_params.get("kind") or "").strip().lower()
    kind = kind_raw if kind_raw in ("issue", "page") else None
    # Structured issue filters. Any one of them puts us in filtered issue-search mode.
    status_filter = (request.query_params.get("status") or "").strip()
    label_filter = (request.query_params.get("label") or "").strip()
    project_filter = (request.query_params.get("project") or "").strip()
    if project_filter and issues.parse_project_filter(project_filter) is None:
        return HTMLResponse("<h1>Invalid project filter</h1>", status_code=400)
    filtered_mode = bool(status_filter or label_filter or project_filter)
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    # Fetch one extra hit to know whether a next page exists without a count query —
    # the same trick the activity feed uses. Trim it off before rendering. In filtered
    # mode the issue_search path applies the structured filters; otherwise it is the
    # plain cross-kind search honouring the scope tab.
    fetch = _SEARCH_PAGE + 1
    skip = (page - 1) * _SEARCH_PAGE
    user = getattr(request.state, "user", None)
    # Search is gated by the viewer: a private project's issues / a private space's
    # pages never appear to someone who can't see them (admins see all).
    if not q:
        hits = []
    elif filtered_mode:
        hits = issue_search.search_issues(
            conn,
            q,
            status=status_filter or None,
            label=label_filter or None,
            project=project_filter or None,
            limit=fetch,
            offset=skip,
            actor=user,
        )
    else:
        hits = search.search(conn, q, kind=kind, limit=fetch, offset=skip, actor=user)
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
            {
                "q": q,
                "kind": scope or "",
                "status": status_filter,
                "label": label_filter,
                "project": project_filter,
                "page": page_num,
            }
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
            "filtered_mode": filtered_mode,
            "status_filter": status_filter,
            "label_filter": label_filter,
            "project_filter": project_filter,
            # The filter dropdowns must not enumerate a private project (its statuses
            # or its name) to someone who can't see it.
            "all_statuses": sorted(
                {i["status"] for i in issues.list_issues(conn, visible_project_ids=access.visible_project_filter(conn, user))}
            ),
            "all_labels": labels.list_labels(conn),
            "all_projects": projects.list_projects(conn, access.visible_project_filter(conn, user)),
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
    items = notifications.list_notifications(conn, user["id"], limit=100, actor=user)
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

    user = getattr(request.state, "user", None)
    # Only count/show issues in projects this viewer may see (public + their own
    # private ones; admins see all; the backlog is always in).
    all_issues = issues.list_issues(
        conn, visible_project_ids=access.visible_project_filter(conn, user)
    )
    from collections import Counter
    status_counts = Counter(issue["status"] for issue in all_issues)
    # Recent issues (newest first)
    recent_issues = sorted(
        all_issues, key=lambda x: x.get("created_at", ""), reverse=True
    )[:5]

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
    # Priority is passed straight through (an unknown value matches nothing), exactly
    # as the API does — so the list and GET /issues stay aligned. Assignee is a user
    # id; a non-numeric value means "no assignee filter" (the dropdown only ever
    # submits real ids, so this only guards a hand-edited URL).
    priority_filter = (request.query_params.get("priority") or "").strip()
    assignee_raw = (request.query_params.get("assignee") or "").strip()
    assignee_id = int(assignee_raw) if assignee_raw.lstrip("-").isdigit() else None
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
    # Sprint is a plain issue column; a numeric value restricts to that sprint, and
    # anything else (incl. blank) means "don't filter by sprint" — the same lenient
    # parse the assignee filter uses, since the dropdown only ever submits real ids.
    sprint_raw = (request.query_params.get("sprint") or "").strip()
    sprint_id = int(sprint_raw) if sprint_raw.isdigit() else None
    # Archived issues are hidden by default (the soft-delete semantics every list
    # wants); the "Show archived" toggle submits a truthy value to include them.
    archived_raw = (request.query_params.get("archived") or "").strip()
    include_archived = archived_raw.lower() in ("1", "true", "on", "yes")
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
    user = getattr(request.state, "user", None)
    filtered = issues.list_issues(
        conn,
        status=status_filter,
        priority=priority_filter or None,
        assignee_id=assignee_id,
        search=search,
        project_id=project_id,
        backlog=backlog,
        sprint_id=sprint_id,
        include_archived=include_archived,
        ids=ids,
        visible_project_ids=access.visible_project_filter(conn, user),
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
            priority=priority_filter,
            assignee=assignee_raw,
            label=label_filter,
            project=project_raw,
            sprint=sprint_raw,
            search=search,
            archived=archived_raw,
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

    # `user` was resolved above (for the visibility filter); reuse it here.
    can_write = user is not None and identity.can_write(user)

    # Sprint-filter options. A sprint belongs to one project, so each option is
    # labelled with its project's key to disambiguate same-named sprints across
    # projects (e.g. "ATH · Sprint 1"). One pass over projects builds the key map.
    all_projects = projects.list_projects(conn, access.visible_project_filter(conn, user))
    project_keys = {p["id"]: p["key"] for p in all_projects}
    # project_keys holds exactly the projects this viewer may see, so keeping only
    # sprints whose project is in it drops sprints in private projects the viewer can't
    # see — their name/id would otherwise leak through this dropdown.
    all_sprints = [
        {"id": s["id"], "name": s["name"], "project_key": project_keys[s["project_id"]]}
        for s in sprints.list_sprints(conn)
        if s["project_id"] in project_keys
    ]

    # Pre-fill link for "Save current view" — carries the active filters to the
    # saved-filters create form so an ad-hoc search becomes a named filter in one
    # click. The create form's assignee field is named assignee_id, so map to that.
    save_filter_url = "/aegis/filters?" + urlencode(
        {
            "status": status_filter or "",
            "priority": priority_filter,
            "assignee_id": assignee_raw,
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
            "all_statuses": _statuses_in_use(conn, access.visible_project_filter(conn, user)),
            "priority_filter": priority_filter,
            "priorities": issues.PRIORITIES,
            "assignee_filter": assignee_raw,
            "all_users": users.list_users(conn),
            "label_filter": label_filter,
            "all_labels": labels.list_labels(conn),
            "project_filter": project_raw,
            "all_projects": all_projects,
            "sprint_filter": sprint_raw,
            "all_sprints": all_sprints,
            "include_archived": include_archived,
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


def _int_or_none(raw: str | None) -> int | None:
    """A query param parsed to int, or None if absent/blank/garbage — a filter the
    user can't set to a bad value just by editing the URL."""
    if raw is None or raw.strip() == "" or not raw.strip().lstrip("-").isdigit():
        return None
    return int(raw)


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
        context={"all_projects": projects.list_projects(conn, access.visible_project_filter(conn, user))},
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
        if not project_id.isdigit() or not access.can_see_project(conn, user, int(project_id)):
            # can_see_project is False for a missing project too, so a private project the
            # actor can't see gives the SAME 400 — no write into (or discovery of) a
            # hidden project, mirroring the REST API.
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
    # Visibility first: a private issue the user can't see is "not found" (same 404 as a
    # missing one), so neither its existence nor a write path leaks — the browser twin of
    # the API's _issue_for_write gate, and it catches a creator/assignee who lost access
    # when the project went private.
    if not issue or not access.can_see_project_or_backlog(
        conn, user, issue["project_id"]
    ):
        return None, HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
    if not identity.can_write(user):
        return None, _readonly_response()
    if not issues.can_modify(issue, user["id"]):
        return None, HTMLResponse(
            '<div class="error">Only the issue creator or assignee may modify it.</div>',
            status_code=403,
        )
    return issue, None


def _issue_visible_or_404(conn, issue_id, user):
    """Return (issue, None) if the user may SEE this issue, else (None, 404 response).
    The visibility-ONLY gate for additive web writes (comments, attachments) and the
    personal watch toggle — any writer may act on a VISIBLE issue, so this stops short of
    the creator/assignee check _authorize_issue_write applies. A hidden issue reads as
    "not found", so its existence never leaks through a write path."""
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, user, issue["project_id"]
    ):
        return None, HTMLResponse('<div class="error">Issue not found.</div>', status_code=404)
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
        # Gate the blocker warning by the viewer: a blocker in a private project they
        # can't see is omitted, so its key/title never leaks through the close warning.
        blockers = dependencies.open_blockers(conn, issue_id, actor=user)
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
    user = getattr(request.state, "user", None)
    # An issue in a private project the viewer can't see is treated exactly like a
    # missing one — same 404, so privacy never leaks through the existence of a ref.
    # Backlog issues (no project) read like a public one.
    if not issue or not access.can_see_project_or_backlog(conn, user, issue["project_id"]):
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
    # Gate children by viewer visibility (a child can sit in a private project the
    # viewer isn't in), matching the JSON API — else the detail page leaks it.
    children = issues.list_children(
        conn, issue_id, visible_project_ids=access.visible_project_filter(conn, user)
    )

    context = {
        "issue": issue,
        "body_html": render_body(conn, issue["body"], actor=user),
        # "Referenced by" hides sources in projects/spaces the viewer can't see.
        "backlinks": links.backlinks(conn, "issue", issue_id, actor=user),
        # Typed dependencies, gated like the backlinks: a blocks/relates edge to an
        # issue in a private project the viewer can't see is dropped, so its key/title
        # never leaks through this issue's relationship list.
        "links": dependencies.list_links(conn, issue_id, actor=user),
        "comments": comment_rows,
        "attachments": attachments.list_for(conn, "issue", issue_id),
        "is_watching": user is not None
        and notifications.is_watching(conn, user["id"], "issue", issue_id),
        "users": users.list_users(conn),
        "contributors": contributors.list_contributors(conn, issue_id),
        "issue_labels": labels.labels_for_issue(conn, issue_id),
        "all_labels": labels.list_labels(conn),
        "all_projects": projects.list_projects(conn, access.visible_project_filter(conn, user)),
        # Sprints the issue could join — only its own project's, since a sprint
        # belongs to one project (and a backlog issue with no project has none).
        "issue_sprints": (
            sprints.list_sprints(conn, project_id=issue["project_id"])
            if issue["project_id"]
            else []
        ),
        # The sprint the issue is currently in (for the read-view label), or None.
        "issue_sprint": (
            sprints.get_sprint(conn, issue["sprint_id"])
            if issue.get("sprint_id")
            else None
        ),
        "issue_statuses": statuses.list_statuses(conn, issue["project_id"]),
        # Only render the parent if the viewer may see it — a parent in a private
        # project the viewer isn't in renders as none (no key/title leak), the same as
        # if it were unset.
        "parent": (
            issues.get_issue(conn, issue["parent_id"])
            if issue.get("parent_id")
            and access.can_see_issue(conn, user, issue["parent_id"])
            else None
        ),
        "children": children,
        "children_done": sum(
            1 for c in children if statuses.is_done(conn, c["project_id"], c["status"])
        ),
        "can_modify": can_modify,
        "can_write": can_write,
        # Admins may moderate (delete) any comment, not just their own — drives the
        # per-comment Delete control the same way the server-side override gates it.
        "is_admin": user is not None and identity.is_admin(user),
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


@router.get("/aegis/issues/{ref}/history", response_class=HTMLResponse)
def issue_history_view(
    request: Request, ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    """Time-travel for one issue — the browser twin of GET /issues/{id}/state. Shows the
    issue's lifecycle state reconstructed AS OF a chosen checkpoint (?as_of=<event id>,
    default now), beside the timeline of its events, each a clickable cutoff. A thin
    client over issue_history.project_issue_state; gated exactly like the detail page (a
    hidden/missing issue is the same 404, so existence never leaks)."""
    if _templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    issue = issues.get_by_ref(conn, ref)
    user = getattr(request.state, "user", None)
    if not issue or not access.can_see_project_or_backlog(
        conn, user, issue["project_id"]
    ):
        return _templates.TemplateResponse(
            request=request,
            name="aegis/issues.html",
            context={"issues": [], "status_filter": "", "search": "", "error": f"Issue {ref} not found"},
            status_code=404,
        )
    as_of = _int_or_none(request.query_params.get("as_of"))
    snapshot = issue_history.project_issue_state(
        conn, issue["id"], as_of_event_id=as_of
    )
    # The issue's timeline (newest-first), each event a checkpoint to time-travel to.
    events = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    )
    return _templates.TemplateResponse(
        request=request,
        name="aegis/issue_history.html",
        context={
            "issue": issue,
            "snapshot": snapshot,
            "events": events,
            "as_of": as_of,
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
        if not project_id.isdigit() or not access.can_see_project(conn, user, int(project_id)):
            # can_see_project is False for a missing project too, so a private project the
            # actor can't see gives the SAME 400 — no write into (or discovery of) a
            # hidden project, mirroring the REST API.
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


@router.post("/aegis/issues/{issue_id}/sprint", dependencies=[Depends(verify_csrf)])
def change_issue_sprint(
    request: Request,
    issue_id: int,
    sprint_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Move an issue into a sprint, or back to the backlog, from the detail page.
    Same write gate as status/assign. An empty value means "no sprint" (None);
    otherwise the sprint must exist AND belong to the issue's OWN project — the
    same rule the REST PUT /issues/{id}/sprint enforces (there a 422), surfaced
    here as a 400."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to change sprint.</div>',
            status_code=401,
        )
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

    sprint_id = sprint_id.strip()
    if sprint_id == "":
        target: int | None = None
    else:
        if not sprint_id.isdigit():
            return HTMLResponse('<div class="error">No such sprint.</div>', status_code=400)
        sprint = sprints.get_sprint(conn, int(sprint_id))
        if sprint is None:
            return HTMLResponse('<div class="error">No such sprint.</div>', status_code=400)
        # A sprint belongs to one project; an issue can only join a sprint in its
        # own project (a backlog issue with no project can't be in any sprint).
        if sprint["project_id"] != issue["project_id"]:
            return HTMLResponse(
                '<div class="error">That sprint belongs to a different project.</div>',
                status_code=400,
            )
        target = int(sprint_id)

    issues.set_sprint(conn, issue_id, target)
    issue_activity.record_sprint_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["sprint_id"],
        after=target,
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
        # A parent the actor can't see collapses to the same 400 as a missing one, so
        # you can't nest under (or probe the existence of) a hidden issue.
        if parent is None or not access.can_see_issue(conn, user, parent["id"]):
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


@router.post("/aegis/issues/{issue_id}/contributors", dependencies=[Depends(verify_csrf)])
def add_issue_contributor(
    request: Request,
    issue_id: int,
    user_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delegate the issue to a teammate (human or agent) by adding them as a
    contributor — the assignee stays the accountable owner. Same write gate as
    labels/status. An unknown user is a 400; idempotent re-add is a no-op."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to add contributors.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    try:
        target = int(user_id)
    except ValueError:
        return HTMLResponse('<div class="error">Pick a user.</div>', status_code=400)
    if users.get_user(conn, target) is None:
        return HTMLResponse('<div class="error">No such user.</div>', status_code=400)
    if contributors.add_contributor(conn, issue_id, target, user["id"]):  # idempotent
        issue_activity.record_contributor_added(
            conn, actor_id=user["id"], issue_id=issue_id, user_id=target
        )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/contributors/{user_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_issue_contributor(
    request: Request,
    issue_id: int,
    user_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Remove a contributor. Same write gate. POST (not DELETE) because HTML forms
    can't issue DELETE."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to manage contributors.</div>',
            status_code=401,
        )
    _, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    if contributors.remove_contributor(conn, issue_id, user_id):
        issue_activity.record_contributor_removed(
            conn, actor_id=user["id"], issue_id=issue_id, user_id=user_id
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
    # A target the actor can't see collapses to the same 400 as a missing one (the
    # REST twin does this too), closing both the link-into-hidden write and the
    # existence oracle.
    if target is None or not access.can_see_issue(conn, user, target["id"]):
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


@router.post("/aegis/issues/{issue_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_issue_web(
    request: Request,
    issue_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Archive (soft-delete) an issue from its detail page. Same creator-or-assignee
    gate as status/assign. The row is kept; it just drops out of the default lists
    and boards until restored. 303 back to the issue (now shown as archived)."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to archive.</div>',
            status_code=401,
        )
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    updated = issues.set_archived(conn, issue_id, True)
    issue_activity.record_archive_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["archived_at"],
        after=updated["archived_at"],
    )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/unarchive", dependencies=[Depends(verify_csrf)])
def unarchive_issue_web(
    request: Request,
    issue_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Restore an archived issue to the active lists, from its detail page. Same
    gate as archive."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to restore.</div>',
            status_code=401,
        )
    issue, err = _authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    updated = issues.set_archived(conn, issue_id, False)
    issue_activity.record_archive_change(
        conn,
        actor_id=user["id"],
        issue_id=issue_id,
        before=issue["archived_at"],
        after=updated["archived_at"],
    )
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
    _, err = _issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse('<div class="error">Comment cannot be empty.</div>', status_code=400)

    comments.add_comment(conn, issue_id=issue_id, author_id=user["id"], body=body)
    issue_activity.record_commented(
        conn, actor_id=user["id"], issue_id=issue_id, body=body
    )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


def _own_comment_or_response(conn, issue_id, comment_id, user, *, allow_admin=False):
    """Return the comment if it belongs to this issue and the session user is its
    author; otherwise an HTMLResponse (404/403) to return as-is. Mirrors the
    API's author-ownership rule on the web write paths. allow_admin lets an admin
    through for moderation — used only on delete, matching the API override; edit
    stays author-only."""
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
        return None, HTMLResponse('<div class="error">Comment not found.</div>', status_code=404)
    if existing["author_id"] != user["id"] and not (allow_admin and identity.is_admin(user)):
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
    _, err = _issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    _, err = _own_comment_or_response(conn, issue_id, comment_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse('<div class="error">Comment cannot be empty.</div>', status_code=400)
    if comments.update_comment(conn, comment_id, body=body) is None:
        # vanished between the author check and the write (a race) — 404, not a
        # silent "success" redirect.
        return HTMLResponse('<div class="error">Comment not found.</div>', status_code=404)
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
    _, err = _issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    _, err = _own_comment_or_response(conn, issue_id, comment_id, user, allow_admin=True)
    if err is not None:
        return err
    if not comments.delete_comment(conn, comment_id):
        # vanished between the author check and the delete (a race) — 404, and don't
        # record an event for a deletion that didn't happen.
        return HTMLResponse('<div class="error">Comment not found.</div>', status_code=404)
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
    _, err = _issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
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
    _, err = _issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
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
    # You can't watch what you can't see: a hidden issue is "not found", and gating here
    # also stops a subscription that would later leak the issue through notifications.
    _, err = _issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
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
    _, err = _issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    notifications.unwatch(conn, user["id"], "issue", issue_id)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)
