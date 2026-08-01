"""Application commands for every Athena Room mutation.

Commands accept only a resolved actor, repeat live authorization under
BEGIN IMMEDIATE, and atomically persist room state, append-only activity,
typed room-event metadata, and derived search state. They perform no external I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Literal

from athena.aegis import rooms
from athena.core import access, activity, db, identity, search, tokens

ErrorKind = Literal[
    "unauthorized",
    "forbidden",
    "not_found",
    "invalid",
    "conflict",
]


class RoomCommandError(Exception):
    """Transport-neutral rejection owned by the Rooms application boundary."""

    def __init__(self, kind: ErrorKind, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_GENERATED_SLUG_PREFIXES = ("agent-", "work-item-")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PATH_RE = re.compile(
    r"(?:^|[\s(\"'\x60])"
    r"(?:"
    r"file://[^\s<>()\[\]{}\"']+|"
    r"(?:~|\.\.?)[/\\][^\s<>()\[\]{}\"']+|"
    r"[A-Za-z]:[\\/][^\s<>()\[\]{}\"']+|"
    r"/(?!/)[^\s<>()\[\]{}\"']+|"
    r"\\\\[^\s<>()\[\]{}\"']+|"
    r"(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+\."
    r"(?:env|ini|cfg|conf|toml|ya?ml|json|xml|sql|pyi?|[cm]?js|jsx|tsx?|"
    r"sh|bash|zsh|fish|ps1|pem|key|crt|cer|p12|db|sqlite3?|md|txt|log|"
    r"csv|tsv|pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|webp|svg)|"
    r"(?:[A-Za-z0-9][A-Za-z0-9_.-]*\."
    r"(?:env|ini|cfg|conf|toml|ya?ml|json|xml|sql|pyi?|[cm]?js|jsx|tsx?|"
    r"sh|bash|zsh|fish|ps1|pem|key|crt|cer|p12|db|sqlite3?|md|txt|log|"
    r"csv|tsv|pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|webp|svg)|"
    r"\.env(?:\.[A-Za-z0-9_-]+)?|Dockerfile|Makefile|Procfile)"
    r")(?=$|[\s),.;:!?\"'\x60])",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?:"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bAuthorization\s*:\s*Bearer\s+\S+|"
    r"\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}|"
    r"\b(?:ath_|gh[pousr]_|sk-)[A-Za-z0-9_-]{16,}|"
    r"\bAKIA[0-9A-Z]{16}\b"
    r")",
    re.IGNORECASE,
)
_LOG_OR_PROVIDER_RE = re.compile(
    r"(?:(?:^|\n)\s*Traceback \(most recent call last\):|"
    r"\b(?:stdout|stderr|raw[_ -]?provider[_ -]?response)\s*:|"
    r"(?:^|\n)\s*\d{4}-\d{2}-\d{2}[T ][^\n ]+\s+"
    r"(?:debug|info|warning|error|critical)\b|"
    r'"(?:choices|prompt_tokens|completion_tokens|raw_response)"\s*:)',
    re.IGNORECASE,
)

_INTEGER_REFERENCE_KINDS = frozenset(
    {"issue", "page", "approval", "activity", "handoff", "dispatch", "attachment"}
)
_AGENT_EVENT_KINDS = frozenset({"check_in", "handoff"})


def unsafe_room_payload_reason(
    value: str, *, reject_structured: bool = False
) -> str | None:
    """Classify text that may not cross a Room payload boundary."""
    if _CONTROL_RE.search(value):
        return "control_characters"
    if _PATH_RE.search(value):
        return "filesystem_path"
    if _SECRET_RE.search(value):
        return "credential_like"
    if _LOG_OR_PROVIDER_RE.search(value):
        return "log_or_provider_payload"
    if reject_structured and value.lstrip()[:1] in "[{":
        try:
            structured = json.loads(value)
        except (TypeError, ValueError):
            structured = None
        if isinstance(structured, (dict, list)):
            return "structured_payload"
    return None


def _strict_positive_id(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= rooms.MAX_SQLITE_ID
    ):
        raise RoomCommandError("invalid", f"{field} must be a positive integer")
    return value


def _safe_plain_text(
    value: object,
    *,
    field: str,
    max_chars: int,
    allow_empty: bool = False,
    multiline: bool = True,
    reject_structured: bool = False,
) -> str:
    if not isinstance(value, str):
        raise RoomCommandError("invalid", f"{field} must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text and not allow_empty:
        raise RoomCommandError("invalid", f"{field} cannot be empty")
    if len(text) > max_chars:
        raise RoomCommandError(
            "invalid", f"{field} must be at most {max_chars} characters"
        )
    if not multiline and ("\n" in text or "\t" in text):
        raise RoomCommandError("invalid", f"{field} must be one line")
    if _CONTROL_RE.search(text):
        raise RoomCommandError("invalid", f"{field} contains control characters")
    if _PATH_RE.search(text):
        raise RoomCommandError("invalid", f"{field} must not contain filesystem paths")
    if _SECRET_RE.search(text):
        raise RoomCommandError(
            "invalid", f"{field} must not contain credential-like material"
        )
    if _LOG_OR_PROVIDER_RE.search(text):
        raise RoomCommandError(
            "invalid", f"{field} must not contain log or provider payloads"
        )
    if reject_structured and text[:1] in "[{":
        try:
            structured = json.loads(text)
        except (TypeError, ValueError):
            structured = None
        if isinstance(structured, (dict, list)):
            raise RoomCommandError(
                "invalid", f"{field} must be plain coordination prose"
            )
    return text


def _normalize_slug(
    value: object | None, *, room_type: str, link_id: int | None
) -> str:
    if room_type == "project":
        generated = "main"
    elif room_type == "brief":
        generated = "brief"
    elif room_type == "work_item":
        generated = f"work-item-{link_id}"
    else:
        generated = f"agent-{link_id}"
    if value is None:
        return generated
    if not isinstance(value, str):
        raise RoomCommandError("invalid", "slug must be a string")
    slug = value.strip().lower()
    if len(slug) > rooms.MAX_SLUG_CHARS or _SLUG_RE.fullmatch(slug) is None:
        raise RoomCommandError(
            "invalid",
            "slug must use 1-80 lowercase letters, digits, and single hyphens",
        )
    if slug != generated and (
        slug in {"main", "brief"} or slug.startswith(_GENERATED_SLUG_PREFIXES)
    ):
        raise RoomCommandError(
            "invalid", "generated room slug is reserved for its linked record"
        )
    return slug


def _live_room_writer(conn: sqlite3.Connection, actor: dict | None) -> dict:
    """Re-resolve user, pause, bearer ownership, and scope inside the write tx."""
    if actor is None:
        raise RoomCommandError("unauthorized", "authentication required")
    actor_id = actor.get("id")
    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1:
        raise RoomCommandError("unauthorized", "authentication required")
    row = conn.execute("SELECT * FROM users WHERE id = ?", (actor_id,)).fetchone()
    if row is None:
        raise RoomCommandError("unauthorized", "authentication required")
    live = dict(row)
    live["is_agent"] = bool(live.get("is_agent"))
    if live.get("paused_at") is not None:
        raise RoomCommandError("forbidden", "actor is paused")
    if not identity.can_write(live):
        raise RoomCommandError("forbidden", "viewer role is read-only")

    token_id = actor.get("_token_id")
    if token_id is not None:
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 1:
            raise RoomCommandError("forbidden", "live bearer token required")
        token = conn.execute(
            "SELECT token_hash, scopes FROM api_tokens "
            "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (token_id, actor_id),
        ).fetchone()
        if token is None or not tokens.is_valid_hash(token["token_hash"]):
            raise RoomCommandError("forbidden", "live bearer token required")
        try:
            scopes = tokens.parse_scopes(token["scopes"])
        except ValueError as exc:
            raise RoomCommandError(
                "forbidden", f"token scope required: {tokens.ROOMS_WRITE_SCOPE}"
            ) from exc
        live["_token_id"] = token_id
        live["_token_scopes"] = scopes
    elif live["is_agent"]:
        raise RoomCommandError("forbidden", "agent writes require a bearer token")

    if not identity.token_has_scope(live, tokens.ROOMS_WRITE_SCOPE):
        raise RoomCommandError(
            "forbidden", f"token scope required: {tokens.ROOMS_WRITE_SCOPE}"
        )
    return live


def _visible_room(conn: sqlite3.Connection, actor: dict, room_id: int) -> dict:
    room = rooms.get_room(conn, room_id)
    if room is None or not rooms.can_see_room(conn, actor, room):
        raise RoomCommandError("not_found", "no such room")
    return room


def _is_project_governor(
    conn: sqlite3.Connection, actor: dict, *, project_id: int, scope_key: str
) -> bool:
    if actor.get("is_agent"):
        return False
    if identity.is_admin(actor):
        return True
    row = conn.execute(
        "SELECT created_by FROM projects WHERE id = ? AND activity_scope_key = ?",
        (project_id, scope_key),
    ).fetchone()
    return row is not None and row["created_by"] == actor["id"]


def _normalize_reference(
    reference_kind: object | None, reference_id: object | None
) -> tuple[str | None, str | None]:
    if reference_kind is None and reference_id is None:
        return None, None
    if not isinstance(reference_kind, str) or reference_id is None:
        raise RoomCommandError(
            "invalid", "reference_kind and reference_id must be provided together"
        )
    kind = reference_kind.strip().lower()
    if kind not in rooms.REFERENCE_KINDS:
        raise RoomCommandError("invalid", "unsupported reference kind")
    if kind in _INTEGER_REFERENCE_KINDS:
        if isinstance(reference_id, bool):
            raise RoomCommandError("invalid", "reference_id must be a positive id")
        if isinstance(reference_id, int):
            if not 1 <= reference_id <= rooms.MAX_SQLITE_ID:
                raise RoomCommandError("invalid", "reference_id must be a positive id")
            return kind, str(reference_id)
        if not isinstance(reference_id, str):
            raise RoomCommandError("invalid", "reference_id must be a positive id")
        normalized = reference_id.strip()
        if (
            not normalized.isascii()
            or not normalized.isdecimal()
            or normalized.startswith("0")
            or int(normalized) > rooms.MAX_SQLITE_ID
        ):
            raise RoomCommandError("invalid", "reference_id must be a positive id")
        return kind, normalized

    if not isinstance(reference_id, str):
        raise RoomCommandError("invalid", "reference_id must be a string")
    normalized = reference_id.strip()
    if (
        not normalized
        or len(normalized) > rooms.MAX_REFERENCE_ID_CHARS
        or _CONTROL_RE.search(normalized)
        or _PATH_RE.search(normalized)
        or _SECRET_RE.search(normalized)
        or _LOG_OR_PROVIDER_RE.search(normalized)
    ):
        raise RoomCommandError("invalid", "reference_id is invalid")
    return kind, normalized


def _unavailable_reference() -> RoomCommandError:
    return RoomCommandError("invalid", "referenced record is unavailable")


def _authorize_reference(
    conn: sqlite3.Connection,
    actor: dict,
    room: dict,
    kind: str | None,
    reference_id: str | None,
) -> None:
    """Resolve and authorize a controlled source without copying its payload."""
    if kind is None or reference_id is None:
        return
    integer_id = int(reference_id) if kind in _INTEGER_REFERENCE_KINDS else 0
    if kind == "issue":
        row = conn.execute(
            "SELECT project_id FROM issues WHERE id = ?", (integer_id,)
        ).fetchone()
        if (
            row is None
            or row["project_id"] != room["project_id"]
            or not access.can_see_issue(conn, actor, integer_id)
        ):
            raise _unavailable_reference()
        return
    if kind == "page":
        if not access.can_see_page(conn, actor, integer_id):
            raise _unavailable_reference()
        return
    if kind == "approval":
        row = conn.execute(
            "SELECT target_kind, target_id FROM approval_requests WHERE id = ?",
            (integer_id,),
        ).fetchone()
        if not identity.is_admin(actor) or row is None or row["target_kind"] != "issue":
            raise _unavailable_reference()
        issue = conn.execute(
            "SELECT project_id FROM issues WHERE id = ?", (row["target_id"],)
        ).fetchone()
        if issue is None or issue["project_id"] != room["project_id"]:
            raise _unavailable_reference()
        return
    if kind == "activity":
        visible = activity.get_visible_activity(conn, integer_id, actor)
        scoped = conn.execute(
            "SELECT 1 FROM activity_visibility_projects "
            "WHERE event_id = ? AND project_scope_key = ?",
            (integer_id, room["project_scope_key"]),
        ).fetchone()
        if visible is None or visible["imported_at"] is not None or scoped is None:
            raise _unavailable_reference()
        return
    if kind == "handoff":
        row = conn.execute(
            "SELECT i.project_id FROM issue_claim_handoffs h "
            "JOIN issues i ON i.id = h.issue_id WHERE h.id = ?",
            (integer_id,),
        ).fetchone()
        if row is None or row["project_id"] != room["project_id"]:
            raise _unavailable_reference()
        return
    if kind == "dispatch":
        row = conn.execute(
            "SELECT i.project_id FROM icarus_dispatches d "
            "JOIN issues i ON i.id = d.work_item_id WHERE d.id = ?",
            (integer_id,),
        ).fetchone()
        if row is None or row["project_id"] != room["project_id"]:
            raise _unavailable_reference()
        return
    if kind == "run":
        in_project = conn.execute(
            "SELECT 1 FROM activity a "
            "JOIN activity_visibility_projects avp ON avp.event_id = a.id "
            "WHERE a.run_id = ? AND a.imported_at IS NULL "
            "AND avp.project_scope_key = ? "
            "AND NOT EXISTS (SELECT 1 FROM activity foreign_activity "
            "WHERE foreign_activity.run_id = a.run_id "
            "AND foreign_activity.imported_at IS NOT NULL) LIMIT 1",
            (reference_id, room["project_scope_key"]),
        ).fetchone()
        if in_project is None or not activity.can_see_complete_run(
            conn, reference_id, actor
        ):
            raise _unavailable_reference()
        return
    if kind == "attachment":
        row = conn.execute(
            "SELECT target_kind, target_id FROM attachments WHERE id = ?",
            (integer_id,),
        ).fetchone()
        if row is None:
            raise _unavailable_reference()
        if row["target_kind"] == "issue":
            issue = conn.execute(
                "SELECT project_id FROM issues WHERE id = ?", (row["target_id"],)
            ).fetchone()
            if (
                issue is None
                or issue["project_id"] != room["project_id"]
                or not access.can_see_issue(conn, actor, row["target_id"])
            ):
                raise _unavailable_reference()
            return
        if row["target_kind"] == "page" and access.can_see_page(
            conn, actor, row["target_id"]
        ):
            return
        raise _unavailable_reference()
    raise RoomCommandError("invalid", "unsupported reference kind")


def create_room(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    project_id: object,
    room_type: object,
    title: object,
    purpose: object = "",
    visibility: object = "members",
    slug: object | None = None,
    issue_id: object | None = None,
    agent_id: object | None = None,
) -> dict:
    """Create one linked room and its internal-only audit fact atomically."""
    normalized_project_id = _strict_positive_id(project_id, "project_id")
    if not isinstance(room_type, str):
        raise RoomCommandError("invalid", "room_type must be a string")
    normalized_type = room_type.strip().lower()
    if normalized_type not in rooms.ROOM_TYPES:
        raise RoomCommandError("invalid", "unsupported room type")
    normalized_title = _safe_plain_text(
        title, field="title", max_chars=rooms.MAX_TITLE_CHARS, multiline=False
    )
    normalized_purpose = _safe_plain_text(
        purpose,
        field="purpose",
        max_chars=rooms.MAX_PURPOSE_CHARS,
        allow_empty=True,
    )
    if not isinstance(visibility, str):
        raise RoomCommandError("invalid", "visibility must be a string")
    normalized_visibility = visibility.strip().lower()
    if normalized_visibility not in rooms.ROOM_VISIBILITIES:
        raise RoomCommandError("invalid", "unsupported room visibility")

    normalized_issue_id = (
        None if issue_id is None else _strict_positive_id(issue_id, "issue_id")
    )
    normalized_agent_id = (
        None if agent_id is None else _strict_positive_id(agent_id, "agent_id")
    )
    if normalized_type in {"project", "brief"}:
        if normalized_issue_id is not None or normalized_agent_id is not None:
            raise RoomCommandError("invalid", "room type does not accept a link")
        link_id = None
    elif normalized_type == "work_item":
        if normalized_issue_id is None or normalized_agent_id is not None:
            raise RoomCommandError("invalid", "work_item room requires issue_id")
        link_id = normalized_issue_id
    else:
        if normalized_agent_id is None or normalized_issue_id is not None:
            raise RoomCommandError("invalid", "agent room requires agent_id")
        link_id = normalized_agent_id
    normalized_slug = _normalize_slug(slug, room_type=normalized_type, link_id=link_id)

    with db.transaction(conn, immediate=True):
        live = _live_room_writer(conn, actor)
        project = conn.execute(
            "SELECT id, activity_scope_key FROM projects WHERE id = ?",
            (normalized_project_id,),
        ).fetchone()
        if project is None or not access.can_see_project(
            conn, live, normalized_project_id
        ):
            raise RoomCommandError("not_found", "no such project")
        if not _is_project_governor(
            conn,
            live,
            project_id=normalized_project_id,
            scope_key=project["activity_scope_key"],
        ):
            raise RoomCommandError(
                "forbidden", "human project creator or admin required"
            )
        if normalized_type == "work_item":
            linked = conn.execute(
                "SELECT 1 FROM issues WHERE id = ? AND project_id = ?",
                (normalized_issue_id, normalized_project_id),
            ).fetchone()
            if linked is None:
                raise RoomCommandError("invalid", "linked work item is unavailable")
        elif normalized_type == "agent":
            linked = conn.execute(
                "SELECT 1 FROM users WHERE id = ? AND is_agent = 1",
                (normalized_agent_id,),
            ).fetchone()
            if linked is None:
                raise RoomCommandError("invalid", "linked agent is unavailable")
        try:
            created = rooms.create_room(
                conn,
                project_id=normalized_project_id,
                project_scope_key=project["activity_scope_key"],
                slug=normalized_slug,
                room_type=normalized_type,
                title=normalized_title,
                purpose=normalized_purpose,
                visibility=normalized_visibility,
                issue_id=normalized_issue_id,
                agent_id=normalized_agent_id,
                created_by=live["id"],
                commit=False,
            )
        except sqlite3.IntegrityError as exc:
            raise RoomCommandError("conflict", "room already exists") from exc
        activity.record(
            conn,
            actor_id=live["id"],
            verb="created_room",
            target_kind="room",
            target_id=created["id"],
            detail=f"{normalized_type} room {normalized_slug}",
            delivery_eligible=False,
            commit=False,
        )
        return created


def archive_room(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    room_id: object,
) -> dict:
    """Archive an operational room and record an internal-only audit fact."""
    normalized_room_id = _strict_positive_id(room_id, "room_id")
    with db.transaction(conn, immediate=True):
        live = _live_room_writer(conn, actor)
        room = _visible_room(conn, live, normalized_room_id)
        if not _is_project_governor(
            conn,
            live,
            project_id=room["project_id"],
            scope_key=room["project_scope_key"],
        ):
            raise RoomCommandError(
                "forbidden", "human project creator or admin required"
            )
        if room["room_type"] in {"project", "brief"}:
            raise RoomCommandError(
                "forbidden", "project and brief rooms cannot be archived"
            )
        if room["archived"]:
            return room
        try:
            archived = rooms.archive_room(conn, normalized_room_id, commit=False)
        except sqlite3.IntegrityError as exc:
            raise RoomCommandError("conflict", "room cannot be archived") from exc
        assert archived is not None
        activity.record(
            conn,
            actor_id=live["id"],
            verb="archived_room",
            target_kind="room",
            target_id=normalized_room_id,
            detail="",
            delivery_eligible=False,
            commit=False,
        )
        return archived


def post_event(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    room_id: object,
    event_kind: object,
    body: object,
    reference_kind: object | None = None,
    reference_id: object | None = None,
    supersedes_event_id: object | None = None,
) -> dict:
    """Append one inert room event and its derived search row atomically."""
    normalized_room_id = _strict_positive_id(room_id, "room_id")
    if not isinstance(event_kind, str):
        raise RoomCommandError("invalid", "event_kind must be a string")
    normalized_kind = event_kind.strip().lower()
    if normalized_kind not in rooms.EVENT_KINDS:
        raise RoomCommandError("invalid", "unsupported room event kind")
    normalized_body = _safe_plain_text(
        body,
        field="body",
        max_chars=rooms.MAX_EVENT_BODY_CHARS,
        reject_structured=True,
    )
    normalized_reference_kind, normalized_reference_id = _normalize_reference(
        reference_kind, reference_id
    )
    normalized_supersedes = (
        None
        if supersedes_event_id is None
        else _strict_positive_id(supersedes_event_id, "supersedes_event_id")
    )
    digest = hashlib.sha256(normalized_body.encode("utf-8")).hexdigest()

    with db.transaction(conn, immediate=True):
        live = _live_room_writer(conn, actor)
        room = _visible_room(conn, live, normalized_room_id)
        if room["archived"]:
            raise RoomCommandError("conflict", "room is archived")
        if room["room_type"] == "brief":
            raise RoomCommandError("forbidden", "brief rooms are read-only")
        if (
            room["room_type"] in {"work_item", "agent"}
            and room.get("link_state") != "active"
        ):
            raise RoomCommandError(
                "conflict", "linked record is unavailable; room is read-only"
            )
        if normalized_kind in _AGENT_EVENT_KINDS and not live.get("is_agent"):
            raise RoomCommandError(
                "forbidden", f"{normalized_kind} requires an agent actor"
            )
        if normalized_kind == "system_notice" and not _is_project_governor(
            conn,
            live,
            project_id=room["project_id"],
            scope_key=room["project_scope_key"],
        ):
            raise RoomCommandError(
                "forbidden", "human project creator or admin required"
            )
        _authorize_reference(
            conn,
            live,
            room,
            normalized_reference_kind,
            normalized_reference_id,
        )
        if normalized_supersedes is not None:
            prior = conn.execute(
                "SELECT re.room_id, successor.activity_id AS successor_id "
                "FROM room_events re LEFT JOIN room_events successor "
                "ON successor.supersedes_event_id = re.activity_id "
                "WHERE re.activity_id = ?",
                (normalized_supersedes,),
            ).fetchone()
            prior_visible = activity.get_visible_activity(
                conn, normalized_supersedes, live
            )
            if (
                prior is None
                or prior_visible is None
                or prior["room_id"] != normalized_room_id
                or prior["successor_id"] is not None
            ):
                raise RoomCommandError(
                    "conflict", "superseded event is unavailable or already replaced"
                )
        recorded = activity.record(
            conn,
            actor_id=live["id"],
            verb=f"room_{normalized_kind}",
            target_kind="room",
            target_id=normalized_room_id,
            detail=normalized_body,
            delivery_eligible=False,
            commit=False,
        )
        try:
            event = rooms.create_room_event(
                conn,
                activity_id=recorded["id"],
                room_id=normalized_room_id,
                event_kind=normalized_kind,
                content_sha256=digest,
                reference_kind=normalized_reference_kind,
                reference_id=normalized_reference_id,
                supersedes_event_id=normalized_supersedes,
                commit=False,
            )
        except sqlite3.IntegrityError as exc:
            raise RoomCommandError(
                "conflict", "room event could not be appended"
            ) from exc
        search.index_document(
            conn, kind="room_event", source_id=recorded["id"], commit=False
        )
        return event


create_event = post_event
post_room_event = post_event
