"""Strict REST adapters for Athena Rooms."""

from __future__ import annotations

import base64
import binascii
import sqlite3
from typing import Annotated, Any, Literal, Never

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from athena.aegis import (
    room_briefs,
    room_commands,
    room_context,
    room_timeline,
    rooms,
)
from athena.core import access, db
from athena.core.deps import get_conn
from athena.core.identity import optional_actor


_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, X-Athena-Actor",
}


class _PrivateRoomRoute(APIRoute):
    """Keep FastAPI-generated validation failures inside the room read envelope."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def private_route_handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={"detail": jsonable_encoder(exc.errors())},
                    headers=_PRIVATE_HEADERS,
                )

        return private_route_handler


router = APIRouter(tags=["aegis", "rooms"], route_class=_PrivateRoomRoute)


_ROOM_CURSOR_PREFIX = "athena.rooms-list.v1:"
DEFAULT_ROOM_LIMIT = 50
MAX_ROOM_LIMIT = 100

RoomType = Literal["project", "work_item", "agent", "brief"]
RoomVisibility = Literal["project", "members"]
EventKind = Literal[
    "message", "check_in", "handoff", "decision", "evidence", "system_notice"
]
ReferenceKind = Literal[
    "issue",
    "page",
    "approval",
    "activity",
    "handoff",
    "dispatch",
    "run",
    "attachment",
]
Classification = Literal["human", "agent", "system", "approval", "evidence", "imported"]
SqliteId = Annotated[int, Path(ge=1, le=rooms.MAX_SQLITE_ID)]
BodySqliteId = Annotated[int, Field(strict=True, ge=1, le=rooms.MAX_SQLITE_ID)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )


class RoomOut(StrictModel):
    id: int
    project_id: int
    slug: str
    room_type: RoomType
    title: str
    purpose: str
    visibility: RoomVisibility
    issue_id: int | None
    agent_id: int | None
    created_by: int
    created_at: str
    updated_at: str
    archived_at: str | None
    archived: bool
    is_detached: bool
    link_state: str
    degraded_reason: str | None


class ActorOut(StrictModel):
    id: int
    name: str
    is_agent: bool


class TargetOut(StrictModel):
    kind: str
    id: int


class ReferenceOut(StrictModel):
    kind: ReferenceKind
    id: str | None
    available: bool
    unavailable_reason: str | None
    title: str | None
    receipt: str | None


class TimelineItemOut(StrictModel):
    activity_id: int
    classification: Classification
    event_kind: EventKind | None
    actor: ActorOut
    verb: str
    body: str
    body_truncated: bool
    created_at: str
    target: TargetOut
    run_id: str | None
    run_receipt: str | None
    run_receipt_unavailable_reason: str | None
    parent_run_id: str | None
    forked_from_event_id: int | None
    imported_at: str | None
    reference: ReferenceOut | None
    supersedes_event_id: int | None
    successor_event_id: int | None
    is_current: bool
    content_sha256: str | None


class TimelinePageMetaOut(StrictModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class TimelineOut(StrictModel):
    room: RoomOut
    items: list[TimelineItemOut]
    page: TimelinePageMetaOut


class CredentialPostureOut(StrictModel):
    live_token_count: int
    revoked_token_count: int
    effective_scopes: list[str]
    last_used_at: str | None
    scope_scan_clipped: bool


class CapabilityOut(StrictModel):
    status: Literal["available", "unavailable"]
    token_scopes: list[str] | None
    credential_posture: CredentialPostureOut | None
    unavailable_reason: str | None


class ClaimOut(StrictModel):
    issue_id: int
    key: str
    title: str
    priority: str
    status: str
    claimed_at: str
    expires_at: str
    generation: str
    receipt: str
    semantics: Literal["recorded_claim_not_process_liveness"]


class ClaimGroupOut(StrictModel):
    items: list[ClaimOut]
    visible_total: int
    clipped: bool


class CheckInOut(StrictModel):
    run_id: str
    first_seen_at: str
    last_seen_at: str
    reporting_state: str
    age_seconds: int
    semantics: Literal["cooperative_report_not_process_liveness"]
    receipt: str | None


class ContributionOut(StrictModel):
    activity_id: int
    verb: str
    target_kind: str
    target_id: int
    created_at: str
    run_id: str | None


class ContributionGroupOut(StrictModel):
    items: list[ContributionOut]
    visible_total: int
    clipped: bool


class LineageOut(StrictModel):
    run_id: str
    last_activity_id: int
    last_activity_at: str
    receipt: str


class LineageGroupOut(StrictModel):
    items: list[LineageOut]
    clipped: bool
    unavailable_reason: str | None


class VisibleAgentOut(StrictModel):
    id: int
    name: str
    role: str
    is_agent: bool
    account_state: Literal["enabled", "revoked", "paused", "unavailable"]
    enabled: bool
    revoked: bool
    paused_at: str | None
    capability: CapabilityOut
    current_claims: ClaimGroupOut
    latest_check_in: CheckInOut | None
    latest_check_in_unavailable_reason: str | None
    recent_contributions: ContributionGroupOut
    visible_lineage: LineageGroupOut


class VisibleAgentGroupOut(StrictModel):
    items: list[VisibleAgentOut]
    visible_total: int
    clipped: bool


class RoomDetailOut(StrictModel):
    room: RoomOut
    visible_agents: VisibleAgentGroupOut


class RoomListPageOut(StrictModel):
    items: list[RoomOut]
    limit: int
    next_cursor: str | None
    has_more: bool


class RoomCreateIn(StrictModel):
    room_type: RoomType
    title: Annotated[str, Field(min_length=1, max_length=rooms.MAX_TITLE_CHARS)]
    purpose: Annotated[str, Field(max_length=rooms.MAX_PURPOSE_CHARS)] = ""
    visibility: RoomVisibility = "members"
    slug: (
        Annotated[str, Field(min_length=1, max_length=rooms.MAX_SLUG_CHARS)] | None
    ) = None
    issue_id: BodySqliteId | None = None
    agent_id: BodySqliteId | None = None


class RoomEventIn(StrictModel):
    event_kind: EventKind
    body: Annotated[str, Field(min_length=1, max_length=rooms.MAX_EVENT_BODY_CHARS)]
    reference_kind: ReferenceKind | None = None
    reference_id: (
        Annotated[str, Field(min_length=1, max_length=rooms.MAX_REFERENCE_ID_CHARS)]
        | BodySqliteId
        | None
    ) = None
    supersedes_event_id: BodySqliteId | None = None


class RoomContextIn(StrictModel):
    question: Annotated[
        str, Field(min_length=1, max_length=room_context.MAX_QUESTION_CHARS)
    ]
    limit: Annotated[int, Field(ge=1, le=room_context.MAX_SELECTION_LIMIT)] = (
        room_context.DEFAULT_SELECTION_LIMIT
    )


class ReceiptOut(StrictModel):
    method: Literal["GET"]
    path: str


class RequesterOut(StrictModel):
    id: int | None
    name: str
    role: str | None
    is_agent: bool


class ContextQueryOut(StrictModel):
    normalized: str
    characters: int
    max_characters: int


class ContextRecordOut(StrictModel):
    record_type: Literal["issue", "page", "activity"]
    record_id: int | str
    title: str
    snippet: str
    source_revision: int | str | None
    source_activity_id: int | None
    digest_sha256: str | None
    receipt: ReceiptOut
    rank: int
    snippet_truncated: bool


class ContextBoundsOut(StrictModel):
    query_term_limit: int
    selected_query_terms: int
    query_terms_clipped: bool
    scope_issue_limit: int
    scoped_issue_count: int
    scope_issues_clipped: bool
    related_page_limit: int
    related_page_count: int
    related_pages_clipped: bool
    candidate_limit: int
    selection_limit: int
    visible_candidate_count: int
    selected_count: int
    candidate_scan_clipped: bool
    candidate_count_is_lower_bound: bool
    selection_clipped: bool


class ContextOmissionOut(StrictModel):
    kind: str
    reason: str
    visible_count: int


class ContextTruncationOut(StrictModel):
    query: bool
    query_terms: bool
    scope: bool
    candidate_scan: bool
    selection: bool
    snippets: int


class ContextUncertaintyOut(StrictModel):
    notice: str
    does_not_assert: list[
        Literal["truth", "completeness", "approval", "current_execution", "causality"]
    ]


class RoomContextOut(StrictModel):
    schema_: Literal["athena.room-context.v1"] = Field(alias="schema")
    room: RoomOut
    requester: RequesterOut
    query: ContextQueryOut
    snapshot_at: str
    records: list[ContextRecordOut]
    bounds: ContextBoundsOut
    omissions: list[ContextOmissionOut]
    truncation: ContextTruncationOut
    uncertainty: ContextUncertaintyOut


class BriefItemOut(StrictModel):
    id: int | None = None
    key: str | None = None
    title: str | None = None
    name: str | None = None
    summary: str | None = None
    verb: str | None = None
    kind: str | None = None
    status: str | None = None
    priority: str | None = None
    body: str | None = None
    detail: str | None = None
    actor: ActorOut | None = None
    created_at: str | None = None
    updated_at: str | None = None
    activity_id: int | None = None
    receipt: str | None = None
    current_claims: ClaimGroupOut | None = None
    latest_check_in: CheckInOut | None = None


class BriefGroupOut(StrictModel):
    items: list[BriefItemOut]
    visible_total: int
    clipped: bool
    unavailable_reason: str | None


class RoomBriefOut(StrictModel):
    schema_: Literal["athena.room-brief.v1"] = Field(alias="schema")
    room: RoomOut
    snapshot_at: str
    purpose: str
    open_priority: BriefGroupOut
    blockers: BriefGroupOut
    agents: BriefGroupOut
    decisions: BriefGroupOut
    knowledge: BriefGroupOut
    recent_timeline: BriefGroupOut
    uncertainty: list[str]


def _private(response: Response) -> None:
    response.headers.update(_PRIVATE_HEADERS)


def _not_found(detail: str) -> Never:
    raise HTTPException(status_code=404, detail=detail, headers=_PRIVATE_HEADERS)


def _raise_command(exc: room_commands.RoomCommandError) -> Never:
    statuses = {
        "unauthorized": 401,
        "forbidden": 403,
        "not_found": 404,
        "invalid": 422,
        "conflict": 409,
    }
    raise HTTPException(
        status_code=statuses[exc.kind],
        detail=exc.detail,
        headers=_PRIVATE_HEADERS,
    ) from exc


def _encode_room_cursor(project_id: int, include_archived: bool, room_id: int) -> str:
    if not rooms.is_sqlite_id(project_id) or not rooms.is_sqlite_id(room_id):
        raise ValueError("cursor ids must be SQLite positive integers")

    raw = (
        f"{_ROOM_CURSOR_PREFIX}{project_id}:{1 if include_archived else 0}:{room_id}"
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_room_cursor(
    cursor: str | None, project_id: int, include_archived: bool
) -> int | None:
    if cursor is None:
        return None
    if not cursor or len(cursor) > 160:
        raise HTTPException(
            status_code=422,
            detail="invalid room list cursor",
            headers=_PRIVATE_HEADERS,
        )
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        prefix, project, archived, room = raw.rsplit(":", 3)
        parsed = (int(project), int(archived), int(room))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid room list cursor",
            headers=_PRIVATE_HEADERS,
        ) from exc
    expected_prefix = _ROOM_CURSOR_PREFIX.removesuffix(":")
    if (
        prefix != expected_prefix
        or parsed[0] != project_id
        or not rooms.is_sqlite_id(parsed[0])
        or parsed[1] != (1 if include_archived else 0)
        or not rooms.is_sqlite_id(parsed[2])
        or _encode_room_cursor(project_id, include_archived, parsed[2]) != cursor
    ):
        raise HTTPException(
            status_code=422,
            detail="invalid room list cursor",
            headers=_PRIVATE_HEADERS,
        )
    return parsed[2]


@router.get("/projects/{project_id}/rooms", response_model=RoomListPageOut)
def list_project_rooms(
    project_id: SqliteId,
    response: Response,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROOM_LIMIT)] = DEFAULT_ROOM_LIMIT,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not access.can_see_project(conn, actor, project_id):
        _not_found("no such project")
    after_id = _decode_room_cursor(cursor, project_id, include_archived)
    rows = rooms.list_rooms_page(
        conn,
        project_id,
        actor=actor,
        include_archived=include_archived,
        after_id=after_id,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    selected = rows[:limit]
    _private(response)
    return {
        "items": [room_timeline.public_room(room) for room in selected],
        "limit": limit,
        "next_cursor": (
            _encode_room_cursor(project_id, include_archived, int(selected[-1]["id"]))
            if has_more and selected
            else None
        ),
        "has_more": has_more,
    }


@router.post(
    "/projects/{project_id}/rooms",
    response_model=RoomOut,
    status_code=201,
)
def create_project_room(
    project_id: SqliteId,
    body: RoomCreateIn,
    response: Response,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        created = room_commands.create_room(
            conn, actor=actor, project_id=project_id, **body.model_dump()
        )
    except room_commands.RoomCommandError as exc:
        _raise_command(exc)
    _private(response)
    return room_timeline.public_room(created)


@router.get("/rooms/{room_id}", response_model=RoomDetailOut)
def get_room(
    room_id: SqliteId,
    response: Response,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    with db.transaction(conn):
        room = rooms.get_visible_room(
            conn, actor=actor, room_id=room_id, include_archived=True
        )
        if room is None:
            _not_found("no such room")
        visible_agents = room_timeline.list_visible_agents(conn, room_id, actor=actor)
        assert visible_agents is not None
        payload = {
            "room": room_timeline.public_room(room),
            "visible_agents": visible_agents,
        }
    _private(response)
    return payload


@router.post("/rooms/{room_id}/archive", response_model=RoomOut)
def archive_room(
    room_id: SqliteId,
    response: Response,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        archived = room_commands.archive_room(conn, actor=actor, room_id=room_id)
    except room_commands.RoomCommandError as exc:
        _raise_command(exc)
    _private(response)
    return room_timeline.public_room(archived)


@router.get("/rooms/{room_id}/timeline", response_model=TimelineOut)
def get_room_timeline(
    room_id: SqliteId,
    response: Response,
    cursor: str | None = None,
    limit: Annotated[
        int, Query(ge=1, le=room_timeline.MAX_LIMIT)
    ] = room_timeline.DEFAULT_LIMIT,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        payload = room_timeline.list_timeline(
            conn, room_id, actor=actor, cursor=cursor, limit=limit
        )
    except room_timeline.InvalidCursor as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers=_PRIVATE_HEADERS,
        ) from exc
    if payload is None:
        _not_found("no such room")
    _private(response)
    return payload


@router.post("/rooms/{room_id}/events", response_model=TimelineItemOut, status_code=201)
def post_room_event(
    room_id: SqliteId,
    body: RoomEventIn,
    response: Response,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        event = room_commands.post_event(
            conn, actor=actor, room_id=room_id, **body.model_dump()
        )
    except room_commands.RoomCommandError as exc:
        _raise_command(exc)
    room = rooms.get_visible_room(
        conn, actor=actor, room_id=room_id, include_archived=True
    )
    if room is None:
        _not_found("no such room")
    projected = room_timeline._timeline_item(conn, room, actor, event)
    _private(response)
    return projected


@router.post("/rooms/{room_id}/context", response_model=RoomContextOut)
def get_room_context(
    room_id: SqliteId,
    body: RoomContextIn,
    response: Response,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        payload = room_context.build_room_context(
            conn,
            room_id,
            actor=actor,
            question=body.question,
            limit=body.limit,
        )
    except room_context.InvalidQuestion as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers=_PRIVATE_HEADERS,
        ) from exc
    if payload is None:
        _not_found("no such room")
    _private(response)
    return payload


@router.get("/rooms/{room_id}/brief", response_model=RoomBriefOut)
def get_room_brief(
    room_id: SqliteId,
    response: Response,
    cursor: str | None = None,
    actor: dict[str, Any] | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        payload = room_briefs.build_live_brief(
            conn, room_id, actor=actor, cursor=cursor
        )
    except room_timeline.InvalidCursor as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers=_PRIVATE_HEADERS,
        ) from exc
    if payload is None:
        _not_found("no such room")
    _private(response)
    return payload
