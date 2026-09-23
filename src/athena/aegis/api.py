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
    comment_commands,
    comments,
    dependencies,
    issue_etags,
    issue_commands,
    issue_history,
    issue_narrative,
    issue_search,
    issues,
    lease_commands,
    issue_query,
)
from athena.core import (
    access,
    attachment_commands,
    attachments,
    graph,
    labels,
    links,
    mentions,
    work_query,
)
from athena.core.ids import RowIdPath
from athena.core.attachments_api import AttachmentOut
from athena.core.deps import get_conn
from athena.core.identity import is_admin, issue_write_actor, optional_actor

router = APIRouter(prefix="/issues", tags=["aegis"])

STATUS_BY_KIND: dict[str, int] = {
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "forbidden": 403,
    "unauthorized": 401,
}
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


def _with_labels(conn: sqlite3.Connection, issue: dict) -> dict:
    """Attach the issue's labels under a "labels" key. Issues own their core row
    (issues.py); labels are composed on here so the two modules stay in their
    lanes and reads still come back as one object for the client."""
    issue["labels"] = labels.labels_for_issue(conn, issue["id"])
    return issue


def _tagged_issue(
    conn: sqlite3.Connection,
    issue: dict,
    response: Response,
) -> dict:
    """Return the exact public representation and its matching strong ETag."""
    public, current_etag = issue_etags.resource_and_etag(conn, issue)
    response.headers["ETag"] = current_etag
    return public


