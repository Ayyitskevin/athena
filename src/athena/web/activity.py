"""Web routes for the Aegis activity timeline — feed, CSV export, and run views.

Split out of web/router.py (the god-file) to keep each web surface navigable,
following the same one-module-per-area pattern as web/projects.py, web/mentor.py,
web/admin.py, and web/labels.py. Its own APIRouter, mounted by main.py. A thin
client over core.activity — it owns no data. The template accessor and the shared
int-parse helper are imported from web.router (get_templates reads the Jinja
instance main.py injects at startup).
"""
from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from athena.core import activity
from athena.core.deps import get_conn
from athena.web.router import _int_or_none, get_templates

router = APIRouter()


_FEED_PAGE = 50
_EXPORT_LIMIT = 1000


def _actor_type_or_none(raw: str | None) -> str | None:
    """Normalize the actor-type lens to 'agent', 'human', or None. An unknown value
    (e.g. a hand-edited URL) falls back to no filter rather than erroring — the same
    lenient stance the verb/kind selects take with input outside their option set."""
    value = (raw or "").strip().lower()
    return value if value in ("agent", "human") else None


def _activity_export_url(
    *,
    actor_id: int | None,
    actor_type: str | None,
    verb: str | None,
    kind: str | None,
    target_id: int | None,
    q: str,
) -> str:
    query = {
        "actor": actor_id or "",
        "actor_type": actor_type or "",
        "verb": verb or "",
        "kind": kind or "",
        "target": target_id or "",
        "q": q,
    }
    return f"/aegis/activity.csv?{urlencode(query)}"


@router.get("/aegis/activity", response_class=HTMLResponse)
def activity_feed(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The global activity timeline — the browser twin of GET /activity. Like the
    JSON API, the feed is gated by the viewer: an anonymous visitor sees only events
    on public targets, never an event on a private project's issue or a private
    space's page. It runs the SAME data-layer query the API serves, so the two never
    disagree.

    Supports the same filters (actor / verb / target kind) and a cursor (?before=)
    for paging back through history one page at a time."""

    actor_id = _int_or_none(request.query_params.get("actor"))
    actor_type = _actor_type_or_none(request.query_params.get("actor_type"))
    before_id = _int_or_none(request.query_params.get("before"))
    target_id = _int_or_none(request.query_params.get("target"))
    verb = (request.query_params.get("verb") or "").strip() or None
    kind = (request.query_params.get("kind") or "").strip() or None
    q = (request.query_params.get("q") or "").strip()
    if target_id is not None and kind is None:
        return HTMLResponse("<h1>Invalid activity target filter</h1>", status_code=400)

    # Fetch one extra row to know whether an older page exists without a count
    # query; if we got it, there's more — trim it off and remember the cursor. The
    # feed is gated by the viewer (anonymous → public targets only), so an event on a
    # private project's issue / a private space's page never shows to an outsider.
    user = getattr(request.state, "user", None)
    rows = activity.list_activity(
        conn,
        target_kind=kind,
        target_id=target_id,
        actor_id=actor_id,
        actor_is_agent=None if actor_type is None else actor_type == "agent",
        verb=verb,
        search=q,
        before_id=before_id,
        limit=_FEED_PAGE + 1,
        actor=user,
    )
    has_more = len(rows) > _FEED_PAGE
    events = rows[:_FEED_PAGE]
    next_before = events[-1]["id"] if has_more and events else None

    return get_templates().TemplateResponse(
        request=request,
        name="aegis/activity.html",
        context={
            "events": events,
            "all_users": activity.distinct_actors(conn, actor=user),
            "all_verbs": activity.distinct_verbs(conn, actor=user),
            "all_kinds": activity.distinct_target_kinds(conn, actor=user),
            "f_actor": actor_id,
            "f_actor_type": actor_type,
            "f_verb": verb,
            "f_kind": kind,
            "f_target": target_id,
            "f_q": q,
            "next_before": next_before,
            "export_url": _activity_export_url(
                actor_id=actor_id,
                actor_type=actor_type,
                verb=verb,
                kind=kind,
                target_id=target_id,
                q=q,
            ),
        },
    )


@router.get("/aegis/activity.csv")
def activity_export_csv(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Download the current activity filters as a CSV audit export."""
    actor_id = _int_or_none(request.query_params.get("actor"))
    actor_type = _actor_type_or_none(request.query_params.get("actor_type"))
    target_id = _int_or_none(request.query_params.get("target"))
    verb = (request.query_params.get("verb") or "").strip() or None
    kind = (request.query_params.get("kind") or "").strip() or None
    q = (request.query_params.get("q") or "").strip()
    if target_id is not None and kind is None:
        return HTMLResponse("<h1>Invalid activity target filter</h1>", status_code=400)

    user = getattr(request.state, "user", None)
    rows = activity.list_activity(
        conn,
        target_kind=kind,
        target_id=target_id,
        actor_id=actor_id,
        actor_is_agent=None if actor_type is None else actor_type == "agent",
        verb=verb,
        search=q,
        limit=_EXPORT_LIMIT,
        actor=user,
    )
    return Response(
        activity.to_csv(rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="athena-activity.csv"'
        },
    )


@router.get("/aegis/activity/runs", response_class=HTMLResponse)
def activity_runs(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The run-timeline view — the browser twin of GET /activity/runs. It reads one
    actor's recent activity reconstructed into runs (work sessions) from the SAME
    data-layer function the API serves, so the two never disagree. A run is a derived
    lens over the append-only trail, not a stored thing (see reconstruct_runs).

    Requires picking an actor (?actor=N) — a "runs" view mixing actors is meaningless,
    since a run is by definition one actor's uninterrupted stretch. With no actor it
    renders the picker and a hint; an unknown actor renders empty."""

    actor_id = _int_or_none(request.query_params.get("actor"))
    user = getattr(request.state, "user", None)
    visible_actors = activity.distinct_actors(conn, actor=user)
    selected_actor = next(
        (candidate for candidate in visible_actors if candidate["id"] == actor_id),
        None,
    )
    runs = (
        activity.reconstruct_runs(conn, actor_id=actor_id, actor=user)
        if selected_actor is not None
        else []
    )
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/activity_runs.html",
        context={
            "all_users": visible_actors,
            "f_actor": actor_id,
            "selected_actor": selected_actor,
            "runs": runs,
        },
    )


@router.get("/aegis/activity/runs/{run_id}/lineage", response_class=HTMLResponse)
def activity_run_lineage(
    run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """The lineage view for one tagged run — the browser twin of
    GET /activity/runs/{run_id}/lineage. Renders the run's causal tree from the SAME
    reconstruction the API serves: the originating goal down to it (ancestors), the run
    with its events, and the runs it spawned (descendants). Reads are gated by the
    viewer's visibility (an event on a target they can't see is dropped); a run with no
    visible events — or no such run — is a 404, so its existence never leaks."""
    user = getattr(request.state, "user", None)
    lineage = activity.run_lineage(conn, run_id, actor=user)
    if lineage is None:
        return HTMLResponse("<h1>No such run</h1>", status_code=404)
    fork_contracts = []
    for event in lineage["run"]["events"]:
        contract = activity.run_fork_contract(
            conn,
            run_id,
            fork_from_event_id=event["id"],
            fork_run_id=f"{run_id}:fork-{event['id']}",
            actor=user,
        )
        if contract is not None:
            fork_contracts.append(contract)
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/run_lineage.html",
        context={"lineage": lineage, "fork_contracts": fork_contracts},
    )
