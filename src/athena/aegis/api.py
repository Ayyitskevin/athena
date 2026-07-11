"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from athena import config
from athena.aegis import (
    comments,
    contributors,
    dependencies,
    issue_activity,
    issue_commands,
    issue_history,
    issue_search,
    issues,
    project_activity,
    projects,
    sprints,
    statuses,
)
from athena.core import access, activity, attachments, labels, links, users
from athena.core.attachments_api import AttachmentOut
from athena.core.deps import get_conn
from athena.core.identity import is_admin, issue_write_actor, optional_actor

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
    # 'public' (anyone may read) or 'private' (creator, admins, and members only).
    # Defaults 'public' for every project until explicitly flipped.
    visibility: str = "public"


class VisibilityUpdate(BaseModel):
    # The privacy flag for a project/space: 'public' | 'private'. A dedicated body
    # (not folded into the project edit) because flipping privacy is creator-OR-admin,
    # while editing name/key/description stays creator-only — different gates.
    visibility: str


class MemberAdd(BaseModel):
    user_id: int


class MemberOut(BaseModel):
    # One membership row on a private project/space: who, plus who granted it and when.
    # Excludes the creator/admins, who get in implicitly (see access.list_*_members).
    user_id: int
    name: str
    is_agent: bool
    added_by: int | None = None
    added_at: str


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


def _issue_command_http_error(
    exc: issue_commands.IssueCommandError,
) -> HTTPException:
    """Translate a framework-free command rejection at the REST boundary."""
    status_code = {
        "unauthorized": 401,
        "forbidden": 403,
        "not_found": 404,
        "invalid": 422,
    }[exc.kind]
    return HTTPException(status_code=status_code, detail=exc.detail)


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
        raise _issue_command_http_error(exc) from exc
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
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Optional filters, same semantics the web list uses (one shared path in
    # issues.list_issues). A label name is resolved to issue ids by labels.py so
    # issues.py stays decoupled from the join; an unknown label matches nothing.
    # project is a direct column on the issue: an id restricts to that project,
    # "none" restricts to the backlog (no project). priority/assignee are direct
    # columns too — the same dimensions a saved filter persists. sprint is a direct
    # column too: an id restricts to that sprint (an unknown id matches nothing).
    # The read is open (optional_actor → None for anonymous), but it only ever
    # returns issues in projects the caller may see (public + their private ones);
    # admins see all, the backlog is always in.
    # limit/offset are bounded (page ≤ 100, default 50), mirroring GET /issues/search
    # — an anonymous caller can't pull the whole issues table in one request. The web
    # board reaches issues.list_issues directly (uncapped) and is unaffected.
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
        visible_project_ids=access.visible_project_filter(conn, actor),
        limit=limit,
        offset=offset,
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
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Full-text issue search narrowed by the structured filters — the ranked twin of
    # GET /issues. Open like the issue list (optional_actor → None for anonymous), but
    # gated by it: an issue in a project the caller can't see never surfaces. project
    # is validated the same way the list does (422 on garbage); the rest are passed
    # through (an unknown status/label/assignee simply matches none). A blank q
    # legitimately returns [] — the issue_search layer handles it. Declared BEFORE GET
    # /{ref} so the literal path wins over the issue-ref parameter.
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
        actor=actor,
    )


