"""Shared Aegis REST adapters.

Issue, project, and claim routes share the If-Match parser, the issue-command
status map, and the visibility read. Those helpers live here so a route module
does not import another route module's private names. This module does not
import ``aegis.api``: ``api`` imports it, then side-imports the route modules
that attach to its routers.
"""

from __future__ import annotations

import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse

from athena.aegis import issue_commands, issues
from athena.core import access
from athena.core.refusals import Refusal

_ISSUE_COMMAND_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "precondition_required": 428,
    "invalid_precondition": 400,
    "precondition_too_large": 431,
    "precondition_failed": 412,
    "lease_generation_required": 428,
    "invalid_lease_generation": 422,
    "lease_generation_mismatch": 409,
}
_ISSUE_COMMAND_CODE = {
    "unauthorized": "unauthenticated",
    "forbidden": "forbidden",
    "not_found": "not_found",
    "invalid": "validation_error",
    "conflict": "conflict",
    "precondition_required": "precondition_required",
    "invalid_precondition": "invalid_if_match",
    "precondition_too_large": "if_match_too_large",
    "precondition_failed": "precondition_failed",
    "lease_generation_required": "lease_generation_required",
    "invalid_lease_generation": "invalid_lease_generation",
    "lease_generation_mismatch": "lease_generation_mismatch",
}


def issue_command_status(exc: issue_commands.IssueCommandError) -> int:
    """The HTTP status this command rejection means.

    Public because the undo engine and the command palette need the same answer
    from outside the issue routes: one mapping, so the boundaries cannot drift.
    """
    return _ISSUE_COMMAND_STATUS[exc.kind]


def issue_command_code(exc: issue_commands.IssueCommandError) -> str:
    """The refusal code this command rejection answers with.

    A rejection that names its own code (a held claim, the blocked-close
    policy) keeps it; otherwise the kind's code, beside its status above."""
    return exc.code or _ISSUE_COMMAND_CODE[exc.kind]


def issue_command_http_error(
    exc: issue_commands.IssueCommandError,
) -> Refusal:
    """Translate a framework-free command rejection at the REST boundary."""
    return Refusal(
        status_code=issue_command_status(exc),
        detail=exc.detail,
        code=issue_command_code(exc),
    )


_PRECONDITION_HTTP = {
    "precondition_required": (428, "precondition_required"),
    "invalid_precondition": (400, "invalid_if_match"),
    "precondition_too_large": (431, "if_match_too_large"),
    "precondition_failed": (412, "precondition_failed"),
}

_LEASE_GENERATION_HTTP = {
    "lease_generation_required": (428, "lease_generation_required"),
    "invalid_lease_generation": (422, "invalid_lease_generation"),
    "lease_generation_mismatch": (409, "lease_generation_mismatch"),
}
_ISSUE_POLICY_HTTP = {
    issue_commands.BLOCKED_CLOSE_POLICY_ERROR_CODE: 409,
}


PRIVATE_LEASE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, X-Athena-Actor",
}


def _issue_precondition_response(
    exc: issue_commands.IssueCommandError,
) -> JSONResponse | None:
    """Render conditional-request failures with a stable code and current tag."""
    spec = _PRECONDITION_HTTP.get(exc.kind)
    if spec is None:
        return None
    status_code, code = spec
    headers = {}
    if exc.current_etag is not None:
        headers["ETag"] = exc.current_etag
    if exc.kind == "precondition_required":
        headers["Cache-Control"] = "no-store"
    return JSONResponse(
        status_code=status_code,
        content={"detail": exc.detail, "code": code},
        headers=headers,
    )


def issue_command_error_response(
    exc: issue_commands.IssueCommandError,
) -> JSONResponse:
    """Return a conditional failure or raise the route's ordinary HTTP error."""
    response = _issue_precondition_response(exc)
    if response is not None:
        return response
    generation_spec = _LEASE_GENERATION_HTTP.get(exc.kind)
    if generation_spec is not None:
        status_code, code = generation_spec
        return JSONResponse(
            status_code=status_code,
            content={"detail": exc.detail, "code": code},
            headers=PRIVATE_LEASE_HEADERS,
        )
    policy_status = _ISSUE_POLICY_HTTP.get(exc.code or "")
    if policy_status is not None:
        return JSONResponse(
            status_code=policy_status,
            content={"detail": exc.detail, "code": exc.code},
            headers=PRIVATE_LEASE_HEADERS,
        )
    raise issue_command_http_error(exc) from exc


def if_match_values(request: Request) -> list[str] | None:
    """Preserve every raw If-Match field line for standards-aware parsing."""
    values = [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"if-match"
    ]
    return values or None


CLAIM_IF_MATCH_OPENAPI = {
    "parameters": [
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "description": "Exactly one strong root issue ETag.",
            "schema": {"type": "string"},
        }
    ]
}


PROJECT_POLICY_PRECONDITION_HTTP = {
    "precondition_required": (428, "precondition_required"),
    "invalid_precondition": (400, "invalid_if_match"),
    "precondition_too_large": (431, "if_match_too_large"),
    "precondition_failed": (412, "precondition_failed"),
}

PROJECT_POLICY_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "precondition_required": 428,
    "invalid_precondition": 400,
    "precondition_too_large": 431,
    "precondition_failed": 412,
}
PROJECT_POLICY_CODE = {
    "not_found": "not_found",
    "forbidden": "forbidden",
    "precondition_required": "precondition_required",
    "invalid_precondition": "invalid_if_match",
    "precondition_too_large": "if_match_too_large",
    "precondition_failed": "precondition_failed",
}


def issue_for_read(conn: sqlite3.Connection, issue_id: int, actor: dict | None) -> dict:
    """Fetch an issue the actor may READ, or raise 404. The read counterpart of
    ``api._issue_for_write``: a missing issue and one in a private project the actor can't see
    are the same 404, so a sub-resource (comments/children/links/contributors/
    attachments) never leaks for a hidden issue. Backlog issues (no project) read like
    a public one. No write check — reads stay open within what's visible."""
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise Refusal(status_code=404, detail="no such issue", code="not_found")
    return issue
