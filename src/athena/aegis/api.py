"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from athena import config
from athena.aegis import (
    claim_handoffs,
    comment_commands,
    comments,
    contributors,
    dependencies,
    issue_etags,
    issue_commands,
    issue_history,
    issue_search,
    issues,
    lease_commands,
    leases,
    project_commands,
    issue_query,
    project_etags,
    projects,
    sprints,
    status_commands,
    statuses,
)
from athena.core import (
    access,
    attachment_commands,
    attachments,
    graph,
    labels,
    links,
    mentions,
    users,
    work_query,
)
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
Priority = Literal["low", "medium", "high", "urgent"]


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
    # issue's final project's set in the command. project_id and sprint_id may be
    # sent together to move directly into a destination sprint. Their None defaults
    # are intentional: model_dump(exclude_unset=True) still distinguishes an omitted
    # relationship (leave it alone) from explicit null (clear it).
    title: str | None = None
    body: str | None = None
    status: str | None = None
    priority: Priority | None = None
    project_id: int | None = None
    sprint_id: int | None = None
    # A human-only, explicit acknowledgment of the protected project's
    # blocked-close policy. Agent actors can never use this flag.
    override_blocked_close: StrictBool = False


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
    # assignee_id/project_id/sprint_id null clears that relationship — distinct
    # from omitting it, which leaves it alone. status/priority are set to a value
    # (there is no "clear status"). Each issue is processed independently.
    ids: list[int]
    status: str | None = None
    priority: Priority | None = None
    assignee_id: int | None = None
    project_id: int | None = None
    sprint_id: int | None = None


class BulkResult(BaseModel):
    id: int
    ok: bool
    # The human-readable reason this issue was skipped (e.g. "no such status for
    # this project"), or null when it succeeded.
    error: str | None = None
    # Stable machine-readable refusal code when the command provides one.
    code: str | None = None


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


class ProjectPolicyUpdate(BaseModel):
    block_agent_closes_when_blocked: StrictBool


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

    # Disabled by default: projects preserve today's close behavior until a human
    # creator/admin explicitly opts in.
    block_agent_closes_when_blocked: bool = False


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
    # Additive actor-kind projection used by operator surfaces and MCP clients to
    # distinguish agent-owned, human-owned, and unassigned work without a user
    # lookup. None means the issue is unassigned.
    assignee_is_agent: bool | None = None
    project_id: int | None = None
    project_name: str | None = None
    # Null when there is no parent or the request actor cannot see it.
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


def issue_command_status(exc: issue_commands.IssueCommandError) -> int:
    """The HTTP status this command rejection means.

    Public because the undo engine needs the same answer from outside this module
    (`aegis/issue_undo.py`): its route lives in ``core``, which may not import
    ``aegis`` to translate. One mapping, so the two boundaries cannot drift."""
    return _ISSUE_COMMAND_STATUS[exc.kind]


def _issue_command_http_error(
    exc: issue_commands.IssueCommandError,
) -> HTTPException:
    """Translate a framework-free command rejection at the REST boundary."""
    return HTTPException(status_code=issue_command_status(exc), detail=exc.detail)


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


