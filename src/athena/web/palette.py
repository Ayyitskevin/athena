"""The gated command palette: keyboard-first operator actions over EXISTING commands.

The palette (Ctrl+K, static/palette.js) is a browser convenience, not an authority.
Every action it renders is computed server-side from the SAME predicates the owning
command enforces, and every action it runs is a thin transport adapter over that
command — the command re-checks authorization, owns the write, and records the one
activity event. Nothing here widens what the operator could already do from the
issue page, the REST API, or the admin cockpit.

Actions in this slice (MWS-18):

- capture  → ``issue_commands.create_issue`` (one issue, one 'created' event)
- claim    → ``lease_commands.claim_issue`` (exact issue ETag precondition, or 428/412)
- yield    → ``lease_commands.yield_claim`` (holder-only, exact lease generation)
- complete → ``lease_commands.complete_claim`` (holder-only, exact lease generation)
- approve  → ``approvals_api.decide_for_actor`` (admin-only, pending requests only)
- inspect  → read-only navigation to the issue's existing work-context page

Identity honesty: an unknown or invisible issue ref is a 404 the palette shows as a
visible refusal — the client never guesses a target. A stale ETag or lease
generation is the command's own 412/409 refusal, surfaced verbatim.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse

from athena.aegis import (
    issue_commands,
    issue_etags,
    issues,
    lease_commands,
    leases,
)
from athena.aegis.api import issue_command_status
from athena.core import access, approvals, approvals_api, identity
from athena.core.deps import get_conn
from athena.core.ids import RowIdPath
from athena.web.csrf import verify_csrf

router = APIRouter()

#: Pending approvals offered at once. The operator's full queue lives at
#: /admin/agents; the palette carries enough to steer by exception, never a page.
PENDING_APPROVALS_LIMIT = 10

_YIELD_REASONS = sorted(lease_commands.CLAIM_YIELD_REASONS)


def _signin_required() -> JSONResponse:
    return JSONResponse({"detail": "sign in required"}, status_code=401)


def _command_error(exc: issue_commands.IssueCommandError) -> JSONResponse:
    """The one refusal dialect: the command's kind, mapped at this boundary."""
    return JSONResponse(
        {"detail": exc.detail, "code": exc.kind},
        status_code=issue_command_status(exc),
    )


def _visible_issue_or_404(
    conn: sqlite3.Connection, actor: dict, issue_ref: str
) -> dict | JSONResponse:
    """Resolve an EXACT ref (numeric id or project key) or refuse visibly.

    Same collapse as the REST read: unknown and invisible both answer 404 so the
    palette never oracles a private issue's existence — and a clipped or partial
    ref simply resolves to nothing, never to a guess."""
    issue = issues.get_by_ref(conn, issue_ref)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        return JSONResponse({"detail": "no such issue"}, status_code=404)
    return issue


def _hidden(name: str, value: str) -> dict:
    return {"name": name, "type": "hidden", "value": value}


