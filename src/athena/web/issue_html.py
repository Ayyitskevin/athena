"""HTML helpers shared by the issue browser routes.

The list and detail pages and the action forms both render the same detail
page and the same command refusal. Those helpers are public so the route
modules do not import private names from each other.
"""

from __future__ import annotations

import html
import sqlite3

from fastapi import Request
from fastapi.responses import HTMLResponse

from athena.aegis import (
    claim_handoffs,
    comments,
    contributors,
    dependencies,
    issue_commands,
    issues,
    projects,
    rollups,
    sprints,
    statuses,
)
from athena.core import (
    access,
    activity,
    attachments,
    event_sources,
    identity,
    labels,
    links,
    notifications,
    users,
)
from athena.web.render import render_comment, render_issue_body
from athena.web.router import get_templates


def issue_command_response(
    exc: issue_commands.IssueCommandError,
) -> HTMLResponse:
    """Translate a shared issue-command rejection at the HTML boundary."""
    status_code = {
        "unauthorized": 401,
        "forbidden": 403,
        "not_found": 404,
        "invalid": 400,
        "conflict": 409,
    }[exc.kind]
    css_class = (
        "blocked" if exc.kind in {"unauthorized", "forbidden", "conflict"} else "error"
    )
    return HTMLResponse(
        f'<div class="{css_class}">{html.escape(exc.detail.capitalize())}.</div>',
        status_code=status_code,
    )


def authorize_issue_write(conn, issue_id, user):
    """Return (issue, None) if the session user may modify this issue, else
    (None, HTMLResponse) with the right status. 404 if no such issue, 403 if the
    user is neither its creator nor its current assignee. Delegates to the shared
    command policy owner; the 401 (logged-out) check stays at each call site."""
    try:
        return issue_commands.get_writable_issue(
            conn, actor=user, issue_id=issue_id
        ), None
    except issue_commands.IssueCommandError as exc:
        return None, issue_command_response(exc)


def issue_visible_or_404(conn, issue_id, user):
    """Return (issue, None) if the user may SEE this issue, else (None, 404 response).
    The visibility-ONLY gate for additive web writes (comments, attachments) and the
    personal watch toggle — any writer may act on a VISIBLE issue, so this stops short of
    the creator/assignee check authorize_issue_write applies. A hidden issue reads as
    "not found", so its existence never leaks through a write path."""
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, user, issue["project_id"]
    ):
        return None, HTMLResponse(
            '<div class="error">Issue not found.</div>', status_code=404
        )
    return issue, None


def render_issue_detail(
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
    can_modify = user is not None and can_write and issues.can_act_on(conn, issue, user)
    comment_rows = comments.list_comments(conn, issue_id)
    for comment in comment_rows:
        comment["body_html"] = render_comment(conn, comment["body"])
    # Gate children by viewer visibility (a child can sit in a private project the
    # viewer isn't in), matching the JSON API — else the detail page leaks it.
    visible_project_ids = access.visible_project_filter(conn, user)
    children = issues.list_children(
        conn, issue_id, visible_project_ids=visible_project_ids
    )
    visible_projects = projects.list_projects(conn, visible_project_ids)
    visible_project_names = {
        project["id"]: project["name"] for project in visible_projects
    }
    placement_sprints = [
        {**sprint, "project_name": visible_project_names[sprint["project_id"]]}
        for sprint in sprints.list_sprints(conn)
        if sprint["project_id"] in visible_project_names
    ]

    context = {
        "issue": issue,
        "body_html": render_issue_body(conn, issue["body"], actor=user),
        # "Referenced by" hides sources in projects/spaces the viewer can't see.
        "backlinks": links.backlinks(conn, "issue", issue_id, actor=user),
        # Typed dependencies, gated like the backlinks: a blocks/relates edge to an
        # issue in a private project the viewer can't see is dropped, so its key/title
        # never leaks through this issue's relationship list.
        "links": dependencies.list_links(conn, issue_id, actor=user),
        "comments": comment_rows,
        "attachments": attachments.list_for(conn, "issue", issue_id),
        # The types the download route serves inline — the template offers a
        # thumbnail and an embed snippet for exactly these, so the affordance
        # and the actual behaviour come from one list.
        "inline_image_types": attachments.INLINE_CONTENT_TYPES,
        "is_watching": user is not None
        and notifications.is_watching(conn, user["id"], "issue", issue_id),
        "users": users.list_users(conn),
        "contributors": contributors.list_contributors(conn, issue_id),
        "open_claim_handoff": claim_handoffs.get_open_handoff(conn, issue_id),
        "issue_labels": labels.labels_for_issue(conn, issue_id),
        "all_labels": labels.list_labels(conn),
        "all_projects": visible_projects,
        # Paired placement can target any sprint in a project the viewer may see.
        # Project names keep the no-JavaScript selector unambiguous without leaking
        # private project vocabulary.
        "placement_sprints": placement_sprints,
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
        # One owner for the number: the same rollup the embed resolves, so the
        # page and a dashboard-in-a-page can never disagree about progress.
        "rollup": rollups.child_rollup(
            conn, issue_id, visible_project_ids=visible_project_ids
        ),
        "can_modify": can_modify,
        "can_write": can_write,
        # Admins may moderate (delete) any comment, not just their own — drives the
        # per-comment Delete control the same way the server-side override gates it.
        "is_admin": user is not None and identity.is_admin(user),
        # This issue's own audit trail (newest first) — the same data-layer read
        # the REST feed serves, scoped to this target.
        "activity": activity.list_activity(
            conn, target_kind="issue", target_id=issue_id, actor=user
        ),
        # Hosts Athena was told to expect events from. The trail links a forge
        # URL only when its host is one of these — otherwise anyone holding a
        # source secret could plant an arbitrary outbound link on an issue.
        "forge_hosts": event_sources.registered_hosts(conn),
    }
    if extra:
        context.update(extra)
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/issue_detail.html",
        context=context,
        status_code=status_code,
    )
