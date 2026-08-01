"""Browser adapters for Athena Rooms.

The web layer owns no room data and assembles no cross-domain projection. Every
read comes from the shared Rooms projections and every write goes through the
room command owner. Browser identity is always the resolved session actor on
request.state.user; forms intentionally have no actor field.
"""

from __future__ import annotations

import base64
import binascii
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import (
    projects,
    room_briefs,
    room_commands,
    room_context,
    room_timeline,
    rooms,
)
from athena.core import access, identity
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates

router = APIRouter()

_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie"}
_SQLITE_MAX_ID = (1 << 63) - 1
_ROOM_LIST_CURSOR_PREFIX = "athena.web-room-list.v1:"
_ROOM_LIST_CURSOR_MAX_CHARS = 160
_ROOM_LIST_LIMIT = 50
_TIMELINE_LIMIT = 50
_CONTEXT_LIMIT = 12
_EVENT_MAX_CHARS = rooms.MAX_EVENT_BODY_CHARS
_QUESTION_MAX_CHARS = room_context.MAX_QUESTION_CHARS
_REFERENCE_ID_MAX_CHARS = rooms.MAX_REFERENCE_ID_CHARS
_REFERENCE_KINDS = (
    ("issue", "Issue"),
    ("page", "Knowledge page"),
    ("approval", "Approval"),
    ("activity", "Activity"),
    ("handoff", "Handoff"),
    ("dispatch", "Dispatch"),
    ("run", "Run"),
    ("attachment", "Attachment"),
)
_COMMAND_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid": 400,
    "conflict": 409,
}


def _is_sqlite_id(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _SQLITE_MAX_ID
    )


class InvalidRoomListCursor(ValueError):
    """Raised when a browser room-list cursor violates its bounded scope."""


def _encode_room_list_cursor(
    project_id: int, include_archived: bool, room_id: int
) -> str:
    raw = (
        f"{_ROOM_LIST_CURSOR_PREFIX}{project_id}:"
        f"{1 if include_archived else 0}:{room_id}"
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_room_list_cursor(
    cursor: str | None, project_id: int, include_archived: bool
) -> int | None:
    if cursor is None:
        return None
    if not cursor or len(cursor) > _ROOM_LIST_CURSOR_MAX_CHARS:
        raise InvalidRoomListCursor
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        prefix, project, archived, room = raw.rsplit(":", 3)
        parsed = (int(project), int(archived), int(room))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidRoomListCursor from exc
    expected_prefix = _ROOM_LIST_CURSOR_PREFIX.removesuffix(":")
    if (
        prefix != expected_prefix
        or parsed[0] != project_id
        or parsed[1] != (1 if include_archived else 0)
        or not _is_sqlite_id(parsed[2])
        or _encode_room_list_cursor(project_id, include_archived, parsed[2]) != cursor
    ):
        raise InvalidRoomListCursor
    return parsed[2]


def _template(
    request: Request,
    name: str,
    context: dict,
    *,
    status_code: int = 200,
):
    """Render one actor-varying page/fragment with the Rooms cache contract."""
    return get_templates().TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
        headers=_PRIVATE_HEADERS,
    )


def _not_found(request: Request, *, resource_label: str = "Room"):
    """One indistinguishable response for records that are missing or hidden."""
    return _template(
        request,
        "rooms/not_found.html",
        {"resource_label": resource_label},
        status_code=404,
    )


def _request_error(
    request: Request,
    *,
    title: str,
    detail: str,
    status_code: int,
    back_url: str,
):
    return _template(
        request,
        "rooms/error.html",
        {
            "error_title": title,
            "error_detail": detail,
            "error_tone": "error" if status_code >= 400 else "warning",
            "back_url": back_url,
        },
        status_code=status_code,
    )


def _can_manage_rooms(actor: dict | None, project: dict) -> bool:
    """Human project creators/admins may manage room lifecycle.

    The command re-resolves and enforces this inside its write transaction; this
    helper only decides whether browser controls should be rendered.
    """
    return bool(
        actor
        and not actor.get("is_agent")
        and identity.can_write(actor)
        and (identity.is_admin(actor) or actor["id"] == project["created_by"])
    )


