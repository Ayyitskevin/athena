"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from athena import config
from athena.aegis import (
    comments,
    contributors,
    dependencies,
    issue_activity,
    issue_search,
    issues,
    labels,
    projects,
    sprints,
    statuses,
)
from athena.core import activity, attachments, links, users
from athena.core.attachments_api import AttachmentOut
from athena.core.deps import get_conn
from athena.core.identity import issue_write_actor

router = APIRouter(prefix="/issues", tags=["aegis"])
# Labels are a top-level resource (shared vocabulary), not nested under an issue,
# so they get their own router. Attaching a label TO an issue is a sub-resource
# of /issues and lives on `router` below.
labels_router = APIRouter(prefix="/labels", tags=["aegis"])
# Projects are a top-level resource too (a container issues belong to), so they
# get their own router. Setting an issue's project is a sub-resource of /issues
# and lives on `router` below.
projects_router = APIRouter(prefix="/projects", tags=["aegis"])

# Priority is still a fixed global lifecycle (validated at the boundary). Status is
# now PER-PROJECT (aegis/statuses), so it can't be a static Literal — it's a free
# string validated against the target project's status set in the handlers below.
Priority = Literal[issues.PRIORITIES]


class IssueCreate(BaseModel):
    title: str
    body: str = ""
    # None => the target project's first (default) status. A given status is
    # validated against that project's set in the handler.
    status: str | None = None
    priority: Priority = "medium"
    project_id: int | None = None


class IssueUpdate(BaseModel):
    # A partial edit: send any subset. Unset fields are left unchanged. priority is
    # constrained to its lifecycle when present; status is validated against the
    # issue's project's set in the handler.
    title: str | None = None
    body: str | None = None
    status: str | None = None
    priority: Priority | None = None


class AssigneeUpdate(BaseModel):
    # None clears the assignee (unassign); an int assigns to that user.
    assignee_id: int | None


class SprintAssign(BaseModel):
    # None moves the issue to the backlog; an int puts it in that sprint (which must
    # belong to the issue's project).
    sprint_id: int | None


class ProjectUpdate(BaseModel):
    # None removes the issue from its project; an int moves it into that project.
    project_id: int | None


class BulkUpdate(BaseModel):
    # Apply the same triage change to many issues at once. Only the fields actually
    # sent are touched (model_dump(exclude_unset=True) in the handler), so a sent
    # assignee_id/sprint_id of null means "unassign"/"move to backlog" — distinct
    # from omitting it, which leaves it alone. status/priority are set to a value
    # (there is no "clear status"). Each issue is processed independently.
    ids: list[int]
    status: str | None = None
    priority: Priority | None = None
    assignee_id: int | None = None
    sprint_id: int | None = None


class BulkResult(BaseModel):
    id: int
    ok: bool
    # The human-readable reason this issue was skipped (e.g. "no such status for
    # this project"), or null when it succeeded.
    error: str | None = None


class BulkUpdateOut(BaseModel):
    # A best-effort batch: each issue is attempted on its own and reported here, so
    # one issue's 403/404/422 never sinks the rest. updated + failed == len(results).
    updated: int
    failed: int
    results: list[BulkResult]


class ParentUpdate(BaseModel):
    # None clears the parent (top-level); an int nests the issue under that issue.
    parent_id: int | None


class ProjectCreate(BaseModel):
    name: str
    # The issue-key prefix (e.g. "ATH" -> ATH-1, ATH-2). Required on create and
    # the canonical short identity; validated for shape and uniqueness below.
    key: str
    description: str = ""


class ProjectEdit(BaseModel):
    # A partial edit of the project itself (not an issue's link to it — that is
    # ProjectUpdate above). Send any subset; unset fields are left unchanged.
    name: str | None = None
    key: str | None = None
    description: str | None = None


class ProjectOut(BaseModel):
    id: int
    name: str
    key: str
    description: str
    created_by: int
    created_at: str


class StatusCreate(BaseModel):
    name: str
    category: str  # 'todo' | 'doing' | 'done'


class StatusOut(BaseModel):
    name: str
    category: str
    position: int


class LabelCreate(BaseModel):
    name: str
    color: str = "#6b7280"


class LabelOut(BaseModel):
    id: int
    name: str
    color: str


class LabelAttach(BaseModel):
    label_id: int


