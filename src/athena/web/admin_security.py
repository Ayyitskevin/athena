"""Browser routes for security signals and the run-control queue.

Split out of web/admin.py."""

from __future__ import annotations
import sqlite3
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from athena.core import (
    activity,
    activity_chain,
    run_control_commands,
    run_controls,
    security_events,
)
from athena.core.deps import get_conn
from athena.web import live
from athena.web.router import get_templates

from athena.web.admin import (
    _ACTIVE_WORK_PRIVATE_HEADERS,
    _CONTROL_STATE_FILTERS,
    _admin_required,
)

router = APIRouter()


@router.get("/admin/security", response_class=HTMLResponse)
def security_signals(
    request: Request,
    verb: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Boundary refusals — the probing an operator should see before a compromise.

    These events have always been on the trail; what was missing was a place to
    read them without already knowing the four verb names. Admin-only, because a
    list of who has been probing is operator intelligence."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    selected = verb if verb in security_events.SECURITY_VERBS else None
    return templates.TemplateResponse(
        request=request,
        name="admin/security.html",
        context={
            "events": security_events.list_failures(conn, verb=selected, limit=100),
            "counts": security_events.failure_counts(conn),
            "verbs": list(security_events.SECURITY_VERBS),
            "selected_verb": selected,
            # Trail integrity (0072): where the hash chain stands, plus a cheap
            # tail recheck for THIS render. The card says exactly what the tail
            # check covers; the full walk belongs to athena-doctor / the API.
            "chain": activity_chain.status(conn),
            "chain_tail": activity_chain.verify_tail(conn),
        },
        headers=_ACTIVE_WORK_PRIVATE_HEADERS,
    )


@router.get("/admin/run-controls", response_class=HTMLResponse)
def run_controls_admin(
    request: Request,
    state: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Every recorded control request in one place — the page the
    fleet-attention rollup's "Run controls awaiting an agent" count links to.

    Until now controls lived only on each run's lineage page, so an operator
    had to already know which runs they had steered. Admin-only, like the rest
    of the fleet cockpit; the bound agent's own inbox stays the API/MCP list.
    This page renders the same command read those serve — it owns no data."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    assert user is not None, "_admin_required accepted a missing user"
    # A hand-edited filter falls back to the default rather than erroring —
    # the security page's lenient stance for GET filters.
    selected = state if state in _CONTROL_STATE_FILTERS else "open"
    controls = run_control_commands.readable_controls(
        conn,
        actor=user,
        state=None if selected == "all" else selected,
        limit=run_controls.MAX_LIST_LIMIT,
    )
    # A heartbeat-only run is steerable (the check-in fallback admits it) but
    # has NO lineage page: that page is built from activity events, and a run
    # whose agent only checked in has none. Linking there anyway would hand the
    # operator a 404 from their own cockpit, so each row learns whether its run
    # has a page before the template decides to link it.
    linkable = {
        control["run_id"]
        for control in controls
        if activity.run_lineage(conn, control["run_id"], actor=user) is not None
    }
    context = {
        "controls": controls,
        "linkable_runs": linkable,
        "states": list(_CONTROL_STATE_FILTERS),
        "selected_state": selected,
        "clipped": len(controls) == run_controls.MAX_LIST_LIMIT,
        "limit": run_controls.MAX_LIST_LIMIT,
        "live": live.build(request, live.RUN_CONTROLS),
    }
    # Refreshes itself through this same route, so the state filter survives and the
    # admin gate cannot be bypassed by asking for the panel directly.
    name = (
        "admin/partials/run_controls_list.html"
        if live.wants_panel(request, live.RUN_CONTROLS)
        else "admin/run_controls.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        headers=_ACTIVE_WORK_PRIVATE_HEADERS,
    )
