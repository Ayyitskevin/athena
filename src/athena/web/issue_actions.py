"""Browser routes for the writes on one issue: status, placement, links, watch.

Split out of web/issues.py. The detail page they return to is rendered by
web.issue_html so this module does not import a private helper from the page
module.
"""

from __future__ import annotations

import html
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import (
    dependencies,
    issue_commands,
    issues,
    projects,
    statuses,
)
from athena.core import (
    notifications,
)
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.issue_html import (
    authorize_issue_write,
    issue_command_response,
    issue_visible_or_404,
    render_issue_detail,
)

router = APIRouter()


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
    page with a visibility-safe warning. The default project behavior remains an
    advisory confirmation. When the optional project policy is enabled, agents are
    refused and an eligible human must explicitly request the audited override."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to change status.</div>',
            status_code=401,
        )
    issue, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    closing = not statuses.is_done(
        conn, issue["project_id"], issue["status"]
    ) and statuses.is_done(conn, issue["project_id"], status)
    project = (
        projects.get_project(conn, issue["project_id"])
        if issue["project_id"] is not None
        else None
    )
    policy_enabled = bool(
        project and project.get("block_agent_closes_when_blocked", False)
    )
    confirming = confirm.strip() == "1"
    if closing and not confirming and not policy_enabled:
        # Gate the blocker warning by the viewer: a blocker in a private project they
        # can't see is omitted, so its key/title never leaks through the close warning.
        blockers = dependencies.open_blockers(conn, issue_id, actor=user)
        if blockers:
            # Don't apply the close — show the warning and let the user confirm.
            return render_issue_detail(
                request,
                conn,
                issue,
                extra={"blocked_warning": blockers, "pending_status": status},
            )
    try:
        issue_commands.update_issue(
            conn,
            actor=user,
            issue_id=issue_id,
            status=status,
            override_blocked_close=confirming,
        )
    except issue_commands.IssueCommandError as exc:
        if exc.code == issue_commands.BLOCKED_CLOSE_POLICY_ERROR_CODE:
            blockers = dependencies.open_blockers(conn, issue_id, actor=user)
            return render_issue_detail(
                request,
                conn,
                issue,
                extra={
                    "blocked_policy_warning": True,
                    "blocked_warning": blockers,
                    "pending_status": status,
                    "allow_blocked_override": not bool(user.get("is_agent")),
                },
                status_code=409,
            )
        return issue_command_response(exc)
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
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    try:
        issue_commands.update_issue(
            conn, actor=user, issue_id=issue_id, priority=priority
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


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
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

    assignee_id = assignee_id.strip()
    if assignee_id == "":
        target: int | None = None
    else:
        try:
            target = int(assignee_id)
        except ValueError:
            return HTMLResponse(
                '<div class="error">Invalid user.</div>', status_code=400
            )

    try:
        issue_commands.update_issue(
            conn, actor=user, issue_id=issue_id, assignee_id=target
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
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
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

    project_id = project_id.strip()
    if project_id == "":
        target: int | None = None
    elif not project_id.isdigit():
        return HTMLResponse(
            '<div class="error">No such project.</div>', status_code=400
        )
    else:
        target = int(project_id)

    try:
        issue_commands.update_issue(
            conn, actor=user, issue_id=issue_id, project_id=target
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post("/aegis/issues/{issue_id}/placement", dependencies=[Depends(verify_csrf)])
def change_issue_placement(
    request: Request,
    issue_id: int,
    project_id: str = Form(""),
    sprint_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Set project and sprint as one placement transition from the detail page.

    Both form controls are submitted together so a user can move directly into a
    destination project sprint. Empty values explicitly clear their relationship;
    the command owns destination visibility, pair validation, status remapping,
    persistence, and audit in one transaction.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> '
            "to change placement.</div>",
            status_code=401,
        )
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

    project_id = project_id.strip()
    if project_id == "":
        project_target: int | None = None
    elif not project_id.isdigit():
        return HTMLResponse(
            '<div class="error">No such project.</div>', status_code=400
        )
    else:
        project_target = int(project_id)

    sprint_id = sprint_id.strip()
    if sprint_id == "":
        sprint_target: int | None = None
    elif not sprint_id.isdigit():
        return HTMLResponse('<div class="error">No such sprint.</div>', status_code=400)
    else:
        sprint_target = int(sprint_id)

    try:
        issue_commands.update_issue(
            conn,
            actor=user,
            issue_id=issue_id,
            project_id=project_target,
            sprint_id=sprint_target,
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
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
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err

    sprint_id = sprint_id.strip()
    if sprint_id == "":
        target: int | None = None
    elif not sprint_id.isdigit():
        return HTMLResponse('<div class="error">No such sprint.</div>', status_code=400)
    else:
        target = int(sprint_id)

    try:
        issue_commands.update_issue(
            conn, actor=user, issue_id=issue_id, sprint_id=target
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
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
    parent_ref = parent_ref.strip()
    if parent_ref == "":
        parent_id: int | None = None
    else:
        parent = issues.get_by_ref(conn, parent_ref)
        # An unresolvable ref is a web parsing failure (400); a ref that resolves to
        # a HIDDEN issue is caught by the command's see-the-parent check, which
        # collapses to the same "No such parent issue." — no existence probe.
        if parent is None:
            return HTMLResponse(
                '<div class="error">No such parent issue.</div>', status_code=400
            )
        parent_id = parent["id"]
    # The command owns the see-the-parent check, self/cycle validation, the write,
    # and the atomic audit event — the same one the REST route calls.
    try:
        issue_commands.set_issue_parent(
            conn, actor=user, issue_id=issue_id, parent_id=parent_id
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
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
    name = name.strip()
    if not name:
        return HTMLResponse(
            '<div class="error">Label name is required.</div>', status_code=400
        )
    # One command owns the whole write: the gate FIRST, then find-or-create, the
    # attach, and its atomic 'labeled' event — one transaction, like REST. When
    # the find-or-create ran here (before the gate), a refused request still
    # grew the shared vocabulary.
    try:
        issue_commands.attach_label_by_name(
            conn, actor=user, issue_id=issue_id, name=name
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/labels/{label_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
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
    try:
        issue_commands.detach_label(
            conn, actor=user, issue_id=issue_id, label_id=label_id
        )
    except issue_commands.IssueCommandError as exc:
        # A label that isn't attached is a no-op in the UI (double-submit) — land
        # back on the issue rather than 404. Real gate failures still surface.
        if exc.detail == "label not on this issue":
            return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)
        return issue_command_response(exc)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/contributors", dependencies=[Depends(verify_csrf)]
)
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
    try:
        target = int(user_id)
    except ValueError:
        return HTMLResponse('<div class="error">Pick a user.</div>', status_code=400)
    # The command owns the gate, the user-exists check, the add + auto-watch, and
    # the atomic 'added_contributor' event — the same one REST calls.
    try:
        issue_commands.add_contributor(
            conn, actor=user, issue_id=issue_id, user_id=target
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
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
    try:
        issue_commands.remove_contributor(
            conn, actor=user, issue_id=issue_id, user_id=user_id
        )
    except issue_commands.IssueCommandError as exc:
        # Not a contributor → no-op redirect (double-submit); real gate failures
        # still surface as the shared HTML rejection.
        if exc.detail == "not a contributor on this issue":
            return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)
        return issue_command_response(exc)
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
    # Visibility-first gate on THIS issue (hidden -> 404 even for a viewer) before the
    # command owns the target check, the edge, and — new — the atomic audit event.
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    try:
        issue_commands.link_issues(
            conn,
            actor=user,
            issue_id=issue_id,
            target_ref=target_ref.strip(),
            relation=relation,
        )
    except issue_commands.IssueCommandError as exc:
        # Only the target/relation validation and the block-each-other contradiction
        # reach here (the issue gate ran above); render the raw reason at 400, exactly
        # as the pre-command handler did.
        return HTMLResponse(
            f'<div class="error">{html.escape(exc.detail)}</div>', status_code=400
        )
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
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    try:
        issue_commands.unlink_issues(
            conn, actor=user, issue_id=issue_id, target_id=target_id, relation=relation
        )
    except issue_commands.IssueCommandError:
        # Removing a link that isn't there is not an error on the form path (its
        # buttons only render for edges that exist); redirect either way, as before.
        pass
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
    # The command owns the gate, the soft-delete, and its atomic 'archived' event.
    try:
        issue_commands.set_issue_archived(
            conn, actor=user, issue_id=issue_id, archived=True
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
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
    try:
        issue_commands.set_issue_archived(
            conn, actor=user, issue_id=issue_id, archived=False
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_response(exc)
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
    _, err = issue_visible_or_404(conn, issue_id, user)
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
    _, err = issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    notifications.unwatch(conn, user["id"], "issue", issue_id)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)