class IssueOut(BaseModel):
    id: int
    # The project-scoped key (e.g. "ATH-12"), or null for a backlog issue with no
    # project. Computed by issues.py from the project prefix + per-project number.
    key: str | None = None
    title: str
    body: str
    status: str
    priority: str
    created_by: int
    created_at: str
    assignee_id: int | None = None
    assignee_name: str | None = None
    project_id: int | None = None
    project_name: str | None = None
    parent_id: int | None = None
    # The sprint this issue is in, or null for the backlog.
    sprint_id: int | None = None
    # When the issue was archived (soft-deleted), or null if it's active.
    archived_at: str | None = None
    labels: list[LabelOut] = []


class LinkOut(BaseModel):
    # One resolved cross-reference: the kind/id it points at, that target's
    # current title, and whether it still exists (title is null when broken).
    kind: str
    id: int
    title: str | None = None
    exists: bool


class LinkCreate(BaseModel):
    # The other issue, addressed by ref — numeric id ("15") or project key
    # ("ATH-15"), the same addressing the read endpoints accept. relation is the
    # user-facing form; "blocked_by" is stored as the inverse "blocks" edge.
    target_ref: str
    relation: Literal["blocks", "blocked_by", "relates"]


class IssueLinkSummary(BaseModel):
    # Just enough of the other issue to link to it and show its state.
    id: int
    key: str | None = None
    title: str
    status: str


class IssueLinksOut(BaseModel):
    # One issue's relationships, grouped by user-facing relation.
    blocks: list[IssueLinkSummary] = []
    blocked_by: list[IssueLinkSummary] = []
    relates: list[IssueLinkSummary] = []


class CommentCreate(BaseModel):
    body: str


class CommentOut(BaseModel):
    id: int
    issue_id: int
    author_id: int
    author_name: str
    body: str
    created_at: str


def _validate_key(key: str) -> str:
    """Normalize and validate a project key, or raise 422. Returns the uppercased
    key the boundary should pass on to the data layer / dup check. The shape rule
    itself lives in projects.normalize_key so the web form enforces it identically."""
    normalized = projects.normalize_key(key)
    if normalized is None:
        raise HTTPException(
            status_code=422,
            detail="key must start with a letter and be 1–10 letters/digits",
        )
    return normalized


def _with_labels(conn: sqlite3.Connection, issue: dict) -> dict:
    """Attach the issue's labels under a "labels" key. Issues own their core row
    (issues.py); labels are composed on here so the two modules stay in their
    lanes and reads still come back as one object for the client."""
    issue["labels"] = labels.labels_for_issue(conn, issue["id"])
    return issue


