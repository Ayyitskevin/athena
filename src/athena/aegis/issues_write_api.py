"""Issue create and field-write routes for the Aegis REST API.

Attaches to the issue router in ``aegis.api``. Imported after the read routes
so ``GET /issues/search`` is already registered before ``GET /issues/{ref}``.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from athena.aegis import issue_commands, lease_commands
from athena.core import mentions
from athena.aegis.api import (
    AssigneeUpdate,
    BulkUpdate,
    BulkUpdateOut,
    CompleteClaimOut,
    IssueCreate,
    IssueOut,
    IssueUpdate,
    LeaseGenerationIn,
    LinkMentionIn,
    ParentUpdate,
    ProjectUpdate,
    SprintAssign,
    router,
)
from athena.aegis.rest_support import (
    PRIVATE_LEASE_HEADERS,
    if_match_values,
    issue_command_error_response,
    issue_command_http_error,
    issue_for_read,
    tagged_issue,
    with_labels,
)
from athena.core.deps import get_conn
from athena.core.ids import RowIdPath
from athena.core.identity import issue_write_actor


@router.post("", response_model=IssueOut, status_code=201)
def create(
    payload: IssueCreate,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The shared command owns actor/target authorization, normalization,
    # validation, persistence, projections, and the required audit event.
    try:
        issue = issue_commands.create_issue(
            conn,
            actor=actor,
            title=payload.title,
            body=payload.body,
            status=payload.status,
            priority=payload.priority,
            project_id=payload.project_id,
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    return tagged_issue(conn, issue, response)


@router.post("/{issue_id}/link-mention", response_model=IssueOut)
def link_issue_mention(
    issue_id: RowIdPath,
    payload: LinkMentionIn,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Rewrite THIS issue's body so its first unlinked mention of the target becomes
    # a reference. The endpoint lives on the source's own domain because the source
    # is what gets edited — and it goes through update_issue, so the edit carries
    # the same authorization, projections, and audit as any other issue edit.
    issue = issue_for_read(conn, issue_id, actor)
    if payload.target_kind not in ("issue", "page"):
        raise HTTPException(status_code=422, detail="target_kind must be issue or page")
    needle = mentions.mention_text(conn, payload.target_kind, payload.target_id)
    if not needle:
        raise HTTPException(status_code=404, detail="no such link target")
    token = mentions.link_token(conn, payload.target_kind, payload.target_id)
    body = mentions.linkify_first(issue["body"] or "", needle, token)
    if body is None:
        # The body moved under the caller: the mention they acted on is gone.
        raise HTTPException(
            status_code=409, detail="that mention is no longer in this issue"
        )
    try:
        updated = issue_commands.update_issue(
            conn, actor=actor, issue_id=issue_id, body=body
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)
    return tagged_issue(conn, updated, response)


@router.patch("/{issue_id}", response_model=IssueOut)
def update(
    issue_id: RowIdPath,
    payload: IssueUpdate,
    request: Request,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Only the fields the client actually sent are touched. The shared command is
    # the one owner of authorization, validation, write, projections, and audit.
    fields = payload.model_dump(exclude_unset=True)
    try:
        updated = issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=issue_id,
            if_match=if_match_values(request),
            **fields,
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)
    return tagged_issue(conn, updated, response)


@router.put("/{issue_id}/assignee", response_model=IssueOut)
def set_assignee(
    issue_id: RowIdPath,
    payload: AssigneeUpdate,
    request: Request,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # The shared command owns current-assignee authorization, target-user
    # validation, the nullable row update, auto-watch, notifications, and audit.
    try:
        updated = issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=issue_id,
            assignee_id=payload.assignee_id,
            if_match=if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)
    return tagged_issue(conn, updated, response)


@router.post("/{issue_id}/archive", response_model=IssueOut)
def archive_issue(
    issue_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The command owns the soft-delete AND its atomic 'archived' event under the
    # same creator/assignee/delegated/admin gate. Idempotent: re-archiving records
    # no new fact.
    try:
        updated = issue_commands.set_issue_archived(
            conn, actor=actor, issue_id=issue_id, archived=True
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    return with_labels(conn, updated)


@router.post("/{issue_id}/unarchive", response_model=IssueOut)
def unarchive_issue(
    issue_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Restore an archived issue to the active lists via the same command; records
    # "unarchived" only if it was actually archived.
    try:
        updated = issue_commands.set_issue_archived(
            conn, actor=actor, issue_id=issue_id, archived=False
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    return with_labels(conn, updated)


_BULK_MAX = 500


def _apply_bulk_update(
    conn: sqlite3.Connection, issue_id: int, provided: dict, actor: dict
) -> None:
    """Apply one best-effort batch item through the shared atomic issue command.

    Validation failures become this item's HTTP-shaped error. Unexpected persistence,
    audit, or finalization failures still fail loud after the command rolls back.
    """
    command_fields = {
        key: provided[key]
        for key in (
            "status",
            "priority",
            "assignee_id",
            "project_id",
            "sprint_id",
        )
        if key in provided
    }
    issue_commands.update_issue(conn, actor=actor, issue_id=issue_id, **command_fields)


@router.post("/bulk", response_model=BulkUpdateOut, response_model_exclude_unset=True)
def bulk_update(
    payload: BulkUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Best-effort batch triage: apply the same change to many issues, each attempted
    # and authorized on its own (creator-or-assignee per issue, exactly as the
    # single-issue writes), so one issue's 403/404/422 never sinks the rest — the
    # per-issue outcome is reported back. Atomic-all-or-nothing is deliberately NOT
    # the contract: an agent moving 50 issues wants the 48 it may touch to move and
    # a clear list of the 2 it couldn't.
    provided = payload.model_dump(exclude_unset=True)
    field_keys = [k for k in provided if k != "ids"]
    if not payload.ids:
        raise HTTPException(status_code=422, detail="ids must be a non-empty list")
    if len(payload.ids) > _BULK_MAX:
        raise HTTPException(
            status_code=422, detail=f"at most {_BULK_MAX} ids per request"
        )
    if not field_keys:
        raise HTTPException(status_code=422, detail="no fields to update")
    # status/priority set a value; there is no "clear" for them, so an explicit null
    # is a malformed request (rejected for the whole batch, before any write).
    for column in ("status", "priority"):
        if column in provided and provided[column] is None:
            raise HTTPException(status_code=422, detail=f"{column} cannot be null")

    results: list[dict] = []
    for issue_id in dict.fromkeys(payload.ids):  # dedupe, preserve first-seen order
        try:
            _apply_bulk_update(conn, issue_id, provided, actor)
            results.append({"id": issue_id, "ok": True, "error": None})
        except issue_commands.IssueCommandError as exc:
            result = {
                "id": issue_id,
                "ok": False,
                "error": exc.detail,
            }
            if exc.code is not None:
                result["code"] = exc.code
            results.append(result)

    updated = sum(1 for r in results if r["ok"])
    return {"updated": updated, "failed": len(results) - updated, "results": results}


@router.put("/{issue_id}/sprint", response_model=IssueOut)
def set_sprint(
    issue_id: RowIdPath,
    payload: SprintAssign,
    request: Request,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # The command owns source authorization, final-project validation, persistence,
    # audit, notifications, and the hidden-sprint existence-oracle boundary.
    try:
        updated = issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=issue_id,
            sprint_id=payload.sprint_id,
            if_match=if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)
    return tagged_issue(conn, updated, response)


@router.put("/{issue_id}/project", response_model=IssueOut)
def set_project(
    issue_id: RowIdPath,
    payload: ProjectUpdate,
    request: Request,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # The command owns destination visibility, key allocation, status remapping,
    # incompatible-sprint clearing, persistence, and every resulting audit fact.
    try:
        updated = issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=issue_id,
            project_id=payload.project_id,
            if_match=if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)
    return tagged_issue(conn, updated, response)


@router.put("/{issue_id}/parent", response_model=IssueOut)
def set_parent(
    issue_id: RowIdPath,
    payload: ParentUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The command owns the nest AND its atomic 'set_parent'/'removed_parent'
    # event, the creator/assignee/delegated/admin gate, the see-the-parent check
    # (a hidden parent collapses to "no such parent issue"), and self/cycle
    # validation (422). Clearing (None) is always allowed.
    try:
        updated = issue_commands.set_issue_parent(
            conn, actor=actor, issue_id=issue_id, parent_id=payload.parent_id
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    return with_labels(conn, updated)


# --- Delegation claim/lease: accept / decline / complete -------------------
#
# The run-time interlock that stops two delegated agents from silently working the same
# issue. A lease is exclusive (one active per issue); claiming acquires it, completing
# releases it, declining rejects the delegation. Reads of the current lease are open;
# the writes need the issue-write scope and the claimant gate the command enforces.


@router.post(
    "/{issue_id}/complete",
    response_model=CompleteClaimOut,
)
def complete_issue_claim(
    issue_id: RowIdPath,
    response: Response,
    payload: LeaseGenerationIn | None = None,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Complete: release the lease you hold (the issue is freed for the next claimant),
    # or clear your own row the clock already expired. 409 if the lease is absent or
    # someone else's. Releases the coordination lease only — status
    # changes go through the ordinary status command. The body says so explicitly so
    # an agent does not have to read source to learn the issue is still open.
    try:
        released = lease_commands.complete_claim(
            conn,
            actor=actor,
            issue_id=issue_id,
            generation=payload.generation if payload is not None else None,
        )
        response.headers.update(PRIVATE_LEASE_HEADERS)
        return released
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)
