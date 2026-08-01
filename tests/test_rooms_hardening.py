"""Adversarial regressions for Rooms projection hardening."""

from __future__ import annotations
import base64

from dataclasses import dataclass
from pathlib import Path
import sqlite3

import pytest

from athena.aegis import (
    issues,
    projects,
    room_briefs,
    room_commands,
    room_context,
    room_timeline,
    rooms,
)
from athena.core import (
    access,
    activity,
    agent_run_checkins,
    db,
    run_context,
    search,
    tokens,
    users,
)


@dataclass
class HardeningWorld:
    conn: sqlite3.Connection
    owner: dict
    outsider: dict
    agent: dict
    agent_token: dict
    project: dict
    hidden_project: dict
    issue: dict
    hidden_issue: dict
    main_room: dict
    brief_room: dict


@pytest.fixture
def hardening_world(tmp_path: Path):
    conn = db.connect(tmp_path / "rooms-hardening.db")
    db.migrate(conn)
    owner = users.create_user(
        conn, email="owner@hardening.example", name="Owner", role="member"
    )
    outsider = users.create_user(
        conn, email="outsider@hardening.example", name="Outsider", role="member"
    )
    agent = users.create_user(
        conn,
        email="agent@hardening.example",
        name="Hardening Agent",
        role="member",
        is_agent=True,
    )
    project = projects.create_project(
        conn,
        name="Hardening Project",
        key="HRD",
        description="Projection hardening",
        created_by=owner["id"],
    )
    hidden_project = projects.create_project(
        conn,
        name="Hidden Project",
        key="HID",
        created_by=outsider["id"],
    )
    projects.set_visibility(conn, project["id"], "private")
    projects.set_visibility(conn, hidden_project["id"], "private")
    access.add_project_member(conn, project["id"], agent["id"], owner["id"])
    project_rooms = rooms.ensure_project_rooms(conn, project_id=project["id"])
    rooms.ensure_project_rooms(conn, project_id=hidden_project["id"])
    issue = issues.create_issue(
        conn,
        title="Visible work",
        body="",
        created_by=owner["id"],
        project_id=project["id"],
    )
    hidden_issue = issues.create_issue(
        conn,
        title="Hidden work",
        body="",
        created_by=outsider["id"],
        project_id=hidden_project["id"],
    )
    rooms.ensure_agent_room(
        conn,
        project_id=project["id"],
        agent_id=agent["id"],
        created_by=owner["id"],
    )
    agent_token = tokens.create_token(
        conn,
        user_id=agent["id"],
        name="rooms-hardening",
        scopes=[tokens.ROOMS_WRITE_SCOPE],
    )
    conn.commit()
    world = HardeningWorld(
        conn=conn,
        owner=owner,
        outsider=outsider,
        agent=agent,
        agent_token=agent_token,
        project=project,
        hidden_project=hidden_project,
        issue=issue,
        hidden_issue=hidden_issue,
        main_room=project_rooms["project"],
        brief_room=project_rooms["brief"],
    )
    try:
        yield world
    finally:
        conn.close()