@router.get("/{ref}", response_model=IssueOut)
def show(
    ref: str,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Reads are open to everyone (optional_actor → None for anonymous), same as the
    # list endpoint — only writes pass through the creator-or-assignee gate. ref is
    # addressable two ways: the numeric id ("12") or the project key ("ATH-12"); both
    # resolve to the same issue. An issue in a private project the caller can't see is
    # a 404, indistinguishable from a missing one, so visibility never leaks via
    # existence. Backlog issues (no project) read like a public one.
    issue = issues.get_by_ref(conn, ref)
    if issue is None or not access.can_see_project_or_backlog(conn, actor, issue["project_id"]):
        raise HTTPException(status_code=404, detail="no such issue")
    return _with_labels(conn, issue)


@router.get("/{issue_id}/backlinks", response_model=list[LinkOut])
def backlinks(
    issue_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # "What references this issue?" — open like other reads, but gated by visibility:
    # a hidden issue 404s identically to a missing one (the lone sub-resource read that
    # used a bare existence check — a 200-vs-404 existence oracle), and the sources are
    # gated by the viewer so a hidden project's/space's reference never reveals itself.
    _issue_for_read(conn, issue_id, actor)
    return links.backlinks(conn, target_kind="issue", target_id=issue_id, actor=actor)


class IssueStateOut(BaseModel):
    # The issue's reconstructed lifecycle state as of a past point — time-travel over the
    # activity log. `state` holds the diff-logged fields (status, priority, assignee,
    # labels, sprint, parent, archived); content (title/body) and project aren't
    # reconstructable from the log and are deliberately absent. as_of_event_id/as_of echo
    # the actual cutoff event; is_current flags whether that cutoff is the latest event.
    issue_id: int
    as_of_event_id: int | None = None
    as_of: str | None = None
    is_current: bool
    state: dict


@router.get("/{issue_id}/state", response_model=IssueStateOut)
def issue_state(
    issue_id: int,
    as_of: int | None = Query(
        None, description="reconstruct state as of this activity event id (default: now)"
    ),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Time-travel: the issue's lifecycle state folded from its activity log as of a past
    # event. Gated like other reads — a hidden/missing issue is a 404; within a visible
    # issue its own history reads openly, like the detail page.
    _issue_for_read(conn, issue_id, actor)
    state = issue_history.project_issue_state(conn, issue_id, as_of_event_id=as_of)
    if state is None:  # _issue_for_read already 404s; this is belt-and-suspenders
        raise HTTPException(status_code=404, detail="no such issue")
    return state


def _issue_for_write(conn: sqlite3.Connection, issue_id: int, actor: dict) -> dict:
    """Fetch an issue the actor is allowed to MODIFY, or raise: 404 if no such issue
    OR one in a private project the actor can't see, 403 if the actor is neither its
    creator nor its current assignee. Centralizes the issue write-authorization rule so
    every write path (status/edit/assign/labels/links/...) enforces it identically.

    Visibility is checked FIRST and collapses to the same 404 as a missing issue —
    "can't write what you can't see." This matters even for a creator/assignee: if a
    project is flipped private without adding them, they lose the issue from view and
    must not be able to keep writing to it. The 404 (not 403) also means a hidden
    issue's existence never leaks through a write attempt."""
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise HTTPException(status_code=404, detail="no such issue")
    if not issues.can_modify(issue, actor["id"]):
        raise HTTPException(
            status_code=403,
            detail="only the issue creator or assignee may modify it",
        )
    return issue


def _issue_for_read(
    conn: sqlite3.Connection, issue_id: int, actor: dict | None
) -> dict:
    """Fetch an issue the actor may READ, or raise 404. The read counterpart of
    _issue_for_write: a missing issue and one in a private project the actor can't see
    are the same 404, so a sub-resource (comments/children/links/contributors/
    attachments) never leaks for a hidden issue. Backlog issues (no project) read like
    a public one. No write check — reads stay open within what's visible."""
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise HTTPException(status_code=404, detail="no such issue")
    return issue


@router.patch("/{issue_id}", response_model=IssueOut)
def update(
    issue_id: int,
    payload: IssueUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Only the fields the client actually sent are touched. The shared command is
    # the one owner of authorization, validation, write, projections, and audit.
    fields = payload.model_dump(exclude_unset=True)
    try:
        updated = issue_commands.update_issue(
            conn, actor=actor, issue_id=issue_id, **fields
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc
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
    try:
        before = issue_commands.get_writable_issue(
            conn, actor=actor, issue_id=issue_id
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc
    # Validate every relationship field before the core command applies anything.
    # Status/priority validation stays in that command, its one policy owner.
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
    # Apply. Core editable fields use the same atomic command as the single REST
    # and browser writes; the remaining nullable relationships will migrate in
    # later vertical slices.
    if "status" in provided or "priority" in provided:
        core_fields = {
            key: provided[key]
            for key in ("status", "priority")
            if key in provided
        }
        try:
            issue_commands.update_issue(
                conn, actor=actor, issue_id=issue_id, **core_fields
            )
        except issue_commands.IssueCommandError as exc:
            raise _issue_command_http_error(exc) from exc
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
    # The TARGET project must be one the actor can see (and exist) — you can't move an
    # issue into a private project you're not in. can_see_project is False for a missing
    # project too, so unknown and invisible collapse to the same 422. None is always
    # valid — it means "remove from project" (the backlog).
    if payload.project_id is not None and not access.can_see_project(
        conn, actor, payload.project_id
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
    # The parent must be one the actor can see: a hidden parent collapses to the same
    # "no such parent issue" 422 validate_parent gives a missing one, so you can't nest
    # under (or probe the existence of) an issue in a private project you're not in.
    if payload.parent_id is not None and not access.can_see_issue(
        conn, actor, payload.parent_id
    ):
        raise HTTPException(status_code=422, detail="no such parent issue")
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
    issue_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like backlinks/comments. 404 if the issue is missing OR in a private
    # project the caller can't see. The children themselves are visibility-gated too:
    # a child can sit in a private project the caller can't see (parenting spans
    # projects), so gate the list the same way the issue list is gated — else the
    # parent's children would leak a hidden child's content.
    _issue_for_read(conn, issue_id, actor)
    children = issues.list_children(
        conn, issue_id, visible_project_ids=access.visible_project_filter(conn, actor)
    )
    return _with_labels_many(conn, children)


@router.post("/{issue_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    issue_id: int,
    payload: CommentCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # author is the authenticated actor, never a caller-supplied field. Commenting is
    # an additive write any issue WRITER may do — but only on an issue they can see, so
    # gate by visibility (404 if missing or hidden), not by can_modify.
    _issue_for_read(conn, issue_id, actor)
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
    issue_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _issue_for_read(conn, issue_id, actor)  # 404 if missing or not visible
    return comments.list_comments(conn, issue_id)


def _author_comment_or_error(
    conn: sqlite3.Connection,
    issue_id: int,
    comment_id: int,
    actor: dict,
    *,
    allow_admin: bool = False,
) -> dict:
    """Fetch a comment that belongs to this issue, requiring the actor to be its
    author. Raises 404 if the comment is missing or hangs off another issue, 403
    if someone other than the author tries to change it. This author-ownership
    check is the one place we enforce per-row ownership today (issues themselves
    are still 'any authenticated actor' — a separate, deferred design).

    allow_admin lifts the author restriction for admins — a moderation override used
    ONLY on delete, so an admin can remove another user's comment (spam, abuse). Edit
    stays strictly author-only even for admins: removing someone's words is moderation,
    but rewriting them would put words in their mouth. The delete is still audited to
    the admin, so the moderation is on the record."""
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
        raise HTTPException(status_code=404, detail="no such comment")
    if existing["author_id"] != actor["id"] and not (allow_admin and is_admin(actor)):
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
    _issue_for_read(conn, issue_id, actor)  # 404 if the issue is missing or hidden
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
    _issue_for_read(conn, issue_id, actor)  # 404 if the issue is missing or hidden
    _author_comment_or_error(conn, issue_id, comment_id, actor, allow_admin=True)
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
    # the creator/assignee) — but only on an issue they can see. 404 if missing or
    # hidden.
    _issue_for_read(conn, issue_id, actor)
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
    issue_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like listing comments. 404 if the issue is missing or not visible.
    _issue_for_read(conn, issue_id, actor)
    return attachments.list_for(conn, "issue", issue_id)


# --- Links: typed dependencies between issues -----------------------------


@router.get("/{issue_id}/links", response_model=IssueLinksOut)
def list_links(
    issue_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Open read, like backlinks/comments. 404 if the issue is missing or not visible,
    # so a hidden/typo'd id reads as not-found rather than three empty lists.
    _issue_for_read(conn, issue_id, actor)
    return dependencies.list_links(conn, issue_id, actor=actor)


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
    # The TARGET must be an issue the actor can see: a hidden target collapses to the
    # same 422 as a missing one, so you can't link to (or probe the existence of) an
    # issue in a private project you're not in. can_see_issue is False for a missing
    # issue too, so the two cases are indistinguishable.
    target = issues.get_by_ref(conn, payload.target_ref)
    if target is None or not access.can_see_issue(conn, actor, target["id"]):
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
    return dependencies.list_links(conn, issue_id, actor=actor)


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
    return dependencies.list_links(conn, issue_id, actor=actor)


# --- Projects: a top-level grouping of issues -----------------------------


@projects_router.get("", response_model=list[ProjectOut])
def list_all_projects(
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading the project list is open (optional_actor → None for anonymous), but it
    # only lists projects the caller may see — public ones plus their own private
    # ones; admins see all. A private project never shows to someone outside it.
    return projects.list_projects(conn, access.visible_project_filter(conn, actor))


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
def show_project(
    project_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    project = projects.get_project(conn, project_id)
    # A private project the caller can't see is a 404, indistinguishable from a missing
    # one, so visibility never leaks through existence — the gate its Mentor twin
    # show_space already applied.
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    return project


def _project_for_write(conn: sqlite3.Connection, project_id: int, actor: dict) -> dict:
    """Fetch a project the actor may MODIFY, or raise: 404 if no such project, 403
    if the actor isn't its creator. Edit/delete is creator-only — projects have no
    assignee, so unlike issues there is no second eligible writer. Reading the
    project (and creating one) stay open; only changing or removing an existing
    one is gated here.

    Visibility first, THEN the creator check: a hidden private project must read as
    "no such project" (404), never as "exists but not yours" (403) — otherwise
    PATCH/DELETE is an existence oracle for names the read path deliberately hides.
    Same order as _project_for_privacy and the web's _authorize_project_write."""
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
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
    # sprints.project_id is NOT NULL with no ON DELETE, so a project that owns any
    # sprint would fail the bare DELETE at the FK and surface as a 500 — permanently
    # undeletable. Refuse cleanly (409), same block-don't-cascade rule as issues.
    if sprints.list_sprints(conn, project_id=project_id):
        raise HTTPException(
            status_code=409, detail="delete its sprints first"
        )
    projects.delete_project(conn, project_id)


# --- Project access control: privacy toggle + membership ------------------
#
# Turning a project private and managing its member roster is creator-OR-admin —
# deliberately WIDER than edit/delete (creator-only, via _project_for_write), because
# an admin must be able to administer access on any project, and the creator must never
# be able to lock themselves out. Reads of the roster are gated by plain visibility:
# anyone who can SEE the project can see who's in it.


def _project_for_privacy(conn: sqlite3.Connection, project_id: int, actor: dict) -> dict:
    """Fetch a project whose privacy/membership the actor may MANAGE, or raise: 404 if
    no such project OR one the actor can't see, 403 if it's visible but the actor is
    neither its creator nor an admin. The wider twin of _project_for_write (creator-
    only): access administration is creator-OR-admin per the access model.

    Visibility is checked first and collapses to a 404, so a private project the actor
    can't see is indistinguishable from a missing one — its existence never leaks
    through a 403, matching the web twin _authorize_project_manage. A member who CAN see
    it but isn't the creator/admin still gets the honest 403."""
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    if project["created_by"] != actor["id"] and not is_admin(actor):
        raise HTTPException(
            status_code=403,
            detail="only the project creator or an admin may manage access",
        )
    return project


@projects_router.put("/{project_id}/visibility", response_model=ProjectOut)
def set_project_visibility(
    project_id: int,
    payload: VisibilityUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator or admin only (404 if missing, 403 otherwise).
    project = _project_for_privacy(conn, project_id, actor)
    visibility = payload.visibility.strip().lower()
    if visibility not in ("public", "private"):
        raise HTTPException(
            status_code=422, detail="visibility must be 'public' or 'private'"
        )
    # Setting it to what it already is is a no-op — no write, no audit event.
    if visibility == project["visibility"]:
        return project
    updated = projects.set_visibility(conn, project_id, visibility)
    # Going private: record the creator as an explicit member so they appear in the
    # roster (they always keep access via created_by regardless — this is for the UI).
    if visibility == "private":
        access.add_project_member(
            conn, project_id, project["created_by"], added_by=actor["id"]
        )
    project_activity.record_project_visibility_changed(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        name=updated["name"],
        visibility=visibility,
    )
    return updated


@projects_router.get("/{project_id}/members", response_model=list[MemberOut])
def list_project_members(
    project_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading the roster is gated by plain visibility: anyone who can see the project
    # sees its members. A private project the caller can't see is a 404 — the roster
    # never reveals that the project (or its members) exist.
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    return access.list_project_members(conn, project_id)


@projects_router.post(
    "/{project_id}/members", response_model=list[MemberOut], status_code=201
)
def add_project_member(
    project_id: int,
    payload: MemberAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Creator or admin only. 422 if the user id isn't real (rather than letting the FK
    # surface a 500). Idempotent: re-adding an existing member records no event.
    _project_for_privacy(conn, project_id, actor)
    member = users.get_user(conn, payload.user_id)
    if member is None:
        raise HTTPException(status_code=422, detail="no such user")
    if access.add_project_member(conn, project_id, payload.user_id, added_by=actor["id"]):
        project_activity.record_project_member_added(
            conn, actor_id=actor["id"], project_id=project_id, member_name=member["name"]
        )
    return access.list_project_members(conn, project_id)


@projects_router.delete("/{project_id}/members/{user_id}", response_model=list[MemberOut])
def remove_project_member(
    project_id: int,
    user_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Creator or admin only. 404 if the user wasn't a member (so a no-op delete is an
    # honest miss, not a silent success). The member keeps no access they had via
    # created_by/admin — this only removes the explicit grant.
    _project_for_privacy(conn, project_id, actor)
    member = users.get_user(conn, user_id)
    if not access.remove_project_member(conn, project_id, user_id):
        raise HTTPException(status_code=404, detail="user is not a member")
    project_activity.record_project_member_removed(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        member_name=member["name"] if member else str(user_id),
    )
    return access.list_project_members(conn, project_id)


# --- Per-project statuses: the configurable lifecycle ---------------------


@projects_router.get("/{project_id}/statuses", response_model=list[StatusOut])
def list_project_statuses(
    project_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading a project's statuses is open, like listing its issues — but gated by the
    # same visibility: a private project the caller can't see is a 404, so its status
    # vocabulary doesn't leak.
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
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
    is_agent: bool = False
    added_by: int
    added_at: str


class ContributorAdd(BaseModel):
    user_id: int


@router.get("/{issue_id}/contributors", response_model=list[ContributorOut])
def list_issue_contributors(
    issue_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like listing comments/labels. 404 if the issue is missing or not
    # visible.
    _issue_for_read(conn, issue_id, actor)
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


@router.post("/{issue_id}/delegate", response_model=list[ContributorOut], status_code=201)
def delegate_issue_to_agent(
    issue_id: int,
    payload: ContributorAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Agent delegation is the explicit agent-as-teammate path: the human assignee stays
    # accountable; the target agent is added as a contributor and receives a distinct
    # delegated audit event. Generic contributor add remains available for humans.
    _issue_for_write(conn, issue_id, actor)
    target = users.get_user(conn, payload.user_id)
    if target is None:
        raise HTTPException(status_code=422, detail="no such user")
    if not target["is_agent"]:
        raise HTTPException(status_code=422, detail="delegation target must be an agent")
    if contributors.add_contributor(conn, issue_id, payload.user_id, actor["id"]):
        issue_activity.record_delegated(
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