@router.get("/aegis/palette/actions")
def palette_actions(
    request: Request,
    issue_ref: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """The actions THIS actor may run in THIS context, and nothing else.

    The palette renders exactly this list; a permission the projection withholds
    is a button that never exists client-side. Read-only: no authority is granted
    here, only advertised — each action's endpoint re-decides."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required()
    actions: list[dict] = []
    if identity.can_write(user):
        actions.append(
            {
                "id": "capture",
                "label": "Capture issue",
                "endpoint": "/aegis/palette/capture",
                "fields": [
                    {
                        "name": "title",
                        "type": "text",
                        "label": "Title",
                        "required": True,
                    }
                ],
            }
        )
    issue_payload: dict | None = None
    lease_payload: dict | None = None
    if issue_ref.strip():
        resolved = _visible_issue_or_404(conn, user, issue_ref)
        if isinstance(resolved, JSONResponse):
            return resolved
        issue = resolved
        ref = issue["key"] or f"#{issue['id']}"
        issue_payload = {
            "id": issue["id"],
            "ref": ref,
            "title": issue["title"],
            "etag": issue_etags.current_etag(conn, issue),
        }
        actions.append(
            {
                "id": "inspect",
                "label": f"Inspect {ref}",
                "navigate": f"/aegis/issues/{issue['id']}/work-context",
            }
        )
        lease = leases.get_lease(conn, issue["id"])
        eligible_claimant = identity.can_write(
            user
        ) and lease_commands.claimant_is_eligible(conn, issue, user)
        if lease is not None and lease["active"]:
            if lease["holder_id"] != user["id"]:
                lease_payload = {
                    "active": True,
                    "holder_name": lease["holder_name"],
                    "expires_at": lease["expires_at"],
                }
            elif eligible_claimant:
                # The holder's lease generation is THEIRS — it fences their own
                # delayed mutations, so the actions read hands it back to them
                # (the REST lease read does the same). Never another actor's.
                lease_payload = {
                    "active": True,
                    "holder_name": lease["holder_name"],
                    "expires_at": lease["expires_at"],
                    "generation": lease["generation"],
                }
                actions.append(
                    {
                        "id": "yield",
                        "label": f"Yield / hand off {ref}",
                        "endpoint": f"/aegis/palette/issues/{issue['id']}/yield",
                        "fields": [
                            _hidden("generation", lease["generation"]),
                            {
                                "name": "reason",
                                "type": "select",
                                "label": "Reason",
                                "options": _YIELD_REASONS,
                                "required": True,
                            },
                            {
                                "name": "attempted_work",
                                "type": "text",
                                "label": "Attempted work",
                                "required": True,
                            },
                            {
                                "name": "blocking_question",
                                "type": "text",
                                "label": "Blocking question",
                                "required": True,
                            },
                            {
                                "name": "resume_instructions",
                                "type": "textarea",
                                "label": "Resume instructions",
                                "required": True,
                            },
                            {
                                "name": "note",
                                "type": "text",
                                "label": "Note (optional)",
                                "required": False,
                            },
                        ],
                    }
                )
                actions.append(
                    {
                        "id": "complete",
                        "label": f"Complete claim on {ref}",
                        "endpoint": f"/aegis/palette/issues/{issue['id']}/complete",
                        "fields": [_hidden("generation", lease["generation"])],
                    }
                )
        elif eligible_claimant:
            actions.append(
                {
                    "id": "claim",
                    "label": f"Claim {ref}",
                    "endpoint": f"/aegis/palette/issues/{issue['id']}/claim",
                    "fields": [_hidden("etag", issue_payload["etag"])],
                }
            )
    if identity.is_admin(user):
        pending = approvals.list_requests(
            conn, state="pending", limit=PENDING_APPROVALS_LIMIT
        )
        for req in pending:
            for decision, verb in (("approve", "Approve"), ("reject", "Reject")):
                actions.append(
                    {
                        "id": f"{decision}-{req.id}",
                        "label": f"{verb} {req.action_kind} (approval #{req.id})",
                        "endpoint": f"/aegis/palette/approvals/{req.id}/decision",
                        "fields": [
                            _hidden("decision", decision),
                            {
                                "name": "note",
                                "type": "text",
                                "label": "Note (optional)",
                                "required": False,
                            },
                        ],
                    }
                )
    return JSONResponse(
        {
            "actor": {"id": user["id"], "name": user["name"]},
            "issue": issue_payload,
            "lease": lease_payload,
            "actions": actions,
        }
    )


@router.post("/aegis/palette/capture", dependencies=[Depends(verify_csrf)])
def palette_capture(
    request: Request,
    title: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Quick-capture one issue through the create command — nothing more."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required()
    try:
        issue = issue_commands.create_issue(conn, actor=user, title=title)
    except issue_commands.IssueCommandError as exc:
        return _command_error(exc)
    return JSONResponse(
        {
            "id": issue["id"],
            "ref": issue["key"] or f"#{issue['id']}",
            "href": f"/aegis/issues/{issue['id']}",
        },
        status_code=201,
    )


@router.post(
    "/aegis/palette/issues/{issue_id}/claim", dependencies=[Depends(verify_csrf)]
)
def palette_claim(
    request: Request,
    issue_id: RowIdPath,
    etag: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Claim through the lease command, preconditioned on the exact ETag the
    palette advertised — a stale palette gets the command's own 412/428."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required()
    try:
        lease = lease_commands.claim_issue(
            conn, actor=user, issue_id=issue_id, if_match=[etag] if etag else None
        )
    except issue_commands.IssueCommandError as exc:
        return _command_error(exc)
    return JSONResponse(
        {
            "issue_id": lease["issue_id"],
            "holder_id": lease["holder_id"],
            "expires_at": lease["expires_at"],
            "generation": lease["generation"],
        },
        status_code=201,
    )


@router.post(
    "/aegis/palette/issues/{issue_id}/yield", dependencies=[Depends(verify_csrf)]
)
def palette_yield(
    request: Request,
    issue_id: RowIdPath,
    generation: str = Form(""),
    reason: str = Form(""),
    note: str = Form(""),
    attempted_work: str = Form(""),
    blocking_question: str = Form(""),
    resume_instructions: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Yield/hand off through the lease command: the lease release, the handoff
    record, and the run-stamped event commit as one unit — or not at all."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required()
    try:
        handoff = lease_commands.yield_claim(
            conn,
            actor=user,
            issue_id=issue_id,
            generation=generation or None,
            reason=reason,
            note=note.strip() or None,
            attempted_work=attempted_work,
            evidence=[],
            blocking_question=blocking_question,
            resume_instructions=resume_instructions,
        )
    except issue_commands.IssueCommandError as exc:
        return _command_error(exc)
    return JSONResponse(
        {
            "issue_id": handoff["issue_id"],
            "handoff_token": handoff["handoff_token"],
            "reason": handoff["reason"],
        }
    )


@router.post(
    "/aegis/palette/issues/{issue_id}/complete", dependencies=[Depends(verify_csrf)]
)
def palette_complete(
    request: Request,
    issue_id: RowIdPath,
    generation: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Release the caller's lease through the complete command. Status stays put —
    completion frees the claim, it does not close the issue."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required()
    try:
        released = lease_commands.complete_claim(
            conn, actor=user, issue_id=issue_id, generation=generation or None
        )
    except issue_commands.IssueCommandError as exc:
        return _command_error(exc)
    return JSONResponse(released)


@router.post(
    "/aegis/palette/approvals/{request_id}/decision",
    dependencies=[Depends(verify_csrf)],
)
def palette_decide_approval(
    request: Request,
    request_id: RowIdPath,
    decision: str = Form(""),
    note: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Decide one pending approval through the same core owner the admin cockpit
    and REST call. Approving opens the gate for one retry; it performs nothing."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required()
    return JSONResponse(
        approvals_api.decide_for_actor(
            conn,
            actor=user,
            request_id=request_id,
            decision=decision.strip(),
            note=note.strip() or None,
        )
    )