def test_authoritative_sources_redact_unsafe_text_without_hiding_safe_detail(
    hardening_world: HardeningWorld,
):
    """WHY: visible source records may contain material Rooms must not re-emit."""
    world = hardening_world
    safe = activity.record(
        world.conn,
        actor_id=world.owner["id"],
        verb="worked",
        target_kind="issue",
        target_id=world.issue["id"],
        detail="safemarker ordinary domain progress",
    )
    unsafe_details = (
        "leakmarker inspect /etc/passwd",
        "leakmarker token=abcdefghijklmnop",
        "leakmarker stdout: secret-prone log output",
        'leakmarker {"choices": ["raw provider response"]}',
    )
    unsafe_ids = {
        activity.record(
            world.conn,
            actor_id=world.owner["id"],
            verb="worked",
            target_kind="issue",
            target_id=world.issue["id"],
            detail=detail,
        )["id"]
        for detail in unsafe_details
    }
    unsafe_issue = issues.create_issue(
        world.conn,
        title="leakmarker source",
        body="leakmarker lives at /srv/private/config.env",
        created_by=world.owner["id"],
        project_id=world.project["id"],
    )
    search.index_document(world.conn, kind="issue", source_id=unsafe_issue["id"])

    timeline = room_timeline.list_timeline(
        world.conn, world.main_room["id"], actor=world.owner
    )
    assert timeline is not None
    by_id = {item["activity_id"]: item for item in timeline["items"]}
    assert by_id[safe["id"]]["body"] == "safemarker ordinary domain progress"
    assert {by_id[event_id]["body"] for event_id in unsafe_ids} == {
        room_timeline.REDACTED_DETAIL
    }

    context = room_context.build_room_context(
        world.conn,
        world.main_room["id"],
        actor=world.owner,
        question="leakmarker",
        limit=25,
    )
    assert context is not None
    assert unsafe_issue["id"] in {
        record["record_id"]
        for record in context["records"]
        if record["record_type"] == "issue"
    }
    for record in context["records"]:
        rendered = f"{record['title']} {record['snippet']}"
        assert "/etc/passwd" not in rendered
        assert "abcdefghijklmnop" not in rendered
        assert "/srv/private/config.env" not in rendered
        if (
            record["record_id"] in unsafe_ids
            or record["record_id"] == unsafe_issue["id"]
        ):
            assert record["digest_sha256"] is None

    brief = room_briefs.build_live_brief(
        world.conn, world.brief_room["id"], actor=world.owner
    )
    assert brief is not None
    assert not any(
        forbidden in str(brief)
        for forbidden in (
            "/etc/passwd",
            "abcdefghijklmnop",
            "/srv/private/config.env",
            "secret-prone log output",
            "raw provider response",
        )
    )


def test_superseded_decisions_are_history_but_not_current_room_state(
    hardening_world: HardeningWorld,
):
    """WHY: an append-only predecessor must not look like a current decision."""
    world = hardening_world
    original = room_commands.post_event(
        world.conn,
        actor=world.owner,
        room_id=world.main_room["id"],
        event_kind="decision",
        body="policydecision original",
    )
    replacement = room_commands.post_event(
        world.conn,
        actor=world.owner,
        room_id=world.main_room["id"],
        event_kind="decision",
        body="policydecision replacement",
        supersedes_event_id=original["id"],
    )

    timeline = room_timeline.list_timeline(
        world.conn, world.main_room["id"], actor=world.owner
    )
    assert timeline is not None
    by_id = {item["activity_id"]: item for item in timeline["items"]}
    assert by_id[original["id"]]["is_current"] is False
    assert by_id[original["id"]]["successor_event_id"] == replacement["id"]
    assert by_id[replacement["id"]]["is_current"] is True
    assert by_id[replacement["id"]]["supersedes_event_id"] == original["id"]

    context = room_context.build_room_context(
        world.conn,
        world.main_room["id"],
        actor=world.owner,
        question="policydecision",
    )
    assert context is not None
    context_ids = {
        record["record_id"]
        for record in context["records"]
        if record["record_type"] == "activity"
    }
    assert replacement["id"] in context_ids
    assert original["id"] not in context_ids

    brief = room_briefs.build_live_brief(
        world.conn, world.brief_room["id"], actor=world.owner
    )
    assert brief is not None
    decision_ids = {item["activity_id"] for item in brief["decisions"]["items"]}
    recent_ids = {item["activity_id"] for item in brief["recent_timeline"]["items"]}
    assert replacement["id"] in decision_ids
    assert original["id"] not in decision_ids
    assert replacement["id"] in recent_ids
    assert original["id"] not in recent_ids


