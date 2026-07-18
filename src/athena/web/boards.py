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

from athena.aegis import issue_commands, issues, statuses
from athena.core import access
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import _attach_labels, _statuses_in_use, get_templates

router = APIRouter()


def _render_board(
    request: Request,
    conn: sqlite3.Connection,
    *,
    search: str,
    status_filter: str,
):
    """Render the board for the current search/status filter — the ONE place the
    board's column-grouping lives, shared by the GET view and the drag-move POST so
    a move re-renders exactly the board a fresh load would. Returns the full page on
    a normal request and just the .board partial on an HTMX request (so a swap
    replaces only the board, keeping the filter chrome)."""
    # Same data-layer path as the issues list and the API: filtering (status +
    # search) is done by list_issues, NOT re-implemented in Python here. The board
    # only shows issues in projects the viewer may see (admins all; backlog always).
    user = getattr(request.state, "user", None)
    filtered = issues.list_issues(
        conn,
        status=status_filter or None,
        search=search,
        visible_project_ids=access.visible_project_filter(conn, user),
    )
    _attach_labels(conn, filtered)

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
    # The status filter offers every status currently in use on issues THIS VIEWER may
    # see — the same gated option set the issue list uses.
    all_statuses = _statuses_in_use(conn, access.visible_project_filter(conn, user))

    template = "aegis/partials/boards_content.html" if request.headers.get("HX-Request") else "aegis/boards.html"
    return get_templates().TemplateResponse(
        request=request,
        name=template,
        context={
            "board_columns": board_columns,
            "all_statuses": all_statuses,
            "search": search,
            "status_filter": status_filter,
        },
    )


@router.get("/aegis/boards", response_class=HTMLResponse)
def boards(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Boards view (Aegis) using real list_issues with search/filter."""
    search = (request.query_params.get("search") or "").strip()
    status_filter = (request.query_params.get("status") or "").strip()
    return _render_board(request, conn, search=search, status_filter=status_filter)


@router.post("/aegis/boards/move/{issue_id}", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def board_move_issue(
    request: Request,
    issue_id: int,
    new_status: str = Form(...),
    search: str = Form(""),
    status: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Change an issue's status from the board, then show the board with the card in
    its new column. Two callers share this endpoint: the drag gesture (board-dnd.js,
    an HTMX request that swaps just the .board) and the per-card keyboard "Move" form
    (a plain POST when JS is off). Both send new_status plus the active search/status
    filter; the response shape follows the request — an HTMX swap of the re-rendered
    board, or a 303 back to /aegis/boards (so a refresh doesn't re-POST) for the form.

    Gated like every other write — a logged-out caller is a 401 (the UI only offers
    the move when signed in), a read-only (viewer) role can't move a card at all, and
    the move applies only if the session user may act on the issue (creator,
    assignee, delegated contributor, or admin).

    A move that isn't allowed or doesn't apply — the actor can't write this issue, or
    the target status isn't valid for the issue's project (boards can mix projects
    with different status sets) — leaves the board UNCHANGED, so the card simply snaps
    back. This quick-move path deliberately skips the open-blockers advisory nudge the
    detail page shows: dependencies are advisory, and that warning belongs on the
    focused view, not a quick board move."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to move cards.</div>',
            status_code=401,
        )
    # The board deliberately snaps back on a rejected move. The shared command is
    # still the one owner of visibility, role/scope, can-act-on policy,
    # status validation, write, and audit; this adapter only chooses not to render
    # its domain error inline on a drag surface.
    try:
        issue_commands.update_issue(
            conn, actor=user, issue_id=issue_id, status=new_status
        )
    except issue_commands.IssueCommandError:
        pass
    search, status = search.strip(), status.strip()
    if request.headers.get("HX-Request"):
        return _render_board(request, conn, search=search, status_filter=status)
    # No-JS keyboard form: redirect to the board (preserving filters) so the URL is a
    # GET and a refresh re-reads rather than re-submitting the move.
    return RedirectResponse(
        "/aegis/boards?" + urlencode({"search": search, "status": status}),
        status_code=303,
    )