_PRIVATE_LEASE_HEADERS = {
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


def _issue_command_error_response(
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
            headers=_PRIVATE_LEASE_HEADERS,
        )
    policy_status = _ISSUE_POLICY_HTTP.get(exc.code or "")
    if policy_status is not None:
        return JSONResponse(
            status_code=policy_status,
            content={"detail": exc.detail, "code": exc.code},
            headers=_PRIVATE_LEASE_HEADERS,
        )
    raise _issue_command_http_error(exc) from exc


def _if_match_values(request: Request) -> list[str] | None:
    """Preserve every raw If-Match field line for standards-aware parsing."""
    values = [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"if-match"
    ]
    return values or None


_CLAIM_IF_MATCH_OPENAPI = {
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


def _with_labels(conn: sqlite3.Connection, issue: dict, *, actor: dict | None) -> dict:
    """Return one canonical actor-visible issue representation with labels."""
    return issue_etags.resource_and_etag(conn, issue, actor=actor)[0]


def _tagged_issue(
    conn: sqlite3.Connection,
    issue: dict,
    response: Response,
    *,
    actor: dict | None,
) -> dict:
    """Return the exact actor-visible representation and matching strong ETag."""
    public, current_etag = issue_etags.resource_and_etag(conn, issue, actor=actor)
    response.headers["ETag"] = current_etag
    return public


def _with_labels_many(
    conn: sqlite3.Connection, rows: list[dict], *, actor: dict | None
) -> list[dict]:
    """Return canonical actor-visible list representations without N+1 queries."""
    by_issue = labels.labels_for_issues(conn, [r["id"] for r in rows])
    return [
        resource
        for resource, _ in issue_etags.resources_and_etags(
            conn,
            rows,
            actor=actor,
            label_rows_by_issue=by_issue,
        )
    ]


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
        raise _issue_command_http_error(exc) from exc
    return _tagged_issue(conn, issue, response, actor=actor)


def _parse_project_filter(project: str | None) -> tuple[int | None, bool]:
    """HTTP wrapper over the shared issues.parse_project_filter: the parsing rules
    live in one place (so the web list can't drift from us), and here we turn the
    "invalid" signal (None) into the API's 422."""
    parsed = issues.parse_project_filter(project)
    if parsed is None:
        raise HTTPException(status_code=422, detail="invalid project filter")
    return parsed


def _query_refusal(exc: Exception, atom: str | None) -> HTTPException:
    """One 422 shape for a bad query, whether the grammar or the domain refused it.

    ``atom`` names the offending piece so a caller can fix the typo instead of
    guessing which of eight terms was wrong — the whole reason an unknown atom is
    an error rather than an empty result set."""
    detail: dict[str, object] = {"error": str(exc), "code": "invalid_query"}
    if atom is not None:
        detail["atom"] = atom
    return HTTPException(status_code=422, detail=detail)


def _parsed_query(raw: str) -> work_query.Query:
    try:
        return work_query.parse(raw)
    except work_query.QueryError as exc:
        raise _query_refusal(exc, exc.atom) from exc


@router.get("", response_model=list[IssueOut])
def index(
    q: str | None = Query(
        None,
        description=(
            "Work query, e.g. 'is:open label:infra project:ATH sort:priority-desc'. "
            "Mutually exclusive with the structured filters below."
        ),
    ),
    status: str | None = None,
    priority: str | None = None,
    assignee: int | None = Query(None, ge=0, le=issues.MAX_SQLITE_INTEGER),
    label: str | None = None,
    search: str | None = None,
    project: str | None = None,
    sprint: int | None = Query(None, ge=0, le=issues.MAX_SQLITE_INTEGER),
    include_archived: bool = False,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=issues.MAX_OFFSET),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # `q` and the structured filters are two spellings of the same intent, so
    # combining them is refused rather than merged: silently AND-ing them would
    # make `?q=is:open&status=done` return nothing and look like a data problem,
    # and silently preferring one would ignore what the caller asked for.
    if q is not None:
        conflicting = [
            name
            for name, value in (
                ("status", status),
                ("priority", priority),
                ("assignee", assignee),
                ("label", label),
                ("search", search),
                ("project", project),
                ("sprint", sprint),
            )
            if value is not None
        ]
        if include_archived:
            conflicting.append("include_archived")
        if conflicting:
            raise HTTPException(
                status_code=422,
                detail=(
                    "q cannot be combined with the structured filters "
                    f"({', '.join(sorted(conflicting))}); express them in the query"
                ),
            )
        try:
            rows = issue_query.run_query(
                conn,
                _parsed_query(q),
                actor=actor,
                visible_project_ids=access.visible_project_filter(conn, actor),
                limit=limit,
                offset=offset,
            )
        except issue_query.QueryCompileError as exc:
            raise _query_refusal(exc, exc.atom) from exc
        return _with_labels_many(conn, rows, actor=actor)
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
    return _with_labels_many(conn, rows, actor=actor)


class QueryCountOut(BaseModel):
    # The total behind a page, so a surface can say "showing 50 of 340" instead of
    # implying the page is the whole answer — the same honesty the active-work and
    # work-context surfaces already apply to their bounded windows.
    q: str
    matched: int


@router.get("/query/count", response_model=QueryCountOut)
def query_count(
    q: str,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """How many issues this query matches, within the caller's visibility."""
    parsed = _parsed_query(q)
    try:
        matched = issue_query.count_query(
            conn,
            parsed,
            actor=actor,
            visible_project_ids=access.visible_project_filter(conn, actor),
        )
    except issue_query.QueryCompileError as exc:
        raise _query_refusal(exc, exc.atom) from exc
    return {"q": parsed.raw, "matched": matched}


@router.get("/query/help")
def query_help() -> dict:
    """The query vocabulary, as data.

    Emitted from `work_query.describe()` rather than restated, so this endpoint,
    the MCP tool's docstring, and docs/QUERY.md cannot drift from the parser.
    """
    return work_query.describe()


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
    assignee: int | None = Query(None, ge=0, le=issues.MAX_SQLITE_INTEGER),
    label: str | None = None,
    project: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=issues.MAX_OFFSET),
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
    response: Response,
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
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise HTTPException(status_code=404, detail="no such issue")
    return _tagged_issue(conn, issue, response, actor=actor)


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


@router.get("/{issue_id}/graph")
def issue_graph(
    issue_id: int,
    depth: int = graph.DEFAULT_DEPTH,
    max_nodes: int = graph.DEFAULT_MAX_NODES,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The bounded neighbourhood around this issue, as positioned data rather than
    # markup — the Aegis twin of the page graph.
    _issue_for_read(conn, issue_id, actor)
    return graph.ego_graph(
        conn,
        kind="issue",
        node_id=issue_id,
        actor=actor,
        depth=depth,
        max_nodes=max_nodes,
    )


@router.get("/{issue_id}/unlinked-mentions")
def issue_unlinked_mentions(
    issue_id: int,
    limit: int = mentions.DEFAULT_LIMIT,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Text naming this issue's key without linking to it. A read: it proposes
    # edges, never creates them.
    _issue_for_read(conn, issue_id, actor)
    return mentions.unlinked_mentions(
        conn, kind="issue", target_id=issue_id, actor=actor, limit=limit
    )


class LinkMentionIn(BaseModel):
    # Which target to link to. The SOURCE is the issue in the path — this endpoint
    # edits that issue's body.
    target_kind: str
    target_id: int


@router.post("/{issue_id}/link-mention", response_model=IssueOut)
def link_issue_mention(
    issue_id: int,
    payload: LinkMentionIn,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Rewrite THIS issue's body so its first unlinked mention of the target becomes
    # a reference. The endpoint lives on the source's own domain because the source
    # is what gets edited — and it goes through update_issue, so the edit carries
    # the same authorization, projections, and audit as any other issue edit.
    issue = _issue_for_read(conn, issue_id, actor)
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
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response, actor=actor)


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
        None,
        description="reconstruct state as of this activity event id (default: now)",
    ),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Time-travel: the issue's lifecycle state folded from its activity log as of a past
    # event. Gated like other reads — a hidden/missing issue is a 404; within a visible
    # issue its own history reads openly, like the detail page.
    _issue_for_read(conn, issue_id, actor)
    try:
        state = issue_history.project_issue_state(
            conn, issue_id, as_of_event_id=as_of, actor=actor
        )
    except issue_history.IncompleteIssueHistory as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except issue_history.IssueHistoryTooLarge as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    if not issues.can_act_on(conn, issue, actor):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the issue creator, assignee, a delegated contributor, "
                "or an admin may modify it"
            ),
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
            if_match=_if_match_values(request),
            **fields,
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response, actor=actor)


@router.put("/{issue_id}/assignee", response_model=IssueOut)
def set_assignee(
    issue_id: int,
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
            if_match=_if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response, actor=actor)


@router.post("/{issue_id}/archive", response_model=IssueOut)
def archive_issue(
    issue_id: int,
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
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, updated, actor=actor)


@router.post("/{issue_id}/unarchive", response_model=IssueOut)
def unarchive_issue(
    issue_id: int,
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
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, updated, actor=actor)


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
    issue_id: int,
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
            if_match=_if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response, actor=actor)