@pytest.mark.parametrize("contamination", ["hidden_event", "imported_event"])
def test_run_receipts_require_native_complete_visibility(
    hardening_world: HardeningWorld,
    contamination: str,
):
    """WHY: a navigable receipt must not imply a partial or foreign run is whole."""
    world = hardening_world
    run_id = f"unsafe-{contamination}"
    token = run_context.set_run_id(run_id)
    try:
        visible = activity.record(
            world.conn,
            actor_id=world.agent["id"],
            verb="worked",
            target_kind="issue",
            target_id=world.issue["id"],
            detail=f"visible {contamination} contribution",
        )
        if contamination == "hidden_event":
            activity.record(
                world.conn,
                actor_id=world.agent["id"],
                verb="worked",
                target_kind="issue",
                target_id=world.hidden_issue["id"],
                detail="hidden contribution",
            )
    finally:
        run_context.reset_run_id(token)

    if contamination == "imported_event":
        imported_id = world.conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, run_id, "
            "visibility_restricted, imported_at) "
            "VALUES (?, 'worked', 'issue', ?, 'foreign contribution', ?, 0, ?)",
            (
                world.agent["id"],
                world.issue["id"],
                run_id,
                "2026-07-31T12:00:00Z",
            ),
        ).lastrowid
        assert imported_id is not None
        world.conn.execute(
            "INSERT INTO activity_visibility_projects "
            "(event_id, project_scope_key) VALUES (?, ?)",
            (imported_id, world.project["activity_scope_key"]),
        )

    agent_run_checkins.upsert_checkin(
        world.conn,
        agent_id=world.agent["id"],
        run_id=run_id,
        token_id=world.agent_token["id"],
    )
    world.conn.commit()

    timeline = room_timeline.list_timeline(
        world.conn, world.main_room["id"], actor=world.owner
    )
    assert timeline is not None
    item = next(
        entry for entry in timeline["items"] if entry["activity_id"] == visible["id"]
    )
    assert item["run_receipt"] is None
    assert item["run_receipt_unavailable_reason"] == room_timeline.INCOMPLETE_RUN

    agents = room_timeline.list_visible_agents(
        world.conn, world.main_room["id"], actor=world.owner
    )
    assert agents is not None
    teammate = next(
        entry for entry in agents["items"] if entry["id"] == world.agent["id"]
    )
    assert teammate["latest_check_in"] is not None
    assert teammate["latest_check_in"]["receipt"] is None
    assert (
        teammate["latest_check_in_unavailable_reason"] == room_timeline.INCOMPLETE_RUN
    )
    assert teammate["visible_lineage"]["items"] == []
    assert (
        teammate["visible_lineage"]["unavailable_reason"]
        == room_timeline.INCOMPLETE_RUN
    )


