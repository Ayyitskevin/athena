"""Browser routes for the Aegis issue list and the create form.

Edit, preview, detail, and history live in ``issue_editor`` and ``issue_detail``.
Field writes live in ``issue_actions``; discussion writes live in
``issue_discussion``. A thin client: mutations go through an Aegis command.
Shared HTML helpers live in ``web.issue_html``; shared list helpers live in
``web.browsing``. ``GET /aegis/issues/new`` stays on this router so it registers
before ``GET /aegis/issues/{ref}``.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from athena.aegis import (
    issue_commands,
    issues,
    projects,
    sprints,
)
from athena.core import (
    access,
    identity,
    labels,
    users,
)
from athena.core.deps import get_conn
from athena.web.browsing import (
    attach_labels,
    readonly_response,
    statuses_in_use,
)
from athena.web.csrf import verify_csrf
from athena.web.issue_html import (
    issue_command_response,
)
from athena.web.router import get_templates

router = APIRouter()


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


@router.get("/aegis/issues", response_class=HTMLResponse)
def issues_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Issues list view (Aegis) wired to real DB via data-access layer (list_issues)."""

    status_filter = request.query_params.get("status")
    # Priority is passed straight through (an unknown value matches nothing), exactly
    # as the API does — so the list and GET /issues stay aligned. Assignee is a user
    # id; a non-numeric value means "no assignee filter" (the dropdown only ever
    # submits real ids, so this only guards a hand-edited URL).
    priority_filter = (request.query_params.get("priority") or "").strip()
    assignee_raw = (request.query_params.get("assignee") or "").strip()
    assignee_id = issues.parse_filter_id(assignee_raw)
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
    sprint_id = issues.parse_filter_id(sprint_raw)
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
    # Resolved once and reused: every dropdown below is gated by the same set, and
    # each call re-reads the membership tables.
    visible_project_ids = access.visible_project_filter(conn, user)

    # Paging is decided BEFORE the read, because the read is what it bounds. A
    # non-numeric page/per_page in the query string is the only failure here; fall
    # back to the defaults for that.
    try:
        page = max(1, int(request.query_params.get("page", 1)))
        per_page = max(5, min(50, int(request.query_params.get("per_page", 20))))
    except (TypeError, ValueError):
        page, per_page = 1, 20

    # One filter set, used for both the count and the page, so the "N issues" label
    # can never describe a different query than the rows under it.
    issue_filters: dict = {
        "status": status_filter,
        "priority": priority_filter or None,
        "assignee_id": assignee_id,
        "search": search,
        "project_id": project_id,
        "backlog": backlog,
        "sprint_id": sprint_id,
        "include_archived": include_archived,
        "ids": ids,
        "visible_project_ids": visible_project_ids,
    }
    # Sort and page in SQL, through the same data-access path the API uses. This
    # handler used to fetch EVERY matching issue, attach every issue's labels, sort
    # the whole list in Python and slice twenty rows out of it — 74 ms per page view
    # at 10k issues against 0.2 ms for the bounded read. Sorting after the slice is
    # not an option either: it would only reorder the rows that reached the page.
    total = issues.count_issues(conn, **issue_filters)
    paged = issues.list_issues(
        conn,
        **issue_filters,
        sort=sort,
        order=order,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    attach_labels(conn, paged)  # one bulk query over this page's rows

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
    all_projects = projects.list_projects(conn, visible_project_ids)
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

    template = (
        "aegis/partials/issues_table.html"
        if request.headers.get("HX-Request")
        else "aegis/issues.html"
    )
    return get_templates().TemplateResponse(
        request=request,
        name=template,
        context={
            "issues": paged,
            "status_filter": status_filter or "",
            "all_statuses": statuses_in_use(conn, visible_project_ids),
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


@router.get("/aegis/issues/new", response_class=HTMLResponse)
def new_issue_form(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Render the new issue creation form."""
    user = getattr(request.state, "user", None)
    if user is not None and not identity.can_write(user):
        return readonly_response()
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/issue_form.html",
        context={
            "all_projects": projects.list_projects(
                conn, access.visible_project_filter(conn, user)
            )
        },
    )


@router.post(
    "/aegis/issues", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)]
)
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

    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to create issues.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return readonly_response()

    # Project is optional ("" = no project); parsing the HTML value is a transport
    # concern, while existence/visibility belongs to the shared command.
    project_id = project_id.strip()
    if project_id == "":
        project: int | None = None
    else:
        if not project_id.isdigit():
            return HTMLResponse(
                '<div class="error">No such project.</div>', status_code=400
            )
        project = int(project_id)
    try:
        issue = issue_commands.create_issue(
            conn,
            actor=user,
            title=title,
            body=body,
            priority=priority,
            project_id=project,
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
    # HTMX follows HX-Redirect after the successful create without an inline script.
    return HTMLResponse(
        f'<div class="success">Created issue #{issue["id"]}.</div>',
        headers={"HX-Redirect": f"/aegis/issues/{issue['id']}"},
    )