def _postable_event_kinds(
    actor: dict | None, *, can_manage: bool
) -> tuple[tuple[str, str], ...]:
    if actor is None or not identity.can_write(actor):
        return ()
    if actor.get("is_agent"):
        return (
            ("message", "Message"),
            ("check_in", "Agent check-in"),
            ("handoff", "Agent handoff"),
            ("evidence", "Evidence note"),
        )
    kinds: list[tuple[str, str]] = [
        ("message", "Operator message"),
        ("decision", "Decision"),
        ("evidence", "Evidence note"),
    ]
    if can_manage:
        kinds.append(("system_notice", "System notice"))
    return tuple(kinds)


def _project_page_context(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    project_id: int,
    include_archived: bool,
    cursor: str | None,
) -> dict | None:
    if not _is_sqlite_id(project_id):
        return None
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        return None

    after_id = _decode_room_list_cursor(cursor, project_id, include_archived)
    room_rows = rooms.list_rooms_page(
        conn,
        project_id,
        actor=actor,
        include_archived=include_archived,
        after_id=after_id,
        limit=_ROOM_LIST_LIMIT + 1,
    )
    has_more = len(room_rows) > _ROOM_LIST_LIMIT
    room_list = room_rows[:_ROOM_LIST_LIMIT]
    next_cursor = (
        _encode_room_list_cursor(
            project_id,
            include_archived,
            int(room_list[-1]["id"]),
        )
        if has_more and room_list
        else None
    )

    brief_candidate = rooms.get_room_by_slug(conn, project_id, "brief")
    brief_room = (
        rooms.get_visible_room(
            conn,
            actor=actor,
            room_id=brief_candidate["id"],
            include_archived=False,
        )
        if brief_candidate is not None
        else None
    )
    brief = None
    brief_error = None
    if brief_room is not None:
        brief = room_briefs.build_live_brief(
            conn,
            brief_room["id"],
            actor=actor,
        )
        if brief is None:
            brief_error = "The visible brief could not be projected for this viewer."

    can_write = actor is not None and identity.can_write(actor)
    return {
        "project": project,
        "room_list": room_list,
        "room_cursor": cursor,
        "room_page": {
            "limit": _ROOM_LIST_LIMIT,
            "next_cursor": next_cursor,
            "has_more": has_more,
        },
        "brief_room": brief_room,
        "brief": brief,
        "brief_error": brief_error,
        "include_archived": include_archived,
        "can_write": can_write,
        "can_edit": bool(
            actor is not None and can_write and actor["id"] == project["created_by"]
        ),
        "can_manage": _can_manage_rooms(actor, project),
    }