def _cursor(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def test_room_payload_safety_helpers_cover_every_rejection_class():
    """WHY: every prohibited payload class needs an independently failing branch."""
    classified = (
        ("bad\x00control", False, "control_characters"),
        ("inspect /etc/passwd", False, "filesystem_path"),
        ("token=abcdefghijklmnop", False, "credential_like"),
        ("prefix stdout: secret-prone output", False, "log_or_provider_payload"),
        (' {"operation": "dispatch"}', True, "structured_payload"),
        ("{not-json", True, None),
        ("ordinary coordination prose", True, None),
    )
    for value, reject_structured, expected in classified:
        assert (
            room_commands.unsafe_room_payload_reason(
                value, reject_structured=reject_structured
            )
            == expected
        )

    invalid_text = (
        (17, {}, "must be a string"),
        ("", {}, "cannot be empty"),
        ("long", {"max_chars": 3}, "at most 3"),
        ("two\nlines", {"multiline": False}, "must be one line"),
        ("bad\x00control", {}, "control characters"),
        ("inspect /etc/passwd", {}, "filesystem paths"),
        ("token=abcdefghijklmnop", {}, "credential-like"),
        ("prefix stderr: raw log", {}, "log or provider"),
        (
            '{"operation": "dispatch"}',
            {"reject_structured": True},
            "plain coordination",
        ),
    )
    for value, overrides, detail in invalid_text:
        kwargs = {"field": "body", "max_chars": 100, **overrides}
        with pytest.raises(room_commands.RoomCommandError, match=detail):
            room_commands._safe_plain_text(value, **kwargs)
    assert (
        room_commands._safe_plain_text(
            " {not-json ",
            field="body",
            max_chars=100,
            reject_structured=True,
        )
        == "{not-json"
    )
    assert (
        room_commands._safe_plain_text(
            " \r\n safe prose \r\n ", field="body", max_chars=100
        )
        == "safe prose"
    )
    assert (
        room_commands._safe_plain_text(
            " ", field="purpose", max_chars=100, allow_empty=True
        )
        == ""
    )


def test_room_identifier_slug_and_reference_helpers_fail_closed():
    """WHY: transport validation is not a substitute for safe direct callers."""
    for value in (True, "1", 0, -1, rooms.MAX_SQLITE_ID + 1):
        with pytest.raises(room_commands.RoomCommandError, match="positive integer"):
            room_commands._strict_positive_id(value, "room_id")
    assert (
        room_commands._strict_positive_id(rooms.MAX_SQLITE_ID, "room_id")
        == rooms.MAX_SQLITE_ID
    )

    assert (
        room_commands._normalize_slug(None, room_type="project", link_id=None) == "main"
    )
    assert (
        room_commands._normalize_slug(None, room_type="brief", link_id=None) == "brief"
    )
    assert (
        room_commands._normalize_slug(None, room_type="work_item", link_id=7)
        == "work-item-7"
    )
    assert (
        room_commands._normalize_slug(None, room_type="agent", link_id=9) == "agent-9"
    )
    assert (
        room_commands._normalize_slug(" Custom-Room ", room_type="agent", link_id=9)
        == "custom-room"
    )
    for slug in (17, "bad slug", "x" * (rooms.MAX_SLUG_CHARS + 1)):
        with pytest.raises(room_commands.RoomCommandError):
            room_commands._normalize_slug(slug, room_type="agent", link_id=9)

    assert room_commands._normalize_reference(None, None) == (None, None)
    invalid_references = (
        (None, 1),
        (7, 1),
        ("unknown", 1),
        ("issue", True),
        ("issue", 0),
        ("issue", rooms.MAX_SQLITE_ID + 1),
        ("issue", object()),
        ("issue", "01"),
        ("issue", "one"),
        ("issue", "１２"),
        ("issue", str(rooms.MAX_SQLITE_ID + 1)),
        ("run", 7),
        ("run", ""),
        ("run", "x" * (rooms.MAX_REFERENCE_ID_CHARS + 1)),
        ("run", "bad\x00run"),
        ("run", "/tmp/run"),
        ("run", "token=abcdefghijklmnop"),
        ("run", "stdout: raw log"),
    )
    for kind, identifier in invalid_references:
        with pytest.raises(room_commands.RoomCommandError):
            room_commands._normalize_reference(kind, identifier)
    assert room_commands._normalize_reference("issue", 7) == ("issue", "7")
    assert room_commands._normalize_reference("issue", "7") == ("issue", "7")
    assert room_commands._normalize_reference("run", " run-seven ") == (
        "run",
        "run-seven",
    )


def test_timeline_cursor_and_projection_helpers_reject_noncanonical_inputs():
    """WHY: opaque cursors and internal limits must be bounded before SQLite."""
    for room_id, activity_id in (
        (0, 1),
        (1, 0),
        (True, 1),
        (1, rooms.MAX_SQLITE_ID + 1),
    ):
        with pytest.raises(ValueError):
            room_timeline.encode_cursor(room_id, activity_id)
    with pytest.raises(ValueError):
        room_timeline.decode_cursor(None, 0)
    assert room_timeline.decode_cursor(None, 1) is None

    invalid_cursors = (
        "",
        "x" * 129,
        "not-base64!",
        _cursor("wrong-contract:1:2"),
        _cursor("athena.room-timeline.v1:1"),
        _cursor("athena.room-timeline.v1:01:2"),
        _cursor("athena.room-timeline.v1:1:0"),
        _cursor(f"athena.room-timeline.v1:1:{rooms.MAX_SQLITE_ID + 1}"),
        _cursor("athena.room-timeline.v1:2:3"),
    )
    for cursor in invalid_cursors:
        with pytest.raises(room_timeline.InvalidCursor):
            room_timeline.decode_cursor(cursor, 1)
    canonical = room_timeline.encode_cursor(1, 3)
    assert room_timeline.decode_cursor(canonical, 1) == 3

    for limit in (True, "1", 0, 3):
        with pytest.raises(ValueError):
            room_timeline._bounded_limit(limit, ceiling=2)
    assert room_timeline._bounded_limit(2, ceiling=2) == 2

    for value in ("", "0", "01", "１２", str(rooms.MAX_SQLITE_ID + 1)):
        assert room_timeline._integer_reference(value) is None
    assert (
        room_timeline._integer_reference(str(rooms.MAX_SQLITE_ID))
        == rooms.MAX_SQLITE_ID
    )
    assert room_timeline.project_authoritative_text("abcd", max_chars=3) == (
        "abc",
        True,
    )
    assert room_timeline._domain_scope_sql(
        {"room_type": "corrupt", "project_id": 1}
    ) == ("0 = 1", [])

    contributions = {
        "items": [
            {
                "run_id": None,
                "activity_id": 1,
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "run_id": "safe-run",
                "activity_id": 2,
                "created_at": "2026-01-02T00:00:00Z",
            },
            {
                "run_id": "safe-run",
                "activity_id": 1,
                "created_at": "2026-01-01T00:00:00Z",
            },
        ],
        "clipped": False,
    }
    lineage = room_timeline._visible_lineage(contributions, {"safe-run"})
    assert [item["run_id"] for item in lineage["items"]] == ["safe-run"]
    assert lineage["unavailable_reason"] is None
    contributions["clipped"] = True
    assert (
        room_timeline._visible_lineage(contributions, {"safe-run"})[
            "unavailable_reason"
        ]
        == "bounded_to_recent_visible_contributions"
    )


def test_room_storage_read_guards_cover_direct_callers(
    hardening_world: HardeningWorld,
):
    """WHY: oversized direct-call IDs must never reach sqlite3 parameter binding."""
    world = hardening_world
    conn = world.conn
    for value in (True, "1", 0, rooms.MAX_SQLITE_ID + 1):
        assert rooms.is_sqlite_id(value) is False
    assert rooms.is_sqlite_id(rooms.MAX_SQLITE_ID) is True
    assert rooms.get_room(conn, rooms.MAX_SQLITE_ID + 1) is None
    assert rooms.get_room_by_slug(conn, rooms.MAX_SQLITE_ID + 1, "main") is None
    assert rooms.get_work_item_room(conn, rooms.MAX_SQLITE_ID + 1) is None
    assert rooms.get_room_event(conn, rooms.MAX_SQLITE_ID + 1) is None
    assert rooms.list_rooms(conn, rooms.MAX_SQLITE_ID + 1) == []
    assert rooms.list_rooms_page(conn, rooms.MAX_SQLITE_ID + 1, actor=world.owner) == []

    with pytest.raises(ValueError, match="room_id"):
        rooms.list_room_events(conn, 0)
    with pytest.raises(ValueError, match="before_id"):
        rooms.list_room_events(conn, world.main_room["id"], before_id=0)
    for limit in (True, "1", 0, rooms.MAX_EVENT_PAGE + 1):
        with pytest.raises(ValueError, match="limit"):
            rooms.list_room_events(conn, world.main_room["id"], limit=limit)
    for limit in (True, "1", 0, 201):
        with pytest.raises(ValueError, match="limit"):
            rooms.list_rooms_page(
                conn, world.project["id"], actor=world.owner, limit=limit
            )
    for after_id in (True, "1", -1, rooms.MAX_SQLITE_ID + 1):
        with pytest.raises(ValueError, match="after_id"):
            rooms.list_rooms_page(
                conn,
                world.project["id"],
                actor=world.owner,
                after_id=after_id,
            )
    assert rooms.list_rooms_page(conn, 999_999, actor=world.owner) == []

    for room_type, expected in (
        ("project", "project"),
        ("brief", "project"),
        ("work_item", "issue"),
        ("agent", "agent"),
    ):
        scope = rooms.timeline_scope(
            {
                "id": 1,
                "project_id": 2,
                "project_scope_key": "scope",
                "room_type": room_type,
                "issue_id": 3,
                "agent_id": 4,
                "is_detached": room_type == "work_item",
            }
        )
        assert scope["scope_kind"] == expected
        assert scope["detached"] is (room_type == "work_item")

    assert (
        room_timeline.list_visible_agents(conn, rooms.MAX_SQLITE_ID, actor=world.owner)
        is None
    )
    assert (
        room_timeline._safe_native_complete_run_ids(conn, world.owner, {"missing-run"})
        == set()
    )
    for clipped, expected in (
        (False, "no_room_visible_report"),
        (True, "bounded_visible_contributions"),
    ):
        assert room_timeline._latest_check_in(
            conn,
            world.agent["id"],
            {"items": [], "clipped": clipped},
            set(),
        ) == (None, expected)


def test_room_context_rejects_invalid_questions_and_selection_limits(
    hardening_world: HardeningWorld,
):
    """WHY: direct callers must share the API's bounded input contract."""
    world = hardening_world
    invalid_questions = (
        None,
        "unsafe\x00control",
        " \t\n ",
        "x" * (room_context.MAX_QUESTION_CHARS + 1),
    )
    for question in invalid_questions:
        with pytest.raises(room_context.InvalidQuestion):
            room_context.build_room_context(
                world.conn,
                world.main_room["id"],
                actor=world.owner,
                question=question,
            )

    for limit in (True, "1", 0, room_context.MAX_SELECTION_LIMIT + 1):
        with pytest.raises(ValueError, match="limit"):
            room_context.build_room_context(
                world.conn,
                world.main_room["id"],
                actor=world.owner,
                question="bounded input",
                limit=limit,
            )


def test_generated_room_slugs_cannot_be_reserved_by_unrelated_links(
    hardening_world: HardeningWorld,
):
    """WHY: a custom room must not permanently poison a future ensure."""
    world = hardening_world
    custom_agent = users.create_user(
        world.conn,
        email="custom-agent@hardening.example",
        name="Custom Agent",
        is_agent=True,
    )
    future_agent = users.create_user(
        world.conn,
        email="future-agent@hardening.example",
        name="Future Agent",
        is_agent=True,
    )
    future_issue = issues.create_issue(
        world.conn,
        title="Future linked work",
        body="",
        created_by=world.owner["id"],
        project_id=world.project["id"],
    )

    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(world.conn, immediate=True):
            rooms.create_room(
                world.conn,
                project_id=world.project["id"],
                slug=f"agent-{future_agent['id']}",
                room_type="agent",
                title="Storage collision attempt",
                purpose="",
                visibility="members",
                agent_id=custom_agent["id"],
                created_by=world.owner["id"],
                commit=False,
            )

    for reserved_slug in (
        f"agent-{future_agent['id']}",
        f"work-item-{future_issue['id']}",
    ):
        with pytest.raises(room_commands.RoomCommandError, match="reserved"):
            room_commands.create_room(
                world.conn,
                actor=world.owner,
                project_id=world.project["id"],
                room_type="agent",
                title="Collision attempt",
                slug=reserved_slug,
                agent_id=custom_agent["id"],
            )

    agent_room = rooms.ensure_agent_room(
        world.conn,
        project_id=world.project["id"],
        agent_id=future_agent["id"],
        created_by=world.owner["id"],
    )
    work_room = rooms.ensure_work_item_room(
        world.conn,
        issue_id=future_issue["id"],
        created_by=world.owner["id"],
    )
    world.conn.commit()
    assert agent_room["slug"] == f"agent-{future_agent['id']}"
    assert work_room is not None
    assert work_room["slug"] == f"work-item-{future_issue['id']}"
