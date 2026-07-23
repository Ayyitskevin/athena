"""Web routes for the Aegis board — the kanban view and status moves.

Split out of web/router.py (the god-file) to keep each web surface navigable,
following the same one-module-per-area pattern as web/projects.py and friends. Its
own APIRouter, mounted by main.py. A thin client over the issues data layer — it
owns no data. The shared label/status render helpers and the template accessor are
imported from web.router (where the issues cluster still lives).
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import (
    issue_commands,
    issue_etags,
    issues,
    projects,
    sprints,
    statuses,
)
from athena.core import access
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import _attach_labels, _statuses_in_use, get_templates

router = APIRouter()


def _board_scope(
    project_filter: str,
    sprint_filter: str,
    *,
    strict_sprint: bool = False,
) -> tuple[int | None, bool, int | None] | None:
    """Parse placement filters with the same semantics as the issue-list view.

    A malformed project fails loud instead of widening to every project. Read-only
    sprint queries intentionally retain the issue-list's lenient behavior, while a
    write caller opts into strict validation so malformed context cannot accompany a
    mutation.
    """
    parsed_project = issues.parse_project_filter(project_filter)
    if parsed_project is None:
        return None
    project_id, backlog = parsed_project
    sprint_id = issues.parse_filter_id(sprint_filter)
    if sprint_filter and sprint_id is None and strict_sprint:
        return None
    return project_id, backlog, sprint_id


def _board_url(
    *,
    search: str,
    status: str,
    project: str,
    sprint: str,
    move_conflict: str | None = None,
) -> str:
    """Build the board URL shared by successful and refused native moves."""
    query = {
        "search": search,
        "status": status,
        "project": project,
        "sprint": sprint,
    }
    if move_conflict is not None:
        query["move_conflict"] = move_conflict
    return f"/aegis/boards?{urlencode(query)}"


def _render_board(
    request: Request,
    conn: sqlite3.Connection,
    *,
    search: str,
    status_filter: str,
    project_filter: str,
    sprint_filter: str,
    move_conflict: str | None = None,
):
    """Render one visibility-safe, placement-filtered fleet board.

    This is the ONE place the board's swimlane/column grouping lives, shared by the
    GET view and the drag-move POST so a move re-renders exactly what a fresh load
    would. Normal requests receive the full page; HTMX requests receive only the
    .board partial, keeping the filter chrome in place.
    """
    scope = _board_scope(project_filter, sprint_filter)
    if scope is None:
        return HTMLResponse("<h1>Invalid project filter</h1>", status_code=400)
    project_id, backlog, sprint_id = scope

    # Same data-layer path as the issues list and API: every filter is applied by
    # list_issues, NOT re-implemented in Python. The board only sees issues in
    # projects the viewer may see (admins all; backlog always).
    user = getattr(request.state, "user", None)
    visible_project_ids = access.visible_project_filter(conn, user)
    filtered = issues.list_issues(
        conn,
        status=status_filter or None,
        search=search,
        project_id=project_id,
        backlog=backlog,
        sprint_id=sprint_id,
        visible_project_ids=visible_project_ids,
    )
    _attach_labels(conn, filtered)
    if user is not None:
        for issue in filtered:
            issue["etag"] = issue_etags.representation_and_etag(issue, issue["labels"])[
                1
            ]

    # Each card carries its OWN project's status menu, so the keyboard "Move" control
    # offers exactly the valid targets for that issue (the backlog uses the default
    # set). Cache per project so a board of N issues across M projects costs M lookups.
    status_opts: dict = {}
    for issue in filtered:
        pid = issue.get("project_id")
        if pid not in status_opts:
            status_opts[pid] = statuses.status_names(conn, pid)
        issue["status_options"] = status_opts[pid]

    # Statuses are per-project now, so the board's columns are dynamic: the union of
    # valid destinations for every matching card (including currently unoccupied
    # statuses), ordered by category (todo → doing → done) then name. A narrow filter
    # therefore still has somewhere to drag an open card.
    from collections import defaultdict

    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for issue in filtered:
        if issue.get("assignee_id") is None:
            lane = "unassigned"
        elif issue.get("assignee_is_agent"):
            lane = "agent"
        else:
            lane = "human"
        grouped[lane][issue.get("status", "")].append(issue)

    cat_rank = {"todo": 0, "doing": 1, "done": 2}

    def _sort_key(name: str) -> tuple[int, str]:
        return (cat_rank.get(statuses.global_category(conn, name), 1), name)

    status_names = sorted(
        {status_name for issue in filtered for status_name in issue["status_options"]}
        | {issue.get("status", "") for issue in filtered},
        key=_sort_key,
    )
    board_lanes = []
    for key, label in (
        ("agent", "Agent work"),
        ("human", "Human work"),
        ("unassigned", "Unassigned"),
    ):
        lane_issues = grouped[key]
        board_lanes.append(
            {
                "key": key,
                "label": label,
                "count": sum(len(items) for items in lane_issues.values()),
                "columns": [
                    {
                        "name": name,
                        "label": name.replace("_", " ").title(),
                        "issues": lane_issues[name],
                    }
                    for name in status_names
                ],
            }
        )

    # The status filter offers every status currently in use on issues THIS VIEWER may
    # see — the same gated option set the issue list uses.
    all_statuses = _statuses_in_use(conn, visible_project_ids)

    # Placement filter options are visibility-safe. Sprint labels carry their
    # project key to disambiguate same-named sprints across projects.
    all_projects = projects.list_projects(conn, visible_project_ids)
    project_keys = {project["id"]: project["key"] for project in all_projects}
    all_sprints = [
        {
            "id": sprint["id"],
            "name": sprint["name"],
            "project_key": project_keys[sprint["project_id"]],
        }
        for sprint in sprints.list_sprints(conn)
        if sprint["project_id"] in project_keys
    ]

    template = (
        "aegis/partials/boards_content.html"
        if request.headers.get("HX-Request")
        else "aegis/boards.html"
    )
    return get_templates().TemplateResponse(
        request=request,
        name=template,
        context={
            "board_lanes": board_lanes,
            "board_total": len(filtered),
            "all_statuses": all_statuses,
            "search": search,
            "status_filter": status_filter,
            "all_projects": all_projects,
            "project_filter": project_filter,
            "all_sprints": all_sprints,
            "sprint_filter": sprint_filter,
            "move_conflict": move_conflict,
        },
    )


@router.get("/aegis/boards", response_class=HTMLResponse)
def boards(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Fleet board using the shared issue query and visibility contract."""
    search = (request.query_params.get("search") or "").strip()
    status_filter = (request.query_params.get("status") or "").strip()
    project_filter = (request.query_params.get("project") or "").strip()
    sprint_filter = (request.query_params.get("sprint") or "").strip()
    requested_conflict = request.query_params.get("move_conflict")
    move_conflict = (
        requested_conflict if requested_conflict in {"stale", "policy"} else None
    )
    return _render_board(
        request,
        conn,
        search=search,
        status_filter=status_filter,
        project_filter=project_filter,
        sprint_filter=sprint_filter,
        move_conflict=move_conflict,
    )