def _with_labels_many(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Same as _with_labels but for a list, using one bulk query (no N+1)."""
    by_issue = labels.labels_for_issues(conn, [r["id"] for r in rows])
    for r in rows:
        r["labels"] = by_issue.get(r["id"], [])
    return rows


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
    return _tagged_issue(conn, issue, response)


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
        return _with_labels_many(conn, rows)
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
def query_help(_actor: dict | None = Depends(optional_actor)) -> dict:
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
    return _tagged_issue(conn, issue, response)


@router.get("/{issue_id}/backlinks", response_model=list[LinkOut])
def backlinks(
    issue_id: RowIdPath,
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
    issue_id: RowIdPath,
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


@router.get("/{issue_id}/related")
def issue_related_items(
    issue_id: RowIdPath,
    limit: int = graph.DEFAULT_RELATED_LIMIT,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # "What cites what this issue cites, but is not linked to it yet?" — the
    # Aegis twin of the page route: co-citation over the same links the graph
    # walks, direct neighbours deliberately absent (they are /backlinks), the
    # bound disclosed. 404 for missing and hidden alike.
    _issue_for_read(conn, issue_id, actor)
    return graph.related_items(
        conn, kind="issue", node_id=issue_id, actor=actor, limit=limit
    )


@router.get("/{issue_id}/unlinked-mentions")
def issue_unlinked_mentions(
    issue_id: RowIdPath,
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
    return _tagged_issue(conn, updated, response)


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
    issue_id: RowIdPath,
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


@router.get("/{issue_id}/history")
def issue_history_narrative(
    issue_id: RowIdPath,
    limit: int = Query(50, ge=1, le=200),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The operator narrative for one issue, as JSON. Gated like other reads — a
    # hidden/missing issue is a 404. The narrative itself preserves each owning
    # surface's visibility rules (admin sees check-ins, agents see their own
    # controls, etc.) and never infers write authority from the evidence it shows.
    _issue_for_read(conn, issue_id, actor)
    narrative = issue_narrative.build_issue_narrative(
        conn, issue_id, actor=actor, limit=limit
    )
    if narrative is None:  # _issue_for_read already 404s; this is belt-and-suspenders
        raise HTTPException(status_code=404, detail="no such issue")
    return narrative


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
            if_match=_if_match_values(request),
            **fields,
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response)


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
            if_match=_if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response)


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
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, updated)


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
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, updated)


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
            if_match=_if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response)


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
            if_match=_if_match_values(request),
        )
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)
    return _tagged_issue(conn, updated, response)


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
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, updated)


@router.get("/{issue_id}/children", response_model=list[IssueOut])
def list_children(
    issue_id: RowIdPath,
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
    issue_id: RowIdPath,
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
        conn, actor=actor, issue_id=issue_id, body=body
    )


@router.get("/{issue_id}/comments", response_model=list[CommentOut])
def list_comments(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _issue_for_read(conn, issue_id, actor)  # 404 if missing or not visible
    return comments.list_comments(conn, issue_id)


def _author_comment_or_error(
    conn: sqlite3.Connection,
    issue_id: RowIdPath,
    comment_id: RowIdPath,
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
    issue_id: RowIdPath,
    comment_id: RowIdPath,
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
            actor=actor,
            issue_id=issue_id,
            comment_id=comment_id,
            body=body,
        )
    except comment_commands.CommentCommandError as exc:
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=str(exc)
        ) from exc


@router.delete("/{issue_id}/comments/{comment_id}", status_code=204)
def delete_comment(
    issue_id: RowIdPath,
    comment_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    _issue_for_read(conn, issue_id, actor)  # 404 if the issue is missing or hidden
    _author_comment_or_error(conn, issue_id, comment_id, actor, allow_admin=True)
    # The command owns the delete AND its atomic 'comment_deleted' event; a comment that
    # vanished in a race records nothing and 404s.
    if not comment_commands.delete_comment(
        conn, actor=actor, issue_id=issue_id, comment_id=comment_id
    ):
        raise HTTPException(status_code=404, detail="no such comment")


# --- Attachments on an issue ----------------------------------------------


@router.post("/{issue_id}/attachments", response_model=AttachmentOut, status_code=201)
def upload_issue_attachment(
    issue_id: RowIdPath,
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
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=str(exc)
        ) from exc


@router.get("/{issue_id}/attachments", response_model=list[AttachmentOut])
def list_issue_attachments(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like listing comments. 404 if the issue is missing or not visible.
    _issue_for_read(conn, issue_id, actor)
    return attachments.list_for(conn, "issue", issue_id)


# --- Links: typed dependencies between issues -----------------------------


@router.get("/{issue_id}/links", response_model=IssueLinksOut)
def list_links(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Open read, like backlinks/comments. 404 if the issue is missing or not visible,
    # so a hidden/typo'd id reads as not-found rather than three empty lists.
    _issue_for_read(conn, issue_id, actor)
    return dependencies.list_links(conn, issue_id, actor=actor)


@router.post("/{issue_id}/links", response_model=IssueLinksOut, status_code=201)
def add_link(
    issue_id: RowIdPath,
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
    issue_id: RowIdPath,
    relation: str,
    target_id: RowIdPath,
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


# --- Projects: a top-level grouping of issues -----------------------------


# --- Project access control: privacy toggle + membership ------------------
#
# Turning a project private and managing its member roster is creator-OR-admin —
# deliberately WIDER than edit/delete (creator-only, via _project_for_write), because
# an admin must be able to administer access on any project, and the creator must never
# be able to lock themselves out. Reads of the roster are gated by plain visibility:
# anyone who can SEE the project can see who's in it.


# --- Per-project statuses: the configurable lifecycle ---------------------


# --- Labels: a top-level shared vocabulary --------------------------------


# --- Labels on an issue: a write, so creator-or-assignee gated -------------


@router.post("/{issue_id}/labels", response_model=IssueOut, status_code=201)
def attach_label(
    issue_id: RowIdPath,
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
    return _with_labels(conn, issue)


@router.delete("/{issue_id}/labels/{label_id}", response_model=IssueOut)
def detach_label(
    issue_id: RowIdPath,
    label_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    try:
        issue = issue_commands.detach_label(
            conn, actor=actor, issue_id=issue_id, label_id=label_id
        )
    except issue_commands.IssueCommandError as exc:
        raise _issue_command_http_error(exc) from exc
    return _with_labels(conn, issue)


# --- Contributors on an issue: delegating teammates (humans or agents) ------
# The single assignee stays the accountable owner; contributors are additional
# actors working the issue. Adding one is a write on the issue (creator-or-assignee
# gated, same as labels). Reading the list is open, like comments/labels.


class CompleteClaimOut(BaseModel):
    released: bool
    issue_id: int
    issue_status: str
    issue_still_open: bool
    # Whether the released row was one the clock had already expired. Declared
    # because response_model filters: an undeclared key is silently dropped, and
    # this one is the difference between "you stood up" and "you cleared a row
    # you had already lost".
    lapsed: bool
    next: str


HandoffEvidenceItem = Annotated[
    str,
    Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_EVIDENCE_ITEM_CHARS,
    ),
]


class LeaseGenerationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation: str | None = None


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
        response.headers.update(_PRIVATE_LEASE_HEADERS)
        return released
    except issue_commands.IssueCommandError as exc:
        return _issue_command_error_response(exc)


# Sibling modules attach the remaining routes to the routers defined
# above. Imported last so those routers and models already exist.
from athena.aegis import projects_api as _projects_api  # noqa: E402, F401
from athena.aegis import labels_api as _labels_api  # noqa: E402, F401
from athena.aegis import claims_api as _claims_api  # noqa: E402, F401