def _room_page_context(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    room_id: int,
    cursor: str | None = None,
    context_packet: dict | None = None,
    question: str = "",
    context_error: str | None = None,
    message_error: str | None = None,
    message_draft: str = "",
    message_event_kind: str = "message",
    message_reference_kind: str = "",
    message_reference_id: str = "",
    page_warning: str | None = None,
) -> dict | None:
    if not _is_sqlite_id(room_id):
        return None
    room = rooms.get_visible_room(
        conn,
        actor=actor,
        room_id=room_id,
        include_archived=True,
    )
    if room is None:
        return None

    cursor_activity_id = room_timeline.decode_cursor(cursor, room_id)
    if cursor_activity_id is not None and not _is_sqlite_id(cursor_activity_id):
        raise room_timeline.InvalidCursor("invalid room timeline cursor")

    project = projects.get_project(conn, room["project_id"])
    if project is None:
        # A room cannot outlive its project under the schema, but preserve the
        # visibility-safe response if a corrupt database violates that invariant.
        return None

    timeline = room_timeline.list_timeline(
        conn,
        room_id,
        actor=actor,
        cursor=cursor,
        limit=_TIMELINE_LIMIT,
    )
    agents = room_timeline.list_visible_agents(
        conn,
        room_id,
        actor=actor,
    )
    if timeline is None or agents is None:
        return None

    brief = None
    brief_error = None
    if room["room_type"] in {"project", "brief"}:
        brief = room_briefs.build_live_brief(conn, room_id, actor=actor)
        if brief is None:
            brief_error = "The live brief is unavailable in this visible room."

    can_manage = _can_manage_rooms(actor, project)
    postable_event_kinds = _postable_event_kinds(actor, can_manage=can_manage)
    can_archive = bool(can_manage and room["room_type"] in {"work_item", "agent"})
    historical_link = bool(
        room["room_type"] in {"work_item", "agent"}
        and room.get("link_state") not in {None, "active", "available"}
    )
    can_post = bool(
        postable_event_kinds
        and room["archived_at"] is None
        and room["room_type"] != "brief"
        and not historical_link
    )
    return {
        "room": room,
        "project": project,
        "timeline": timeline,
        "timeline_cursor": cursor,
        "timeline_error": None,
        "agents": agents,
        "agents_error": None,
        "brief": brief,
        "brief_error": brief_error,
        "can_manage": can_manage,
        "can_archive": can_archive,
        "historical_link": historical_link,
        "can_post": can_post,
        "postable_event_kinds": postable_event_kinds,
        "reference_kinds": _REFERENCE_KINDS,
        "event_max_chars": _EVENT_MAX_CHARS,
        "question_max_chars": _QUESTION_MAX_CHARS,
        "reference_id_max_chars": _REFERENCE_ID_MAX_CHARS,
        "context_packet": context_packet,
        "context_error": context_error,
        "question": question,
        "message_error": message_error,
        "message_draft": message_draft,
        "message_event_kind": message_event_kind,
        "message_reference_kind": message_reference_kind,
        "message_reference_id": message_reference_id,
        "page_warning": page_warning,
    }


def _render_room(
    request: Request,
    conn: sqlite3.Connection,
    *,
    room_id: int,
    status_code: int = 200,
    template_name: str = "rooms/detail.html",
    **overrides,
):
    actor = getattr(request.state, "user", None)
    context = _room_page_context(
        conn,
        actor=actor,
        room_id=room_id,
        **overrides,
    )
    if context is None:
        return _not_found(request)
    return _template(
        request,
        template_name,
        context,
        status_code=status_code,
    )


