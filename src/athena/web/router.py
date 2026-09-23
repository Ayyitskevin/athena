"""HTML page routes (APIRouter).

The actual page rendering logic lives here. The Jinja2Templates instance
is configured in main.py (per wiring contract) and injected via init_templates.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from athena.aegis import (
    dashboard,
    delegations,
    fleet_attention,
    issue_search,
    issues,
    projects,
)
from athena.core import (
    access,
    activity,
    identity,
    labels,
    notifications,
    search,
)
from athena.core.deps import get_conn
from athena.web import live
from athena.web.csrf import verify_csrf
from athena.web.render import (
    render_snippet,
)

# Attachment-command refusal codes this browser adapter answers with.

router = APIRouter()

# Populated at app startup from main.py wiring.
_templates: Jinja2Templates | None = None


def init_templates(templates: Jinja2Templates) -> None:
    """Receive the configured Jinja2Templates from the app factory."""
    global _templates
    _templates = templates


def get_templates() -> Jinja2Templates:
    """The configured templates instance, for other web routers (e.g. auth)."""
    if _templates is None:
        raise RuntimeError("web templates have not been initialized")
    return _templates


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Simple landing page. No dynamic data yet — just the foundation."""
    return get_templates().TemplateResponse(
        request=request,
        name="home.html",
    )


@router.get("/aegis/dashboard", response_class=HTMLResponse)
def aegis_dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """A live overview of the work: headline totals, the issue distribution by status
    and priority, per-project and backlog counts, what's in flight (active sprints),
    the signed-in user's assignee plate and delegation inbox, and the latest activity.
    Read-only — aggregate numbers come from aegis.dashboard and delegated work comes
    from aegis.delegations, so this route only lays them out. Open to read, like the
    issue list."""
    user = getattr(request.state, "user", None)
    # The steer-by-exception rollup, admin-only because every input is already an
    # admin-scoped read. It is counts and links only — it computes no state of its
    # own, so it can never disagree with the surfaces it points at.
    attention = (
        fleet_attention.build_attention(conn, actor=user)
        if user is not None and identity.is_admin(user)
        else None
    )
    live_refresh = live.build(request, live.FLEET_ATTENTION)
    # The attention card refreshes itself, and answers here before the rest of the
    # dashboard is built: a poll wants one card, and every ten seconds is the wrong
    # cadence at which to also count the whole board and read a delegation inbox
    # nobody is looking at. The gate is the same one the page uses — a non-admin
    # has `attention = None`, renders no card and so has no poll to fire, and asking
    # for the panel directly gets the same nothing rather than the card.
    if live.wants_panel(request, live.FLEET_ATTENTION):
        if attention is None:
            return HTMLResponse("")
        return get_templates().TemplateResponse(
            request=request,
            name="aegis/partials/fleet_attention.html",
            context={"fleet_attention": attention, "live": live_refresh},
        )
    # Every number is counted only over the projects this viewer may see (admins all;
    # the backlog is always in). recent_activity is gated the same way: actor=user
    # makes list_activity drop events whose target the viewer can't see.
    vis = access.visible_project_filter(conn, user)
    delegation_inbox = (
        delegations.list_delegations(conn, user, limit=8) if user else None
    )
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/dashboard.html",
        context={
            "live": live_refresh,
            "totals": dashboard.totals(conn, vis),
            "status_counts": dashboard.status_counts(conn, vis),
            "priority_counts": dashboard.priority_counts(conn, vis),
            "projects": dashboard.project_open_counts(conn, vis),
            "backlog_count": dashboard.backlog_count(conn),
            "active_sprints": dashboard.active_sprints(conn, vis),
            # The user's own plate only makes sense when we know who they are.
            "my_issues": dashboard.my_open_issues(
                conn, user["id"], visible_project_ids=vis
            )
            if user
            else [],
            "my_delegation_inbox": delegation_inbox,
            "fleet_attention": attention,
            "recent_activity": activity.list_activity(conn, limit=10, actor=user),
        },
    )


_SEARCH_PAGE = 20
_MAX_SEARCH_PAGE = search.MAX_OFFSET // _SEARCH_PAGE + 1


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
        page = min(
            _MAX_SEARCH_PAGE,
            max(1, int(request.query_params.get("page", 1))),
        )
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
        # the href maps a hit's kind to where it lives in the web UI. A comment has no
        # page of its own — it lives on its PARENT issue/page (parent_id from enrichment),
        # so a comment hit links there, not to a bogus /mentor/pages/{comment_id}.
        h["snippet_html"] = render_snippet(h.get("snippet"))
        if h["kind"] == "issue":
            h["href"] = f"/aegis/issues/{h['source_id']}"
        elif h["kind"] == "page":
            h["href"] = f"/mentor/pages/{h['source_id']}"
        elif h["kind"] == "issue_comment":
            h["href"] = f"/aegis/issues/{h.get('parent_id')}"
        else:  # page_comment
            h["href"] = f"/mentor/pages/{h.get('parent_id')}"

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

    return get_templates().TemplateResponse(
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
                {
                    i["status"]
                    for i in issues.list_issues(
                        conn,
                        visible_project_ids=access.visible_project_filter(conn, user),
                    )
                }
            ),
            "all_labels": labels.list_labels(conn),
            "all_projects": projects.list_projects(
                conn, access.visible_project_filter(conn, user)
            ),
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
    return get_templates().TemplateResponse(
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
    notifications.mark_read(conn, user["id"], notification_id, actor=user)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/read-all", dependencies=[Depends(verify_csrf)])
def mark_inbox_all_read(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Mark every notification read, then back to the inbox."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    notifications.mark_all_read(conn, user["id"], actor=user)
    return RedirectResponse("/inbox", status_code=303)


@router.get("/aegis", response_class=HTMLResponse)
def aegis(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Aegis dashboard using real data from list_issues."""

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
    return get_templates().TemplateResponse(
        request=request,
        name="aegis.html",
        context={
            "status_counts": dict(status_counts),
            "recent_issues": recent_issues,
            "total_issues": len(all_issues),
            "can_write": can_write,
        },
    )
