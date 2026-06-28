"""The Label hub — browse everything tagged with a label, across Aegis issues and
Mentor pages.

Labels are shared vocabulary that now lives in core, so a label can sit on an issue
AND a page. This is the payoff: a cross-cutting view that, for one label, gathers
both. A thin client like the rest of web/ — it reads through core.labels and the two
feature data layers (aegis.issues, mentor.pages) and owns no data. Its own module
because it is genuinely cross-cutting; it deliberately uses the /aegis/labels URL
space (the REST /labels path is the JSON vocabulary the autocomplete reads, so the
HTML hub must not shadow it).
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from athena.aegis import issues
from athena.core import access, labels
from athena.core.deps import get_conn
from athena.mentor import pages
from athena.web.router import get_templates

router = APIRouter()


@router.get("/aegis/labels", response_class=HTMLResponse)
def labels_index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Every label with how many issues and pages carry it, each linking to its hub
    view. Reading is open, like the issue list."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    # The per-label counts reflect only what this viewer may see, so a label's count
    # never reveals how many hidden issues/pages carry it.
    user = getattr(request.state, "user", None)
    usage = labels.label_usage(
        conn,
        visible_project_ids=access.visible_project_filter(conn, user),
        visible_space_ids=access.visible_space_filter(conn, user),
    )
    return templates.TemplateResponse(
        request=request,
        name="labels/index.html",
        context={"labels": usage},
    )


@router.get("/aegis/labels/{name}", response_class=HTMLResponse)
def label_detail(
    request: Request, name: str, conn: sqlite3.Connection = Depends(get_conn)
):
    """One label's hub: every active issue and every page that carries it. Label
    names are case-insensitive (the column is COLLATE NOCASE), so the URL matches
    however it's cased. 404 (rendered) if no label has that name."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    label = labels.get_label_by_name(conn, name)
    if label is None:
        return templates.TemplateResponse(
            request=request,
            name="labels/detail.html",
            context={"name": name, "label": None, "issues": [], "pages": []},
            status_code=404,
        )
    # Resolve the ids the label points at, then read each kind through its own data
    # layer — the same ids→rows split the issue list uses for label filtering. Issues
    # go through list_issues, so archived ones are hidden by default (consistent with
    # every other list); pages have no archival, so all tagged pages show. Both sides
    # are gated by visibility: an issue in a project / a page in a space the viewer
    # can't see never appears in the hub (admins see all).
    user = getattr(request.state, "user", None)
    tagged_issues = issues.list_issues(
        conn,
        ids=labels.issue_ids_for_label(conn, name),
        visible_project_ids=access.visible_project_filter(conn, user),
    )
    tagged_pages = [
        page
        for pid in labels.page_ids_for_label(conn, name)
        if (page := pages.get_page(conn, pid)) is not None
        and access.can_see_space(conn, user, page["space_id"])
    ]
    return templates.TemplateResponse(
        request=request,
        name="labels/detail.html",
        context={
            "name": name,
            "label": label,
            "issues": tagged_issues,
            "pages": tagged_pages,
        },
    )