def _with_labels_many(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Same as _with_labels but for a list, using one bulk query (no N+1)."""
    by_issue = labels.labels_for_issues(conn, [r["id"] for r in rows])
    for r in rows:
        r["labels"] = by_issue.get(r["id"], [])
    return rows


@router.post("", response_model=IssueOut, status_code=201)
def create(
    payload: IssueCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # created_by is the authenticated actor, never a value the caller supplied.
    # Reject a blank title at the boundary (422) so the API matches the web form,
    # which already strips and rejects empties — persist the stripped value.
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    # Reject an unknown project here (422) rather than letting the FK raise a 500.
    if (
        payload.project_id is not None
        and projects.get_project(conn, payload.project_id) is None
    ):
        raise HTTPException(status_code=422, detail="no such project")
    # Status is validated against the TARGET project's set (the backlog uses the
    # default set). Unset means "start at the project's first status".
    if payload.status is None:
        status = statuses.first_status(conn, payload.project_id)
    elif not statuses.is_valid(conn, payload.project_id, payload.status):
        raise HTTPException(
            status_code=422, detail="no such status for this project"
        )
    else:
        status = payload.status
    issue = issues.create_issue(
        conn,
        title=title,
        body=payload.body,
        status=status,
        priority=payload.priority,
        project_id=payload.project_id,
        created_by=actor["id"],
    )
    issue_activity.record_created(
        conn, actor_id=actor["id"], issue_id=issue["id"], body=issue["body"]
    )
    return _with_labels(conn, issue)


def _parse_project_filter(project: str | None) -> tuple[int | None, bool]:
    """HTTP wrapper over the shared issues.parse_project_filter: the parsing rules
    live in one place (so the web list can't drift from us), and here we turn the
    "invalid" signal (None) into the API's 422."""
    parsed = issues.parse_project_filter(project)
    if parsed is None:
        raise HTTPException(status_code=422, detail="invalid project filter")
    return parsed


@router.get("", response_model=list[IssueOut])
def index(
    status: str | None = None,
    priority: str | None = None,
    assignee: int | None = None,
    label: str | None = None,
    search: str | None = None,
    project: str | None = None,
    sprint: int | None = None,
    include_archived: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Optional filters, same semantics the web list uses (one shared path in
    # issues.list_issues). A label name is resolved to issue ids by labels.py so
    # issues.py stays decoupled from the join; an unknown label matches nothing.
    # project is a direct column on the issue: an id restricts to that project,
    # "none" restricts to the backlog (no project). priority/assignee are direct
    # columns too — the same dimensions a saved filter persists. sprint is a direct
    # column too: an id restricts to that sprint (an unknown id matches nothing).
    project_id, backlog = _parse_project_filter(project)
    ids = labels.issue_ids_for_label(conn, label) if label else None
    rows = issues.list_issues(
        conn,
        status=status,
        priority=priority,
        assignee_id=assignee,
        search=search,
        project_id=project_id,
        backlog=backlog,
        sprint_id=sprint,
        include_archived=include_archived,
        ids=ids,
    )
    return _with_labels_many(conn, rows)


class IssueSearchHit(BaseModel):
    # A ranked issue hit: the FTS relevance fields plus the per-issue context
    # core.search enriches (key/status). All hits are issues, so kind is always
    # "issue"; it's kept for shape-parity with the cross-kind /search response.
    kind: str
    source_id: int
    title: str
    snippet: str
    key: str | None = None
    status: str | None = None


@router.get("/search", response_model=list[IssueSearchHit])
def search_issues_endpoint(
    q: str,
    status: str | None = None,
    priority: str | None = None,
    assignee: int | None = None,
    label: str | None = None,
    project: str | None = None,
    limit: int = 20,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Full-text issue search narrowed by the structured filters — the ranked twin of
    # GET /issues. Open like the issue list (it returns issue data only, no pages), so
    # no actor. project is validated the same way the list does (422 on garbage); the
    # rest are passed through (an unknown status/label/assignee simply matches none).
    # A blank q legitimately returns [] — the issue_search layer handles it. Declared
    # BEFORE GET /{ref} so the literal path wins over the issue-ref parameter.
    if project is not None:
        _parse_project_filter(project)  # raises 422 on an unparseable filter
    return issue_search.search_issues(
        conn,
        q,
        status=status,
        priority=priority,
        assignee_id=assignee,
        label=label,
        project=project,
        limit=limit,
        offset=offset,
    )


@router.get("/{ref}", response_model=IssueOut)
def show(ref: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    # Reads are open to everyone (no actor), same as the list endpoint — only
    # writes pass through the creator-or-assignee gate. ref is addressable two
    # ways: the numeric id ("12") or the project key ("ATH-12"); both resolve to
    # the same issue. Writes and sub-resources below stay numeric (forms post the
    # id we control), so only this read entry point widens.
    issue = issues.get_by_ref(conn, ref)
    if issue is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return _with_labels(conn, issue)


@router.get("/{issue_id}/backlinks", response_model=list[LinkOut])
def backlinks(
    issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    # "What references this issue?" — open like other reads. 404 if the issue
    # itself is missing, so a typo'd id reads as not-found, not empty.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return links.backlinks(conn, target_kind="issue", target_id=issue_id)


def _issue_for_write(conn: sqlite3.Connection, issue_id: int, actor: dict) -> dict:
    """Fetch an issue the actor is allowed to MODIFY, or raise: 404 if no such
    issue, 403 if the actor is neither its creator nor its current assignee.
    Centralizes the issue write-authorization rule (issues.can_modify) so every
    write path (status/edit/assign) enforces it identically. Reads and comments
    do not go through here."""
    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="no such issue")
    if not issues.can_modify(issue, actor["id"]):
        raise HTTPException(
            status_code=403,
            detail="only the issue creator or assignee may modify it",
        )
    return issue


@router.patch("/{issue_id}", response_model=IssueOut)
def update(
    issue_id: int,
    payload: IssueUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator or assignee only (404 if missing, 403 if not permitted).
    before = _issue_for_write(conn, issue_id, actor)
    # Only the fields the client actually sent are touched (exclude_unset).
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    title = payload.title.strip() if payload.title is not None else None
    if title is not None and not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    # A new status must belong to the issue's project's set (backlog uses defaults).
    if payload.status is not None and not statuses.is_valid(
        conn, before["project_id"], payload.status
    ):
        raise HTTPException(
            status_code=422, detail="no such status for this project"
        )
    updated = issues.update_issue(
        conn,
        issue_id,
        title=title,
        body=payload.body,
        status=payload.status,
        priority=payload.priority,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such issue")
    # Record a status change as its own audit fact (the lifecycle moment that
    # matters: "open → done"). The helper no-ops if status didn't actually move,
    # so an edit that only touches title/body records nothing.
    if "status" in fields:
        issue_activity.record_status_change(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            before=before["status"],
            after=updated["status"],
        )
    if "priority" in fields:
        issue_activity.record_priority_change(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            before=before["priority"],
            after=updated["priority"],
        )
    # A title/body edit is its own audit fact, separate from a status move; the
    # helper no-ops if neither actually changed, so a status/priority-only edit
    # records nothing here.
    issue_activity.record_edited(
        conn,
        actor_id=actor["id"],
        issue_id=issue_id,
        before=before,
        after=updated,
    )
    return _with_labels(conn, updated)


@router.put("/{issue_id}/assignee", response_model=IssueOut)
def set_assignee(
    issue_id: int,
    payload: AssigneeUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator or assignee only (404 if missing, 403 if not permitted). Checked
    # against the CURRENT assignee — so an unassigned issue can only be assigned
    # by its creator, and an assignee may reassign or unassign themselves.
    before = _issue_for_write(conn, issue_id, actor)
    # Reject an unknown user here (422) rather than letting the FK raise a 500.
    # None is always valid — it means "unassign".
    if (
        payload.assignee_id is not None
        and users.get_user(conn, payload.assignee_id) is None
    ):
        raise HTTPException(status_code=422, detail="no such user")
    updated = issues.set_assignee(conn, issue_id, payload.assignee_id)
    # The helper records "assigned"/"unassigned" only when the assignee actually
    # changed (re-PUTting the same assignee records nothing).
    issue_activity.record_assignee_change(
        conn,
        actor_id=actor["id"],
        issue_id=issue_id,
        before=before["assignee_id"],
        after=payload.assignee_id,
    )
    return _with_labels(conn, updated)


@router.post("/{issue_id}/archive", response_model=IssueOut)
def archive_issue(
    issue_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Archiving (soft-delete) is a write on the issue — creator-or-assignee only
    # (404 / 403), the same gate as status/assign. The row is never destroyed; it's
    # hidden from the default lists and can be restored. Idempotent: re-archiving an
    # archived issue re-stamps the time but records no new audit fact.
    before = _issue_for_write(conn, issue_id, actor)
    updated = issues.set_archived(conn, issue_id, True)
    issue_activity.record_archive_change(
        conn,
        actor_id=actor["id"],
        issue_id=issue_id,
        before=before["archived_at"],
        after=updated["archived_at"],
    )
    return _with_labels(conn, updated)


@router.post("/{issue_id}/unarchive", response_model=IssueOut)
def unarchive_issue(
    issue_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Restore an archived issue to the active lists. Same write gate; records
    # "unarchived" only if it was actually archived.
    before = _issue_for_write(conn, issue_id, actor)
    updated = issues.set_archived(conn, issue_id, False)
    issue_activity.record_archive_change(
        conn,
        actor_id=actor["id"],
        issue_id=issue_id,
        before=before["archived_at"],
        after=updated["archived_at"],
    )
    return _with_labels(conn, updated)


_BULK_MAX = 500


def _apply_bulk_update(
    conn: sqlite3.Connection, issue_id: int, provided: dict, actor: dict
) -> None:
    """Apply the requested fields to ONE issue, or raise HTTPException (404/403/422)
    so the caller records this issue as failed. Reuses the exact per-issue write
    gate, validation, and activity recorders the single-issue endpoints use, so the
    bulk path can never diverge from them. ALL requested fields are validated before
    any are written, so a rejected issue is never left half-updated."""
    before = _issue_for_write(conn, issue_id, actor)  # 404 / 403
    # Validate everything up front (same rules as the single-issue endpoints).
    if "status" in provided and not statuses.is_valid(
        conn, before["project_id"], provided["status"]
    ):
        raise HTTPException(status_code=422, detail="no such status for this project")
    if (
        "assignee_id" in provided
        and provided["assignee_id"] is not None
        and users.get_user(conn, provided["assignee_id"]) is None
    ):
        raise HTTPException(status_code=422, detail="no such user")
    if "sprint_id" in provided and provided["sprint_id"] is not None:
        sprint = sprints.get_sprint(conn, provided["sprint_id"])
        if sprint is None:
            raise HTTPException(status_code=422, detail="no such sprint")
        if sprint["project_id"] != before["project_id"]:
            raise HTTPException(
                status_code=422,
                detail="sprint belongs to a different project than the issue",
            )
    # Apply — each write commits on its own (best-effort persists per issue), and
    # each recorder no-ops when its field didn't actually move.
    if "status" in provided or "priority" in provided:
        updated = issues.update_issue(
            conn,
            issue_id,
            status=provided.get("status"),
            priority=provided.get("priority"),
        )
        if updated is None:  # vanished mid-batch (a race) — report, don't 500
            raise HTTPException(status_code=404, detail="no such issue")
        if "status" in provided:
            issue_activity.record_status_change(
                conn, actor_id=actor["id"], issue_id=issue_id,
                before=before["status"], after=updated["status"],
            )
        if "priority" in provided:
            issue_activity.record_priority_change(
                conn, actor_id=actor["id"], issue_id=issue_id,
                before=before["priority"], after=updated["priority"],
            )
    if "assignee_id" in provided:
        issues.set_assignee(conn, issue_id, provided["assignee_id"])
        issue_activity.record_assignee_change(
            conn, actor_id=actor["id"], issue_id=issue_id,
            before=before["assignee_id"], after=provided["assignee_id"],
        )
    if "sprint_id" in provided:
        issues.set_sprint(conn, issue_id, provided["sprint_id"])
        issue_activity.record_sprint_change(
            conn, actor_id=actor["id"], issue_id=issue_id,
            before=before["sprint_id"], after=provided["sprint_id"],
        )


@router.post("/bulk", response_model=BulkUpdateOut)
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
            results.append({"id": issue_id, "ok": True})
        except HTTPException as exc:
            results.append({"id": issue_id, "ok": False, "error": str(exc.detail)})
    updated = sum(1 for r in results if r["ok"])
    return {"updated": updated, "failed": len(results) - updated, "results": results}


@router.put("/{issue_id}/sprint", response_model=IssueOut)
def set_sprint(
    issue_id: int,
    payload: SprintAssign,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Putting an issue in a sprint is a write on the issue — creator-or-assignee only
    # (404 if missing, 403 if not permitted), same gate as assign/project.
    before = _issue_for_write(conn, issue_id, actor)
    if payload.sprint_id is not None:
        sprint = sprints.get_sprint(conn, payload.sprint_id)
        if sprint is None:
            raise HTTPException(status_code=422, detail="no such sprint")
        # A sprint belongs to one project; an issue can only join a sprint in its OWN
        # project (a backlog issue with no project can't be in any sprint).
        if sprint["project_id"] != before["project_id"]:
            raise HTTPException(
                status_code=422,
                detail="sprint belongs to a different project than the issue",
            )
    updated = issues.set_sprint(conn, issue_id, payload.sprint_id)
    # Records "moved_to_sprint"/"removed_from_sprint" only on a real change.
    issue_activity.record_sprint_change(
        conn,
        actor_id=actor["id"],
        issue_id=issue_id,
        before=before["sprint_id"],
        after=payload.sprint_id,
    )
    return _with_labels(conn, updated)


@router.put("/{issue_id}/project", response_model=IssueOut)
def set_project(
    issue_id: int,
    payload: ProjectUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Moving an issue between projects is a write — creator-or-assignee only
    # (404 if missing, 403 if not permitted), same gate as status/assign/labels.
    before = _issue_for_write(conn, issue_id, actor)
    # Reject an unknown project here (422) rather than letting the FK raise a 500.
    # None is always valid — it means "remove from project".
    if (
        payload.project_id is not None
        and projects.get_project(conn, payload.project_id) is None
    ):
        raise HTTPException(status_code=422, detail="no such project")
    updated = issues.set_project(conn, issue_id, payload.project_id)
    issue_activity.record_project_change(
        conn,
        actor_id=actor["id"],
        issue_id=issue_id,
        before=before["project_id"],
        after=updated["project_id"],
    )
    return _with_labels(conn, updated)


@router.put("/{issue_id}/parent", response_model=IssueOut)
def set_parent(
    issue_id: int,
    payload: ParentUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Nesting an issue is a write on it — creator-or-assignee only (404/403), same
    # gate as status/assign/project. The parent is validated for existence, self,
    # and cycles (422); clearing (None) is always allowed.
    before = _issue_for_write(conn, issue_id, actor)
    reason = issues.validate_parent(conn, issue_id, payload.parent_id)
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)
    updated = issues.set_parent(conn, issue_id, payload.parent_id)
    issue_activity.record_parent_change(
        conn,
        actor_id=actor["id"],
        issue_id=issue_id,
        before=before["parent_id"],
        after=payload.parent_id,
    )
    return _with_labels(conn, updated)


@router.get("/{issue_id}/children", response_model=list[IssueOut])
def list_children(
    issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    # Open read, like backlinks/comments. 404 if the issue itself is missing.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return _with_labels_many(conn, issues.list_children(conn, issue_id))


@router.post("/{issue_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    issue_id: int,
    payload: CommentCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # author is the authenticated actor, never a caller-supplied field.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    comment = comments.add_comment(
        conn, issue_id=issue_id, author_id=actor["id"], body=body
    )
    issue_activity.record_commented(
        conn, actor_id=actor["id"], issue_id=issue_id, body=body
    )
    return comment


@router.get("/{issue_id}/comments", response_model=list[CommentOut])
def list_comments(
    issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return comments.list_comments(conn, issue_id)


def _author_comment_or_error(
    conn: sqlite3.Connection, issue_id: int, comment_id: int, actor: dict
) -> dict:
    """Fetch a comment that belongs to this issue, requiring the actor to be its
    author. Raises 404 if the comment is missing or hangs off another issue, 403
    if someone other than the author tries to change it. This author-ownership
    check is the one place we enforce per-row ownership today (issues themselves
    are still 'any authenticated actor' — a separate, deferred design)."""
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
        raise HTTPException(status_code=404, detail="no such comment")
    if existing["author_id"] != actor["id"]:
        raise HTTPException(status_code=403, detail="not the comment author")
    return existing


@router.patch("/{issue_id}/comments/{comment_id}", response_model=CommentOut)
def edit_comment(
    issue_id: int,
    comment_id: int,
    payload: CommentCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _author_comment_or_error(conn, issue_id, comment_id, actor)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    updated = comments.update_comment(conn, comment_id, body=body)
    if updated is None:  # vanished between the author check and the write (a race)
        raise HTTPException(status_code=404, detail="no such comment")
    return updated


@router.delete("/{issue_id}/comments/{comment_id}", status_code=204)
def delete_comment(
    issue_id: int,
    comment_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    _author_comment_or_error(conn, issue_id, comment_id, actor)
    if not comments.delete_comment(conn, comment_id):
        # vanished between the author check and the delete (a race) — don't record
        # an event for a deletion that didn't happen.
        raise HTTPException(status_code=404, detail="no such comment")
    issue_activity.record_comment_deleted(conn, actor_id=actor["id"], issue_id=issue_id)


# --- Attachments on an issue ----------------------------------------------


@router.post("/{issue_id}/attachments", response_model=AttachmentOut, status_code=201)
def upload_issue_attachment(
    issue_id: int,
    file: UploadFile = File(...),
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Attaching is additive, like commenting: any issue writer may do it (not just
    # the creator/assignee). 404 if the issue is missing.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    if len(data) > config.ATTACH_MAX_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    att = attachments.store(
        conn,
        target_kind="issue",
        target_id=issue_id,
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        uploaded_by=actor["id"],
        attach_dir=config.ATTACH_DIR,
    )
    activity.record(
        conn,
        actor_id=actor["id"],
        verb="added_attachment",
        target_kind="issue",
        target_id=issue_id,
        detail=att["filename"],
    )
    return att


@router.get("/{issue_id}/attachments", response_model=list[AttachmentOut])
def list_issue_attachments(
    issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    # Open read, like listing comments. 404 if the issue itself is missing.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return attachments.list_for(conn, "issue", issue_id)


# --- Links: typed dependencies between issues -----------------------------


@router.get("/{issue_id}/links", response_model=IssueLinksOut)
def list_links(issue_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    # Open read, like backlinks/comments. 404 if the issue itself is missing, so
    # a typo'd id reads as not-found rather than three empty lists.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return dependencies.list_links(conn, issue_id)


@router.post("/{issue_id}/links", response_model=IssueLinksOut, status_code=201)
def add_link(
    issue_id: int,
    payload: LinkCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Declaring a relationship FROM this issue is a write on it — creator-or-
    # assignee only (404 if missing, 403 if not permitted), same gate as
    # status/assign/labels. The gate is on THIS issue (the one being edited),
    # regardless of which end the edge is stored on.
    _issue_for_write(conn, issue_id, actor)
    target = issues.get_by_ref(conn, payload.target_ref)
    if target is None:
        raise HTTPException(status_code=422, detail="no such target issue")
    reason = dependencies.add_link(
        conn,
        from_id=issue_id,
        to_id=target["id"],
        relation=payload.relation,
        created_by=actor["id"],
    )
    if reason is not None:
        # The direct contradiction (A blocks B while B blocks A) conflicts with
        # existing state -> 409; everything else is bad input -> 422.
        status = 409 if "block each other" in reason else 422
        raise HTTPException(status_code=status, detail=reason)
    return dependencies.list_links(conn, issue_id)


@router.delete("/{issue_id}/links/{relation}/{target_id}", response_model=IssueLinksOut)
def remove_link(
    issue_id: int,
    relation: str,
    target_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Removing a relationship is a write on this issue too. relation is the same
    # user-facing form used to add it; an unknown relation simply matches no row.
    _issue_for_write(conn, issue_id, actor)
    if not dependencies.remove_link(
        conn, from_id=issue_id, to_id=target_id, relation=relation
    ):
        raise HTTPException(status_code=404, detail="no such relationship")
    return dependencies.list_links(conn, issue_id)


# --- Projects: a top-level grouping of issues -----------------------------


@projects_router.get("", response_model=list[ProjectOut])
def list_all_projects(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    # Reading the project list is open, like listing issues.
    return projects.list_projects(conn)


@projects_router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may create a project (like creating a label).
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="project name is required")
    key = _validate_key(payload.key)
    if projects.get_project_by_name(conn, name) is not None:
        raise HTTPException(status_code=409, detail="project already exists")
    if projects.get_project_by_key(conn, key) is not None:
        raise HTTPException(status_code=409, detail="project key already in use")
    return projects.create_project(
        conn,
        name=name,
        key=key,
        description=payload.description,
        created_by=actor["id"],
    )


@projects_router.get("/{project_id}", response_model=ProjectOut)
def show_project(project_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    project = projects.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    return project


def _project_for_write(conn: sqlite3.Connection, project_id: int, actor: dict) -> dict:
    """Fetch a project the actor may MODIFY, or raise: 404 if no such project, 403
    if the actor isn't its creator. Edit/delete is creator-only — projects have no
    assignee, so unlike issues there is no second eligible writer. Reading the
    project (and creating one) stay open; only changing or removing an existing
    one is gated here."""
    project = projects.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    if project["created_by"] != actor["id"]:
        raise HTTPException(
            status_code=403, detail="only the project creator may modify it"
        )
    return project


@projects_router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectEdit,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator only (404 if missing, 403 if not permitted).
    _project_for_write(conn, project_id, actor)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    name = payload.name.strip() if payload.name is not None else None
    if name is not None:
        if not name:
            raise HTTPException(status_code=422, detail="project name cannot be empty")
        # A rename onto another project's name is the same collision create guards
        # against (409). Renaming to your own current name is fine (NOCASE match
        # on yourself), so only block when the match is a DIFFERENT project.
        clash = projects.get_project_by_name(conn, name)
        if clash is not None and clash["id"] != project_id:
            raise HTTPException(status_code=409, detail="project already exists")
    key = _validate_key(payload.key) if payload.key is not None else None
    if key is not None:
        # Same collision logic as name: a key already held by ANOTHER project is a
        # 409; re-saving your own current key (NOCASE self-match) is fine.
        clash = projects.get_project_by_key(conn, key)
        if clash is not None and clash["id"] != project_id:
            raise HTTPException(status_code=409, detail="project key already in use")
    updated = projects.update_project(
        conn, project_id, name=name, key=key, description=payload.description
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such project")
    return updated


@projects_router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    # Creator only (404 if missing, 403 if not permitted).
    _project_for_write(conn, project_id, actor)
    # Refuse rather than cascade/detach: a project that still owns issues must be
    # emptied first (reassign or delete those issues). 409, mirroring the Mentor
    # page-delete-on-children rule — a delete must not silently move data.
    if issues.count_issues_in_project(conn, project_id) > 0:
        raise HTTPException(
            status_code=409, detail="reassign or delete its issues first"
        )
    projects.delete_project(conn, project_id)


# --- Per-project statuses: the configurable lifecycle ---------------------


@projects_router.get("/{project_id}/statuses", response_model=list[StatusOut])
def list_project_statuses(
    project_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    # Reading a project's statuses is open, like listing its issues. 404 if the
    # project itself is missing.
    if projects.get_project(conn, project_id) is None:
        raise HTTPException(status_code=404, detail="no such project")
    return statuses.list_statuses(conn, project_id)


@projects_router.post(
    "/{project_id}/statuses", response_model=list[StatusOut], status_code=201
)
def add_project_status(
    project_id: int,
    payload: StatusCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Configuring statuses is project config — creator-only, like editing the
    # project itself.
    _project_for_write(conn, project_id, actor)
    reason = statuses.add_status(conn, project_id, payload.name, payload.category)
    if reason is not None:
        status = 409 if "already exists" in reason else 422
        raise HTTPException(status_code=status, detail=reason)
    return statuses.list_statuses(conn, project_id)


@projects_router.delete("/{project_id}/statuses/{name}", response_model=list[StatusOut])
def remove_project_status(
    project_id: int,
    name: str,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _project_for_write(conn, project_id, actor)
    reason = statuses.remove_status(conn, project_id, name)
    if reason is not None:
        status = 404 if reason == "no such status" else 409
        raise HTTPException(status_code=status, detail=reason)
    return statuses.list_statuses(conn, project_id)


# --- Labels: a top-level shared vocabulary --------------------------------


@labels_router.get("", response_model=list[LabelOut])
def list_all_labels(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    # Reading the vocabulary is open, like listing issues.
    return labels.list_labels(conn)


@labels_router.post("", response_model=LabelOut, status_code=201)
def create_label(
    payload: LabelCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may add to the shared vocabulary (like commenting).
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="label name is required")
    if labels.get_label_by_name(conn, name) is not None:
        raise HTTPException(status_code=409, detail="label already exists")
    try:
        return labels.create_label(conn, name=name, color=payload.color)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- Labels on an issue: a write, so creator-or-assignee gated -------------


@router.post("/{issue_id}/labels", response_model=IssueOut, status_code=201)
def attach_label(
    issue_id: int,
    payload: LabelAttach,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Changing an issue's labels is a write — same gate as status/assign.
    issue = _issue_for_write(conn, issue_id, actor)
    if labels.get_label(conn, payload.label_id) is None:
        raise HTTPException(status_code=422, detail="no such label")
    if labels.add_label_to_issue(conn, issue_id, payload.label_id):  # idempotent
        issue_activity.record_label_added(
            conn, actor_id=actor["id"], issue_id=issue_id, label_id=payload.label_id
        )
    return _with_labels(conn, issue)


@router.delete("/{issue_id}/labels/{label_id}", response_model=IssueOut)
def detach_label(
    issue_id: int,
    label_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    issue = _issue_for_write(conn, issue_id, actor)
    if not labels.remove_label_from_issue(conn, issue_id, label_id):
        raise HTTPException(status_code=404, detail="label not on this issue")
    issue_activity.record_label_removed(
        conn, actor_id=actor["id"], issue_id=issue_id, label_id=label_id
    )
    return _with_labels(conn, issue)


# --- Contributors on an issue: delegating teammates (humans or agents) ------
# The single assignee stays the accountable owner; contributors are additional
# actors working the issue. Adding one is a write on the issue (creator-or-assignee
# gated, same as labels). Reading the list is open, like comments/labels.


class ContributorOut(BaseModel):
    user_id: int
    name: str
    added_by: int
    added_at: str


class ContributorAdd(BaseModel):
    user_id: int


@router.get("/{issue_id}/contributors", response_model=list[ContributorOut])
def list_issue_contributors(
    issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    # Open read, like listing comments/labels. 404 if the issue itself is missing.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return contributors.list_contributors(conn, issue_id)


@router.post("/{issue_id}/contributors", response_model=list[ContributorOut], status_code=201)
def add_issue_contributor(
    issue_id: int,
    payload: ContributorAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Delegating is a write — same creator-or-assignee gate as labels/status.
    _issue_for_write(conn, issue_id, actor)
    if users.get_user(conn, payload.user_id) is None:
        raise HTTPException(status_code=422, detail="no such user")
    # Idempotent: record (and auto-watch) only when a NEW pairing was created.
    if contributors.add_contributor(conn, issue_id, payload.user_id, actor["id"]):
        issue_activity.record_contributor_added(
            conn, actor_id=actor["id"], issue_id=issue_id, user_id=payload.user_id
        )
    return contributors.list_contributors(conn, issue_id)


@router.delete("/{issue_id}/contributors/{user_id}", response_model=list[ContributorOut])
def remove_issue_contributor(
    issue_id: int,
    user_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _issue_for_write(conn, issue_id, actor)
    if not contributors.remove_contributor(conn, issue_id, user_id):
        raise HTTPException(status_code=404, detail="not a contributor on this issue")
    issue_activity.record_contributor_removed(
        conn, actor_id=actor["id"], issue_id=issue_id, user_id=user_id
    )
    return contributors.list_contributors(conn, issue_id)