@router.get("/aegis/projects/{project_id}", response_class=HTMLResponse)
@router.get("/aegis/projects/{project_id}/rooms", response_class=HTMLResponse)
def project_rooms(
    request: Request,
    project_id: int,
    archived: bool = False,
    cursor: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Project room index and live brief, visibility-gated before rendering."""
    actor = getattr(request.state, "user", None)
    try:
        context = _project_page_context(
            conn,
            actor=actor,
            project_id=project_id,
            include_archived=archived,
            cursor=cursor,
        )
    except InvalidRoomListCursor:
        back_url = f"/aegis/projects/{project_id}"
        if archived:
            back_url += "?archived=1"
        return _request_error(
            request,
            title="Invalid room-list cursor",
            detail=(
                "The requested room-list cursor is malformed or belongs to "
                "another project or archive view."
            ),
            status_code=400,
            back_url=back_url,
        )
    if context is None:
        return _not_found(request, resource_label="Project")
    return _template(request, "aegis/project_detail.html", context)


@router.get("/aegis/rooms/{room_id}", response_class=HTMLResponse)
def room_detail(
    request: Request,
    room_id: int,
    cursor: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Render one room from shared, visibility-safe projections."""
    try:
        return _render_room(request, conn, room_id=room_id, cursor=cursor)
    except room_timeline.InvalidCursor:
        return _request_error(
            request,
            title="Invalid timeline cursor",
            detail="The requested timeline cursor is malformed or outside the bounded contract.",
            status_code=400,
            back_url=f"/aegis/rooms/{room_id}",
        )


@router.get("/aegis/rooms/{room_id}/timeline", response_class=HTMLResponse)
def room_timeline_page(
    request: Request,
    room_id: int,
    cursor: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Return an older keyset page for HTMX, or the full room without JavaScript."""
    try:
        if request.headers.get("HX-Request"):
            return _render_room(
                request,
                conn,
                room_id=room_id,
                cursor=cursor,
                template_name="rooms/partials/timeline_page.html",
            )
        return _render_room(request, conn, room_id=room_id, cursor=cursor)
    except room_timeline.InvalidCursor:
        return _request_error(
            request,
            title="Invalid timeline cursor",
            detail="The requested timeline cursor is malformed or outside the bounded contract.",
            status_code=400,
            back_url=f"/aegis/rooms/{room_id}",
        )


@router.post(
    "/aegis/rooms/{room_id}/events",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def post_room_event(
    request: Request,
    room_id: int,
    event_kind: str = Form("message"),
    body: str = Form(""),
    reference_kind: str = Form(""),
    reference_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Append an inert room event as the resolved browser actor."""
    if not _is_sqlite_id(room_id):
        return _not_found(request)
    actor = getattr(request.state, "user", None)
    event_kind = event_kind.strip()
    body = body.strip()
    reference_kind = reference_kind.strip()
    reference_id = reference_id.strip()
    try:
        room_commands.post_event(
            conn,
            actor=actor,
            room_id=room_id,
            event_kind=event_kind,
            body=body,
            reference_kind=reference_kind or None,
            reference_id=reference_id or None,
        )
    except room_commands.RoomCommandError as exc:
        if exc.kind == "not_found":
            return _not_found(request)
        status_code = _COMMAND_STATUS.get(exc.kind, 400)
        return _render_room(
            request,
            conn,
            room_id=room_id,
            status_code=200 if request.headers.get("HX-Request") else status_code,
            template_name=(
                "rooms/partials/stream.html"
                if request.headers.get("HX-Request")
                else "rooms/detail.html"
            ),
            message_error=exc.detail,
            message_draft=body,
            message_event_kind=event_kind,
            message_reference_kind=reference_kind,
            message_reference_id=reference_id,
        )

    if request.headers.get("HX-Request"):
        return _render_room(
            request,
            conn,
            room_id=room_id,
            template_name="rooms/partials/stream.html",
        )
    return RedirectResponse(
        f"/aegis/rooms/{room_id}",
        status_code=303,
        headers=_PRIVATE_HEADERS,
    )


@router.post(
    "/aegis/rooms/{room_id}/ask",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def ask_room(
    request: Request,
    room_id: int,
    question: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Assemble the model-free room-context packet; never call a provider."""
    if not _is_sqlite_id(room_id):
        return _not_found(request)
    actor = getattr(request.state, "user", None)
    question = question.strip()
    try:
        packet = room_context.build_room_context(
            conn,
            room_id,
            actor=actor,
            question=question,
            limit=_CONTEXT_LIMIT,
        )
    except room_context.InvalidQuestion as exc:
        detail = (
            str(exc) or "The question is outside the bounded room-context contract."
        )
        return _render_room(
            request,
            conn,
            room_id=room_id,
            status_code=(200 if request.headers.get("HX-Request") else exc.status_code),
            template_name=(
                "rooms/partials/ask_result.html"
                if request.headers.get("HX-Request")
                else "rooms/detail.html"
            ),
            question=question,
            context_error=detail,
        )

    if packet is None:
        return _not_found(request)
    return _render_room(
        request,
        conn,
        room_id=room_id,
        template_name=(
            "rooms/partials/ask_result.html"
            if request.headers.get("HX-Request")
            else "rooms/detail.html"
        ),
        question=question,
        context_packet=packet,
    )


@router.post(
    "/aegis/rooms/{room_id}/archive",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def archive_room(
    request: Request,
    room_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Archive a room through the shared command; the browser supplies no actor."""
    if not _is_sqlite_id(room_id):
        return _not_found(request)
    actor = getattr(request.state, "user", None)
    try:
        room_commands.archive_room(conn, actor=actor, room_id=room_id)
    except room_commands.RoomCommandError as exc:
        if exc.kind == "not_found":
            return _not_found(request)
        return _render_room(
            request,
            conn,
            room_id=room_id,
            status_code=_COMMAND_STATUS.get(exc.kind, 400),
            page_warning=exc.detail,
        )
    return RedirectResponse(
        f"/aegis/rooms/{room_id}",
        status_code=303,
        headers=_PRIVATE_HEADERS,
    )