@router.put("/{issue_id}/project", response_model=IssueOut)
def set_project(
    issue_id: int,
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
            if_match=_if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response, actor=actor)


@router.put("/{issue_id}/parent", response_model=IssueOut)
def set_parent(
    issue_id: int,
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
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, updated, actor=actor)


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
    return _with_labels_many(conn, children, actor=actor)


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
    # The command owns the insert AND its atomic 'commented' event (with the auto-watch
    # and any mentions), so a comment and its activity footprint land together.
    return comment_commands.create_comment(
        conn, actor_id=actor["id"], issue_id=issue_id, body=body
    )


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
    # The command owns the edit AND its atomic 'comment_edited' event — previously a
    # silent content rewrite.
    try:
        return comment_commands.edit_comment(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            comment_id=comment_id,
            body=body,
        )
    except comment_commands.CommentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.delete("/{issue_id}/comments/{comment_id}", status_code=204)
def delete_comment(
    issue_id: int,
    comment_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    _issue_for_read(conn, issue_id, actor)  # 404 if the issue is missing or hidden
    _author_comment_or_error(conn, issue_id, comment_id, actor, allow_admin=True)
    # The command owns the delete AND its atomic 'comment_deleted' event; a comment that
    # vanished in a race records nothing and 404s.
    if not comment_commands.delete_comment(
        conn, actor_id=actor["id"], issue_id=issue_id, comment_id=comment_id
    ):
        raise HTTPException(status_code=404, detail="no such comment")


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
    try:
        return attachment_commands.create_attachment(
            conn,
            actor=actor,
            target_kind="issue",
            target_id=issue_id,
            filename=file.filename,
            content_type=file.content_type,
            data=data,
            attach_dir=config.ATTACH_DIR,
        )
    except attachment_commands.AttachmentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


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
    # Declaring a relationship FROM this issue is a write on it — the command owns the
    # creator-or-assignee gate, the target visibility check, and now the audit event,
    # so the same edge created here or over MCP records one attributable "linked" event
    # atomically. (A hidden target collapses to the same 422 as a missing one.)
    try:
        return issue_commands.link_issues(
            conn,
            actor=actor,
            issue_id=issue_id,
            target_ref=payload.target_ref,
            relation=payload.relation,
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc


@router.delete("/{issue_id}/links/{relation}/{target_id}", response_model=IssueLinksOut)
def remove_link(
    issue_id: int,
    relation: str,
    target_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Removing a relationship is a write on this issue too; the command records the
    # audit event and 404s (not_found) when there's nothing to remove.
    try:
        return issue_commands.unlink_issues(
            conn,
            actor=actor,
            issue_id=issue_id,
            target_id=target_id,
            relation=relation,
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc


_PROJECT_POLICY_PRECONDITION_HTTP = {
    "precondition_required": (428, "precondition_required"),
    "invalid_precondition": (400, "invalid_if_match"),
    "precondition_too_large": (431, "if_match_too_large"),
    "precondition_failed": (412, "precondition_failed"),
}

_PROJECT_POLICY_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "precondition_required": 428,
    "invalid_precondition": 400,
    "precondition_too_large": 431,
    "precondition_failed": 412,
}


def _tagged_project(project: dict, response: Response) -> dict:
    """Return the public project representation and matching strong ETag."""
    public, current = project_etags.resource_and_etag(project)
    response.headers["ETag"] = current
    return public


def _project_policy_error_response(
    exc: project_commands.ProjectPolicyCommandError,
) -> JSONResponse:
    """Map guarded policy failures without weakening project visibility."""
    spec = _PROJECT_POLICY_PRECONDITION_HTTP.get(exc.kind)
    if spec is not None:
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
    raise HTTPException(
        status_code=_PROJECT_POLICY_STATUS[exc.kind], detail=exc.detail
    ) from exc


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
    # The command owns the insert, its default statuses, AND the atomic
    # 'created_project' audit event — a workspace container never appears silently.
    return project_commands.create_project(
        conn,
        actor_id=actor["id"],
        name=name,
        key=key,
        description=payload.description,
    )


@projects_router.get("/{project_id}", response_model=ProjectOut)
def show_project(
    project_id: int,
    response: Response,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    project = projects.get_project(conn, project_id)
    # A private project the caller can't see is a 404, indistinguishable from a missing
    # one, so visibility never leaks through existence — the gate its Mentor twin
    # show_space already applied.
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    return _tagged_project(project, response)


@projects_router.put("/{project_id}/policy", response_model=ProjectOut)
def set_project_policy(
    project_id: int,
    payload: ProjectPolicyUpdate,
    request: Request,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    """Configure blocked-close governance from an exact reviewed project state."""
    try:
        updated = project_commands.set_blocked_close_policy(
            conn,
            actor=actor,
            project_id=project_id,
            enabled=payload.block_agent_closes_when_blocked,
            if_match=_if_match_values(request),
        )
    except project_commands.ProjectPolicyCommandError as exc:
        return _project_policy_error_response(exc)
    return _tagged_project(updated, response)


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
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    reason = access.container_write_reason(
        conn,
        actor,
        kind="project",
        container_id=project_id,
        created_by=project["created_by"],
    )
    if reason == "not_visible":
        raise HTTPException(status_code=404, detail="no such project")
    if reason == "not_owner":
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
    # The command owns the edit AND its atomic 'edited_project' audit event.
    updated = project_commands.update_project(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        name=name,
        key=key,
        description=payload.description,
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
        raise HTTPException(status_code=409, detail="delete its sprints first")
    # The command owns the delete AND its atomic 'deleted_project' event, which
    # outlives the vanished container so the trail keeps who removed it.
    project_commands.delete_project(conn, actor_id=actor["id"], project_id=project_id)


# --- Project access control: privacy toggle + membership ------------------
#
# Turning a project private and managing its member roster is creator-OR-admin —
# deliberately WIDER than edit/delete (creator-only, via _project_for_write), because
# an admin must be able to administer access on any project, and the creator must never
# be able to lock themselves out. Reads of the roster are gated by plain visibility:
# anyone who can SEE the project can see who's in it.


def _project_for_privacy(
    conn: sqlite3.Connection, project_id: int, actor: dict
) -> dict:
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
    # The command owns the flip, the creator's roster row when going private, and the
    # atomic visibility event — so an access-control change can never half-land.
    try:
        return project_commands.set_project_visibility(
            conn, actor_id=actor["id"], project_id=project_id, visibility=visibility
        )
    except project_commands.ProjectAccessCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


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
    # The command owns the grant and its atomic audit event.
    project_commands.add_project_member(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        user_id=payload.user_id,
        member_name=member["name"],
    )
    return access.list_project_members(conn, project_id)


@projects_router.delete(
    "/{project_id}/members/{user_id}", response_model=list[MemberOut]
)
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
    # The command owns the revoke and its atomic audit event; a non-member is an
    # honest 404 rather than a silent success.
    if not project_commands.remove_project_member(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        user_id=user_id,
        member_name=member["name"] if member else str(user_id),
    ):
        raise HTTPException(status_code=404, detail="user is not a member")
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
    # project itself. The command owns that gate now (from rows read under its own
    # write lock) along with the validation, the write, and the audit event.
    try:
        return status_commands.add_status(
            conn,
            actor=actor,
            project_id=project_id,
            name=payload.name,
            category=payload.category,
        )
    except status_commands.StatusCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@projects_router.delete("/{project_id}/statuses/{name}", response_model=list[StatusOut])
def remove_project_status(
    project_id: int,
    name: str,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    try:
        return status_commands.remove_status(
            conn, actor=actor, project_id=project_id, name=name
        )
    except status_commands.StatusCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


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
    except sqlite3.IntegrityError as exc:
        # The pre-check above races a concurrent create: the UNIQUE constraint
        # (case-insensitive, 0007) is the backstop, and losing that race is a
        # 409, not a 500.
        raise HTTPException(status_code=409, detail="label already exists") from exc


# --- Labels on an issue: a write, so creator-or-assignee gated -------------


@router.post("/{issue_id}/labels", response_model=IssueOut, status_code=201)
def attach_label(
    issue_id: int,
    payload: LabelAttach,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The command owns the attach AND its atomic 'labeled' event under the
    # creator/assignee/delegated/admin gate. Idempotent: re-attach records nothing.
    try:
        issue = issue_commands.attach_label(
            conn, actor=actor, issue_id=issue_id, label_id=payload.label_id
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, issue, actor=actor)


@router.delete("/{issue_id}/labels/{label_id}", response_model=IssueOut)
def detach_label(
    issue_id: int,
    label_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    try:
        issue = issue_commands.detach_label(
            conn, actor=actor, issue_id=issue_id, label_id=label_id
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, issue, actor=actor)


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


class ClaimHandoffEventOut(BaseModel):
    event_id: int
    actor_id: int
    actor_name: str
    created_at: str
    run_id: str | None


class ClaimHandoffResumeEventOut(ClaimHandoffEventOut):
    lease_generation: str
    note: str | None


class ClaimHandoffOut(BaseModel):
    handoff_token: str
    issue_id: int
    lease_generation: str
    schema_version: Literal[1]
    state: Literal["awaiting_resume", "resumed"]
    reason: lease_commands.ClaimYieldReason
    note: str | None
    attempted_work: str
    evidence: list[str]
    blocking_question: str
    resume_instructions: str
    yielded: ClaimHandoffEventOut
    resumed: ClaimHandoffResumeEventOut | None
    advisory_untrusted: Literal[True]


class LeaseOut(BaseModel):
    # The exclusive claim on an issue: who holds it, when it was taken, when it expires,
    # and whether that window is still open (active=false is an expired, reclaimable lease).
    issue_id: int
    holder_id: int
    holder_name: str
    claimed_at: str
    expires_at: str
    generation: str
    active: bool
    open_claim_handoff: ClaimHandoffOut | None = None


HandoffEvidenceItem = Annotated[
    str,
    Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_EVIDENCE_ITEM_CHARS,
    ),
]


class ClaimIn(BaseModel):
    # How long the lease should hold before it must be renewed. Omitted → the server
    # default. Bounded by the command to [MIN, MAX] lease seconds.
    lease_seconds: int | None = None
    # Omit to acquire a free/expired lease. Supply the current value to renew the
    # same active possession; a supplied stale value never becomes acquisition.
    generation: str | None = None


class YieldClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Optional at the transport boundary so the command can return the stable
    # 428 lease_generation_required error instead of a generic validation error.
    generation: str | None = None
    reason: lease_commands.ClaimYieldReason
    note: str | None = Field(
        default=None,
        max_length=lease_commands.MAX_CLAIM_YIELD_NOTE_CHARS,
    )
    attempted_work: str = Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_ATTEMPTED_WORK_CHARS,
    )
    evidence: list[HandoffEvidenceItem] = Field(
        max_length=lease_commands.MAX_HANDOFF_EVIDENCE_ITEMS
    )
    blocking_question: str = Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_BLOCKING_QUESTION_CHARS,
    )
    resume_instructions: str = Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS,
    )


class LeaseGenerationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation: str | None = None


class ResumeClaimHandoffIn(LeaseGenerationIn):
    resume_note: str | None = Field(
        default=None,
        max_length=lease_commands.MAX_HANDOFF_RESUME_NOTE_CHARS,
    )


class DeclineDelegationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Required only when decline would release this actor's active lease.
    generation: str | None = None


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


@router.post(
    "/{issue_id}/contributors", response_model=list[ContributorOut], status_code=201
)
def add_issue_contributor(
    issue_id: int,
    payload: ContributorAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # The command owns the add, its auto-watch, and its atomic 'added_contributor'
    # event under the same write gate. Idempotent: re-add records nothing.
    try:
        return issue_commands.add_contributor(
            conn, actor=actor, issue_id=issue_id, user_id=payload.user_id
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc


@router.post(
    "/{issue_id}/delegate", response_model=list[ContributorOut], status_code=201
)
def delegate_issue_to_agent(
    issue_id: int,
    payload: ContributorAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Agent delegation is the explicit agent-as-teammate path: the human assignee stays
    # accountable; the target agent is added as a contributor and receives a distinct
    # 'delegated' event. The command owns the add + atomic event; require_agent gates
    # the target to an agent. Generic contributor add remains available for humans.
    try:
        return issue_commands.add_contributor(
            conn,
            actor=actor,
            issue_id=issue_id,
            user_id=payload.user_id,
            require_agent=True,
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc


@router.delete(
    "/{issue_id}/contributors/{user_id}", response_model=list[ContributorOut]
)
def remove_issue_contributor(
    issue_id: int,
    user_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    try:
        return issue_commands.remove_contributor(
            conn, actor=actor, issue_id=issue_id, user_id=user_id
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc


# --- Delegation claim/lease: accept / decline / complete -------------------
#
# The run-time interlock that stops two delegated agents from silently working the same
# issue. A lease is exclusive (one active per issue); claiming acquires it, completing
# releases it, declining rejects the delegation. Reads of the current lease are open;
# the writes need the issue-write scope and the claimant gate the command enforces.


@router.get("/{issue_id}/lease", response_model=LeaseOut | None)
def get_issue_lease(
    issue_id: int,
    response: Response,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | None:
    # Who holds this issue right now (or null if unclaimed) — so an agent can see it is
    # taken before trying to claim. 404 if the issue is missing or hidden, like the other
    # sub-resource reads; the lease itself carries `active` (an expired lease reads
    # active=false rather than vanishing, so the last holder is still visible).
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise HTTPException(status_code=404, detail="no such issue")
    response.headers.update(_PRIVATE_LEASE_HEADERS)
    lease = leases.get_lease(conn, issue_id)
    if lease is not None:
        lease["open_claim_handoff"] = claim_handoffs.get_open_handoff(conn, issue_id)
    return lease


@router.post(
    "/{issue_id}/claim",
    response_model=LeaseOut,
    status_code=201,
    openapi_extra=_CLAIM_IF_MATCH_OPENAPI,
)
def claim_issue(
    issue_id: int,
    request: Request,
    response: Response,
    payload: ClaimIn | None = None,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Accept only against the exact root issue revision the claimant reviewed.
    # The command checks the raw If-Match under the lease's write transaction.
    lease_seconds = payload.lease_seconds if payload else None
    response.headers.update(_PRIVATE_LEASE_HEADERS)
    kwargs = {} if lease_seconds is None else {"lease_seconds": lease_seconds}
    try:
        return lease_commands.claim_issue(
            conn,
            actor=actor,
            issue_id=issue_id,
            if_match=_if_match_values(request),
            generation=payload.generation if payload else None,
            **kwargs,
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)


@router.post(
    "/{issue_id}/yield",
    response_model=ClaimHandoffOut,
    status_code=201,
)
def yield_issue_claim(
    issue_id: int,
    payload: YieldClaimIn,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Yield is a truthful holder action, not completion or reassignment. The
    # command atomically releases the lease and records the run-stamped reason.
    try:
        handoff = lease_commands.yield_claim(
            conn,
            actor=actor,
            issue_id=issue_id,
            generation=payload.generation,
            reason=payload.reason,
            note=payload.note,
            attempted_work=payload.attempted_work,
            evidence=payload.evidence,
            blocking_question=payload.blocking_question,
            resume_instructions=payload.resume_instructions,
        )
        response.headers.update(_PRIVATE_LEASE_HEADERS)
        return handoff
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)


@router.post(
    "/{issue_id}/claim-handoffs/{handoff_token}/resume",
    response_model=ClaimHandoffOut,
)
def resume_issue_claim_handoff(
    issue_id: int,
    handoff_token: str,
    payload: ResumeClaimHandoffIn,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    try:
        handoff = lease_commands.resume_claim_handoff(
            conn,
            actor=actor,
            issue_id=issue_id,
            handoff_token=handoff_token,
            generation=payload.generation,
            resume_note=payload.resume_note,
        )
        response.headers.update(_PRIVATE_LEASE_HEADERS)
        return handoff
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)


@router.post("/{issue_id}/complete", status_code=204, response_model=None)
def complete_issue_claim(
    issue_id: int,
    response: Response,
    payload: LeaseGenerationIn | None = None,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None | JSONResponse:
    # Complete: release the lease you hold (the issue is freed for the next claimant).
    # 409 if you don't hold an active lease. Releases the coordination lease only — status
    # changes go through the ordinary status command.
    try:
        lease_commands.complete_claim(
            conn,
            actor=actor,
            issue_id=issue_id,
            generation=payload.generation if payload is not None else None,
        )
        response.headers.update(_PRIVATE_LEASE_HEADERS)
        return None
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)


@router.post("/{issue_id}/decline", response_model=list[ContributorOut])
def decline_issue_delegation(
    issue_id: int,
    response: Response,
    payload: DeclineDelegationIn | None = None,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict] | JSONResponse:
    # Decline: remove yourself from the contributor set so the work is visibly refused, not
    # silently dropped. 404 if you weren't a delegated contributor. Returns the remaining
    # contributors; any lease you held is released with the same act.
    try:
        result = lease_commands.decline_delegation(
            conn,
            actor=actor,
            issue_id=issue_id,
            generation=payload.generation if payload else None,
        )
        response.headers.update(_PRIVATE_LEASE_HEADERS)
        return result
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