@router.post(
    "/aegis/boards/move/{issue_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def board_move_issue(
    request: Request,
    issue_id: int,
    new_status: str = Form(...),
    if_match: str = Form(""),
    search: str = Form(""),
    status: str = Form(""),
    project: str = Form(""),
    sprint: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Change an issue's status from the board, then show the board with the card in
    its new column. Two callers share this endpoint: the drag gesture (board-dnd.js,
    an HTMX request that swaps just the .board) and the per-card keyboard "Move" form
    (a plain POST when JS is off). Both send new_status plus the active
    search/status/project/sprint filters; the response shape follows the request —
    an HTMX swap of the re-rendered board, or a 303 back to /aegis/boards (so a
    refresh doesn't re-POST) for the form.

    Gated like every other write — a logged-out caller is a 401 (the UI only offers
    the move when signed in), a read-only (viewer) role can't move a card at all, and
    the move applies only if the session user may act on the issue (creator,
    assignee, delegated contributor, or admin).

    A move that isn't allowed or doesn't apply — the actor can't write this issue, or
    the target status isn't valid for the issue's project (boards can mix projects
    with different status sets) — leaves the board UNCHANGED, so the card simply snaps
    back. Stale and blocked-close-policy refusals are different: both fail closed
    and refresh the board with an explicit conflict notice. The board never offers
    the human policy-override control; that deliberate exception remains on the
    focused issue view."""
    search, status = search.strip(), status.strip()
    project, sprint = project.strip(), sprint.strip()
    # Validate before the write: a forged, malformed filter must not mutate an issue
    # and only then fail while re-rendering its response.
    if _board_scope(project, sprint, strict_sprint=True) is None:
        return HTMLResponse("<h1>Invalid board filter</h1>", status_code=400)

    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to move cards.</div>',
            status_code=401,
        )
    # The board deliberately snaps back on ordinary rejected moves. The shared
    # command is still the one owner of visibility, role/scope, can-act-on policy,
    # status validation, precondition comparison, write, and audit. This adapter
    # makes stale-precondition and hard-policy refusals visible because silently
    # snapping back would hide an operationally relevant conflict.
    try:
        issue_commands.update_issue(
            conn,
            actor=user,
            issue_id=issue_id,
            status=new_status,
            if_match=[if_match],
        )
    except issue_commands.IssueCommandError as exc:
        if exc.kind == "precondition_failed":
            conflict_url = _board_url(
                search=search,
                status=status,
                project=project,
                sprint=sprint,
                move_conflict="stale",
            )
            if request.headers.get("HX-Request"):
                response_headers = {"HX-Redirect": conflict_url}
                if exc.current_etag is not None:
                    response_headers["ETag"] = exc.current_etag
                return HTMLResponse(
                    "",
                    status_code=412,
                    headers=response_headers,
                )
            return RedirectResponse(conflict_url, status_code=303)
        if exc.code == issue_commands.BLOCKED_CLOSE_POLICY_ERROR_CODE:
            policy_url = _board_url(
                search=search,
                status=status,
                project=project,
                sprint=sprint,
                move_conflict="policy",
            )
            if request.headers.get("HX-Request"):
                return HTMLResponse(
                    "",
                    status_code=409,
                    headers={
                        "HX-Redirect": policy_url,
                        "Cache-Control": "private, no-store",
                    },
                )
            return RedirectResponse(policy_url, status_code=303)
    if request.headers.get("HX-Request"):
        return _render_board(
            request,
            conn,
            search=search,
            status_filter=status,
            project_filter=project,
            sprint_filter=sprint,
        )
    # No-JS keyboard form: redirect to the board (preserving filters) so the URL is a
    # GET and a refresh re-reads rather than re-submitting the move.
    return RedirectResponse(
        _board_url(
            search=search,
            status=status,
            project=project,
            sprint=sprint,
        ),
        status_code=303,
    )
