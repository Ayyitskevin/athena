"""Browser routes for one issue: graph, mention linking, detail, and history.

Graph and mention routes are registered before ``GET /aegis/issues/{ref}``.
The list router's ``GET /aegis/issues/new`` is mounted first.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import (
    issue_commands,
    issue_history,
    issue_narrative,
    issues,
)
from athena.core import (
    access,
    activity,
    graph,
    identity,
    mentions,
)
from athena.core.deps import get_conn
from athena.web.browsing import (
    int_or_none,
    readonly_response,
)
from athena.web.csrf import verify_csrf
from athena.web.issue_html import (
    issue_command_response,
    issue_visible_or_404,
    render_issue_detail,
)
from athena.web.router import get_templates

router = APIRouter()


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
