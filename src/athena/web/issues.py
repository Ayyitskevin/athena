"""Browser routes for the Aegis issue pages: list, create, edit, detail, history.

Split out of the issue browser so the field writes and the discussion writes
live beside it. A thin client: mutations go through an Aegis command. Shared
HTML helpers live in web.issue_html; shared list helpers live in web.browsing.
"""

from __future__ import annotations

import html
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import (
    issue_commands,
    issue_drafts,
    issue_etags,
    issue_history,
    issue_narrative,
    issues,
    projects,
    sprints,
)
from athena.core import (
    access,
    activity,
    graph,
    identity,
    labels,
    mentions,
    users,
)
from athena.core.deps import get_conn
from athena.web.browsing import (
    attach_labels,
    int_or_none,
    readonly_response,
    statuses_in_use,
)
from athena.web.csrf import verify_csrf
from athena.web.issue_html import (
    authorize_issue_write,
    issue_command_response,
    issue_visible_or_404,
    render_issue_detail,
)
from athena.web.render import (
    MAX_PREVIEW_CHARS,
    render_issue_body,
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


@router.get("/aegis/issues/{issue_id}/edit", response_class=HTMLResponse)
def edit_issue_form(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the edit form for an issue, prefilled with its current title/body.
    Gated on the session user — editing is a write, so logged-out callers get a
    sign-in prompt rather than a form they can't submit."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    issue, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/issue_edit.html",
        context=_issue_edit_context(
            conn,
            issue,
            user,
            restored=request.query_params.get("restore") == "1",
            notice=request.query_params.get("notice", ""),
        ),
    )


def _issue_edit_context(
    conn: sqlite3.Connection,
    issue: dict,
    user: dict,
    *,
    restored: bool = False,
    notice: str = "",
    conflict: dict | None = None,
) -> dict:
    """The issue edit form's context, built in one place so opening the form and
    the losing side of a concurrent save cannot disagree about the baseline.

    ``issue`` is always the REAL current row, because the ETag is derived from
    it — the form now shows the saved issue in every case, exactly as the page
    editor does, because the author's own text is safe in ``issue_drafts``
    (0074). An unsaved draft is OFFERED, never applied: ``restored`` re-renders
    the fields with the draft's text, and nothing is written until Save."""
    draft = issue_drafts.get_draft(conn, issue_id=issue["id"], owner_id=user["id"])
    if draft is not None and not issue_drafts.differs_from(draft, issue):
        # Identical to the saved issue: not unsaved work, so offering to restore
        # it would just make an author wonder what they had forgotten.
        draft = None
    return {
        "issue": issue,
        "body_html": render_issue_body(conn, issue["body"] or "", actor=user),
        "issue_etag": issue_etags.current_etag(conn, issue),
        "draft": draft,
        "restored": restored and draft is not None,
        "draft_is_stale": draft is not None
        and issue_drafts.is_stale(draft, issue_etags.current_etag(conn, issue)),
        "notice": notice,
        # The author's unsaved text, shown for comparison when this render is a
        # refusal — the page editor's shape, now that issues have a draft store.
        "conflict": conflict,
    }


@router.post("/aegis/issues/{issue_id}/edit", dependencies=[Depends(verify_csrf)])
def edit_issue(
    request: Request,
    issue_id: int,
    title: str = Form(""),
    body: str = Form(""),
    if_match: str = Form(""),
    based_on: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save edits to an issue's title and body from the edit form. Gated on the
    session user (same actor rule as every write), rejects an empty title, then
    303-redirects back to the issue so it reloads with the new content.

    The form carries the issue's ETag as rendered, so a save that would land on
    top of someone else's is refused rather than silently winning — the browser
    half of the optimistic lock REST and MCP already had."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    # An empty if_match means a form rendered before this field existed (a tab
    # left open across the upgrade). Those keep the old last-write-wins behavior
    # rather than being refused over a field their author cannot see or fix.
    try:
        issue_commands.update_issue(
            conn,
            actor=user,
            issue_id=issue_id,
            title=title,
            body=body,
            if_match=[if_match] if if_match.strip() else None,
        )
    except issue_commands.IssueCommandError as exc:
        if exc.kind == "precondition_failed":
            return _issue_conflict_response(
                conn,
                request,
                issue_id,
                user,
                title=title,
                body=body,
                based_on=based_on,
            )
        if exc.kind in ("invalid_precondition", "precondition_too_large"):
            # A tampered or malformed hidden field is not an authorization
            # signal; treat it as no precondition rather than walling an author
            # out of their own issue.
            try:
                issue_commands.update_issue(
                    conn, actor=user, issue_id=issue_id, title=title, body=body
                )
            except issue_commands.IssueCommandError as retry_exc:
                return issue_command_response(retry_exc)
        else:
            return issue_command_response(exc)
    # The text IS the issue now, so the author's draft of it is a stale copy of
    # something that finally has a real home on the trail. Dropping it is what
    # makes "you have unsaved work" mean it the next time it appears.
    issue_drafts.discard_draft(conn, issue_id=issue_id, owner_id=user["id"])
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


def _issue_conflict_response(
    conn: sqlite3.Connection,
    request: Request,
    issue_id: int,
    user: dict,
    *,
    title: str,
    body: str,
    based_on: str,
) -> HTMLResponse:
    """The losing editor's answer for an issue — the PAGE editor's answer now.

    This path used to invert the page equivalent because issues had no draft
    store: the loser's text stayed in the fields, admitted to being unstored,
    and navigating away lost it. ``issue_drafts`` (0074) erases that asymmetry.
    The loser's text is written to their own draft first, the fields show the
    winner's version — the issue as it stands — and restoring is one click.
    Nothing is overwritten, nothing is merged, and nothing is lost.

    The draft keeps the baseline the author was editing FROM (the form's
    ``based_on``), never the issue's new tag — stamping today's tag would mark
    stale work fresh and silence the warning at the one moment it exists for.
    The re-rendered form carries the CURRENT tag, so saving again deliberately
    overwrites instead of looping on the same refusal.
    """
    current = issues.get_issue(conn, issue_id)
    if current is None:
        # It was deleted, not edited, between the precondition and this read.
        return HTMLResponse(
            '<div class="error">Issue not found.</div>', status_code=404
        )
    issue_drafts.save_draft(
        conn,
        issue_id=issue_id,
        owner_id=user["id"],
        title=title,
        body=body,
        based_on=based_on,
    )
    # The fields show what won; `conflict` carries what the author typed.
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/issue_edit.html",
        context=_issue_edit_context(
            conn,
            current,
            user,
            conflict={"title": title, "body": body},
        ),
        status_code=409,
    )


@router.post("/aegis/issues/{issue_id}/draft", dependencies=[Depends(verify_csrf)])
def autosave_issue_draft(
    request: Request,
    issue_id: int,
    title: str = Form(""),
    body: str = Form(""),
    based_on: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Record where this author has got to, without touching the issue.

    The mentor autosave's Aegis twin: nothing here writes to ``issues`` — no
    activity event, no watcher notified, no lifecycle fact. A crashed browser
    should cost nothing, and the trail should still say nothing happened until
    a human decides something did. Gated exactly like the edit form itself
    (creator or current assignee), because a draft OF a write belongs only to
    someone who could perform the write.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    issue, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    try:
        saved = issue_drafts.save_draft(
            conn,
            issue_id=issue_id,
            owner_id=user["id"],
            title=title,
            body=body,
            # The etag the EDITOR RENDERED WITH, carried by the form — never
            # re-read here. Stamping the current etag at autosave time would
            # mark a draft fresh the moment someone else saved, which is the
            # exact moment the stale warning exists for. Blank only for a
            # cached pre-upgrade form; falling back to the current etag there
            # restores the old (weaker) behavior instead of refusing the save.
            based_on=based_on.strip() or issue_etags.current_etag(conn, issue),
        )
    except issue_drafts.DraftTooLarge:
        # Fixed literals from the module's own bounds, not the exception's text:
        # the message is identical in substance, and nothing exception-derived
        # reaches the response (CodeQL's stack-trace-exposure rule, honored the
        # strict way rather than suppressed).
        return HTMLResponse(
            '<div class="error">Draft not held — too large. Titles cap at '
            f"{issue_drafts.MAX_TITLE_CHARS} characters and bodies at "
            f"{issue_drafts.MAX_BODY_CHARS:,}.</div>",
            413,
        )
    return HTMLResponse(
        f'<span class="draft-saved">Draft held {html.escape(saved["updated_at"])}</span>'
    )


@router.post(
    "/aegis/issues/{issue_id}/draft/discard", dependencies=[Depends(verify_csrf)]
)
def discard_issue_draft(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Throw away this author's draft of this issue. Affects nobody else."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    issue_drafts.discard_draft(conn, issue_id=issue_id, owner_id=user["id"])
    # int() is redundant to FastAPI's own path coercion, but that coercion is
    # invisible to the URL-redirection taint analysis; making it explicit proves
    # the Location header cannot carry anything but digits.
    return RedirectResponse(
        f"/aegis/issues/{int(issue_id)}/edit?notice=Draft+discarded.",
        status_code=303,
    )


@router.post("/aegis/issues/preview", dependencies=[Depends(verify_csrf)])
def preview_issue_body(
    request: Request,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Render unsaved issue text exactly as the issue view will render it.

    Calls ``render_issue_body`` — the SAME function the issue page calls — so the
    two cannot drift. That includes the parts an author might wish were
    different: embeds are not resolved on issues, so a directive previews as its
    "not rendered here" box, because showing a live embed here would promise
    something the saved issue will not deliver.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to'
            " preview.</div>",
            status_code=401,
        )
    if len(body) > MAX_PREVIEW_CHARS:
        return HTMLResponse(
            '<div class="error">Too long to preview.</div>', status_code=413
        )
    return HTMLResponse(str(render_issue_body(conn, body, actor=user)))


@router.get("/aegis/issues/{issue_id}/graph", response_class=HTMLResponse)
def issue_graph(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """An issue's neighbourhood: the bounded link graph plus its unlinked mentions.

    A separate route rather than a panel on the issue, for the same reason the page
    version is: a mention scan and a graph walk are both per-view work that reading
    should not pay for.
    """
    user = getattr(request.state, "user", None)
    issue, err = issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    assert issue is not None
    return get_templates().TemplateResponse(
        request=request,
        name="knowledge.html",
        context={
            "subject_title": issue["title"],
            "back_url": f"/aegis/issues/{issue_id}",
            "target_kind": "issue",
            "target_id": issue_id,
            # The VIEWER's visibility, never the issue author's.
            "graph": graph.ego_graph(conn, kind="issue", node_id=issue_id, actor=user),
            "mentions": mentions.unlinked_mentions(
                conn, kind="issue", target_id=issue_id, actor=user
            ),
            "can_write": user is not None and identity.can_write(user),
        },
    )


@router.post(
    "/aegis/issues/{issue_id}/link-mention", dependencies=[Depends(verify_csrf)]
)
def link_issue_mention(
    request: Request,
    issue_id: int,
    target_kind: str = Form(...),
    target_id: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Rewrite THIS issue's body so its first unlinked mention of the target becomes
    a real reference — an ordinary issue edit, through the ordinary command."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to link.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return readonly_response()
    issue, err = issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    assert issue is not None
    if target_kind not in ("issue", "page"):
        return HTMLResponse('<div class="error">Unknown target.</div>', status_code=422)
    needle = mentions.mention_text(conn, target_kind, target_id)
    if not needle:
        return HTMLResponse(
            '<div class="error">That link target no longer exists.</div>',
            status_code=404,
        )
    body = mentions.linkify_first(
        issue["body"] or "", needle, mentions.link_token(conn, target_kind, target_id)
    )
    if body is None:
        # The mention the operator clicked is gone; refuse rather than rewrite
        # text they never saw.
        return HTMLResponse(
            '<div class="error">That mention is no longer in this issue.</div>',
            status_code=409,
        )
    try:
        issue_commands.update_issue(conn, actor=user, issue_id=issue_id, body=body)
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
    back = (
        f"/mentor/pages/{target_id}/graph"
        if target_kind == "page"
        else f"/aegis/issues/{target_id}/graph"
    )
    return RedirectResponse(back, status_code=303)


@router.get("/aegis/issues/{ref}", response_class=HTMLResponse)
def issue_detail(
    request: Request, ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    """Show a single issue, addressable by numeric id ("12") or project key
    ("ATH-12"). get_by_ref resolves either form; everything past it keys off the
    issue's real numeric id (backlinks/comments/labels stay numeric)."""

    issue = issues.get_by_ref(conn, ref)
    user = getattr(request.state, "user", None)
    # An issue in a private project the viewer can't see is treated exactly like a
    # missing one — same 404, so privacy never leaks through the existence of a ref.
    # Backlog issues (no project) read like a public one.
    if not issue or not access.can_see_project_or_backlog(
        conn, user, issue["project_id"]
    ):
        # not-found state: render empty list page with error (minimal). Carry a
        # real 404 status — a missing issue is not a 200, and the API surface for
        # the same id returns 404, so the browser path must not disagree.
        return get_templates().TemplateResponse(
            request=request,
            name="aegis/issues.html",
            context={
                "issues": [],
                "status_filter": "",
                "search": "",
                "error": f"Issue {ref} not found",
            },
            status_code=404,
        )

    return render_issue_detail(request, conn, issue)


@router.get("/aegis/issues/{ref}/history", response_class=HTMLResponse)
def issue_history_view(
    request: Request, ref: str, conn: sqlite3.Connection = Depends(get_conn)
):
    """Time-travel for one issue — the browser twin of GET /issues/{id}/state. Shows the
    issue's lifecycle state reconstructed AS OF a chosen checkpoint (?as_of=<event id>,
    default now), beside the timeline of its events, each a clickable cutoff. A thin
    client over issue_history.project_issue_state; gated exactly like the detail page (a
    hidden/missing issue is the same 404, so existence never leaks).

    The run narrative (issue_narrative.build_issue_narrative) is added alongside the
    existing snapshot and checkpoint list: it stitches claim, handoff, run-control, and
    check-in signals into one operator story without replacing the raw trail."""
    issue = issues.get_by_ref(conn, ref)
    user = getattr(request.state, "user", None)
    if not issue or not access.can_see_project_or_backlog(
        conn, user, issue["project_id"]
    ):
        return get_templates().TemplateResponse(
            request=request,
            name="aegis/issues.html",
            context={
                "issues": [],
                "status_filter": "",
                "search": "",
                "error": f"Issue {ref} not found",
            },
            status_code=404,
        )
    as_of = int_or_none(request.query_params.get("as_of"))
    try:
        snapshot = issue_history.project_issue_state(
            conn, issue["id"], as_of_event_id=as_of, actor=user
        )
    except issue_history.IncompleteIssueHistory:
        return HTMLResponse(
            '<div class="blocked">Complete issue history is not available.</div>',
            status_code=403,
        )
    except issue_history.IssueHistoryTooLarge:
        return HTMLResponse(
            '<div class="error">Issue history exceeds the exact projection limit.</div>',
            status_code=409,
        )
    # The issue's timeline (newest-first), each event a checkpoint to time-travel to.
    events = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"], actor=user
    )
    # The operator run narrative: claim/handoff/control/check-in story, read-only.
    narrative = issue_narrative.build_issue_narrative(conn, issue["id"], actor=user)
    if narrative is None:  # issue was read above; keep the projection fail-closed
        return HTMLResponse("Issue not found", status_code=404)
    narrative_event_ids = {
        item["source"]["id"]
        for item in narrative["items"]
        if item["source"]["kind"] == "activity"
    }
    timeline_items = []
    for item in narrative["items"]:
        source = item["source"]
        event_id = source["id"] if source["kind"] == "activity" else None
        action_href = (
            f"/aegis/issues/{issue['id']}/history?as_of={event_id}"
            if event_id is not None
            else item["via"].removeprefix("GET ")
        )
        timeline_items.append(
            {
                "kind": "narrative",
                "at": item["at"],
                "actor_name": None if item["actor"] is None else item["actor"]["name"],
                "summary": item["summary"],
                "signal": item["signal"],
                "source_label": f"{source['kind']} #{source['id']}",
                "action_href": action_href,
                "event_id": event_id,
            }
        )
    for event in events:
        if event["id"] in narrative_event_ids:
            continue
        timeline_items.append(
            {
                "kind": "event",
                "at": event["created_at"],
                "actor_name": event["actor_name"],
                "summary": (
                    f"{event['verb']} {event['detail']}"
                    if event["detail"]
                    else event["verb"]
                ),
                "signal": None,
                "source_label": f"activity #{event['id']}",
                "action_href": (
                    f"/aegis/issues/{issue['id']}/history?as_of={event['id']}"
                ),
                "event_id": event["id"],
            }
        )
    timeline_items.sort(
        key=lambda item: (str(item["at"]), str(item["source_label"])), reverse=True
    )
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/issue_history.html",
        context={
            "issue": issue,
            "snapshot": snapshot,
            "events": events,
            "as_of": as_of,
            "narrative": narrative,
            "timeline_items": timeline_items,
        },
    )
