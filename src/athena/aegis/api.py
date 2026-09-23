"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py. Handlers live in the
sibling modules imported at the bottom and attach to these routers once the
models exist. ``issues_read_api`` is imported first so ``GET /issues/search``
and the other static paths register before ``GET /issues/{ref}``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from athena.aegis import lease_commands

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


class QueryCountOut(BaseModel):
    # The total behind a page, so a surface can say "showing 50 of 340" instead of
    # implying the page is the whole answer — the same honesty the active-work and
    # work-context surfaces already apply to their bounded windows.
    q: str
    matched: int


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


class LinkMentionIn(BaseModel):
    # Which target to link to. The SOURCE is the issue in the path — this endpoint
    # edits that issue's body.
    target_kind: str
    target_id: int


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


# Sibling modules attach the remaining routes to the routers defined
# above. Imported last so those routers and models already exist.
# Read routes first: static paths must register before GET /{ref}.
from athena.aegis import issues_read_api as _issues_read_api  # noqa: E402, F401
from athena.aegis import issues_write_api as _issues_write_api  # noqa: E402, F401
from athena.aegis import issues_discussion_api as _issues_discussion_api  # noqa: E402, F401
from athena.aegis import projects_api as _projects_api  # noqa: E402, F401
from athena.aegis import labels_api as _labels_api  # noqa: E402, F401
from athena.aegis import claims_api as _claims_api  # noqa: E402, F401
