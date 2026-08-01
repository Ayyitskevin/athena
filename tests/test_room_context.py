"""Imported-history boundaries for Athena Room read projections.

Foreign activity remains visible as labelled audit history, but it must not become
evidence of native coordination, participation, decisions, or source revision.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

import pytest

from athena.aegis import (
    issue_commands,
    issues,
    leases,
    projects,
    room_briefs,
    room_commands,
    room_context,
    room_timeline,
    rooms,
)
from athena.aegis.rooms_api import ClaimOut
from athena.core import access, activity, approvals, db, users


@pytest.fixture
def imported_history_world(tmp_path: Path):
    conn = db.connect(tmp_path / "room-imported-history.db")
    db.migrate(conn)

    owner = users.create_user(
        conn,
        email="owner@room-history.example",
        name="Room Owner",
        role="admin",
    )
    agent = users.create_user(
        conn,
        email="agent@room-history.example",
        name="Foreign History Agent",
        is_agent=True,
    )
    project = projects.create_project(
        conn,
        name="Imported History Project",
        key="IHP",
        description="Imported-history projection regression fixture",
        created_by=owner["id"],
    )
    project_rooms = rooms.ensure_project_rooms(conn, project_id=project["id"])
    issue = issues.create_issue(
        conn,
        title="Revision needle issue",
        body="revisionneedle foreign-only context evidence",
        created_by=owner["id"],
        project_id=project["id"],
    )
    agent_room = rooms.ensure_agent_room(
        conn,
        project_id=project["id"],
        agent_id=agent["id"],
        created_by=owner["id"],
    )
    conn.commit()

    native = activity.record(
        conn,
        actor_id=owner["id"],
        verb="created_issue",
        target_kind="issue",
        target_id=issue["id"],
        detail="Native source revision baseline",
    )
    imported = activity.record(
        conn,
        actor_id=agent["id"],
        verb="approval_imported",
        target_kind="issue",
        target_id=issue["id"],
        detail="foreign-only revisionneedle coordination claim",
        imported_at="2026-07-30 12:00:00",
    )

    world: dict[str, Any] = {
        "conn": conn,
        "owner": owner,
        "agent": agent,
        "project": project,
        "issue": issue,
        "main_room": project_rooms["project"],
        "brief_room": project_rooms["brief"],
        "agent_room": agent_room,
        "native": native,
        "imported": imported,
    }
    try:
        yield world
    finally:
        conn.close()


def test_imported_history_is_labelled_but_not_live_room_coordination(
    imported_history_world: dict[str, Any],
):
    world = imported_history_world
    conn: sqlite3.Connection = world["conn"]
    owner = world["owner"]
    imported_id = int(world["imported"]["id"])

    timeline = room_timeline.list_timeline(
        conn,
        int(world["main_room"]["id"]),
        actor=owner,
    )
    assert timeline is not None
    imported_item = next(
        item for item in timeline["items"] if item["activity_id"] == imported_id
    )
    assert imported_item["classification"] == "imported"
    assert imported_item["imported_at"] == "2026-07-30 12:00:00"

    main_agents = room_timeline.list_visible_agents(
        conn,
        int(world["main_room"]["id"]),
        actor=owner,
    )
    assert main_agents is not None
    assert world["agent"]["id"] not in {item["id"] for item in main_agents["items"]}

    brief = room_briefs.build_live_brief(
        conn,
        int(world["brief_room"]["id"]),
        actor=owner,
    )
    assert brief is not None
    assert brief["decisions"]["items"] == []
    assert brief["decisions"]["unavailable_reason"] is None
    assert brief["agents"]["items"] == []
    assert brief["agents"]["unavailable_reason"] is None
    assert imported_id not in {
        item["activity_id"] for item in brief["decisions"]["items"]
    }
    assert imported_id not in {
        item["activity_id"] for item in brief["recent_timeline"]["items"]
    }


def test_imported_history_cannot_expand_agent_context_or_replace_revision(
    imported_history_world: dict[str, Any],
):
    world = imported_history_world
    conn: sqlite3.Connection = world["conn"]
    owner = world["owner"]
    agent_id = int(world["agent"]["id"])
    imported_id = int(world["imported"]["id"])

    agents = room_timeline.list_visible_agents(
        conn,
        int(world["agent_room"]["id"]),
        actor=owner,
    )
    assert agents is not None
    linked_agent = next(item for item in agents["items"] if item["id"] == agent_id)
    assert linked_agent["recent_contributions"] == {
        "items": [],
        "visible_total": 0,
        "clipped": False,
    }
    assert linked_agent["latest_check_in"] is None
    assert linked_agent["visible_lineage"]["items"] == []

    agent_context = room_context.build_room_context(
        conn,
        int(world["agent_room"]["id"]),
        actor=owner,
        question="foreign-only",
    )
    assert agent_context is not None
    assert agent_context["bounds"]["scoped_issue_count"] == 0
    assert agent_context["records"] == []

    project_context = room_context.build_room_context(
        conn,
        int(world["main_room"]["id"]),
        actor=owner,
        question="revisionneedle",
    )
    assert project_context is not None
    issue_record = next(
        record
        for record in project_context["records"]
        if record["record_type"] == "issue"
    )
    assert issue_record["source_activity_id"] == world["native"]["id"]
    assert imported_id not in {
        record["source_activity_id"] for record in project_context["records"]
    }


def test_active_claim_projection_carries_lease_generation(
    imported_history_world: dict[str, Any],
):
    world = imported_history_world
    conn: sqlite3.Connection = world["conn"]
    owner = world["owner"]
    lease = leases.upsert_lease(
        conn,
        int(world["issue"]["id"]),
        int(world["agent"]["id"]),
        lease_seconds=3_600,
    )

    agents = room_timeline.list_visible_agents(
        conn,
        int(world["agent_room"]["id"]),
        actor=owner,
    )
    assert agents is not None
    linked_agent = next(
        item for item in agents["items"] if item["id"] == world["agent"]["id"]
    )
    claim = next(
        item
        for item in linked_agent["current_claims"]["items"]
        if item["issue_id"] == world["issue"]["id"]
    )
    assert claim["generation"] == lease["generation"]
    assert claim["receipt"] == f"/issues/{world['issue']['id']}"
    validated = ClaimOut.model_validate(claim)
    assert validated.generation == lease["generation"]
    assert validated.receipt == claim["receipt"]


def test_approval_reference_details_are_admin_only(
    imported_history_world: dict[str, Any],
):
    world = imported_history_world
    conn: sqlite3.Connection = world["conn"]
    owner = world["owner"]
    member = users.create_user(
        conn,
        email="member@room-history.example",
        name="Project Member",
        role="member",
    )
    access.add_project_member(
        conn,
        int(world["project"]["id"]),
        int(member["id"]),
        int(owner["id"]),
    )
    request = approvals.open_request(
        conn,
        actor_id=int(world["agent"]["id"]),
        action_kind=approvals.ACTION_ISSUE_CLOSE,
        target_kind="issue",
        target_id=int(world["issue"]["id"]),
        run_id=None,
    )
    event = room_commands.post_event(
        conn,
        actor=owner,
        room_id=int(world["main_room"]["id"]),
        event_kind="decision",
        body="Approval detail is operator-only",
        reference_kind="approval",
        reference_id=request.id,
    )

    member_page = room_timeline.list_timeline(
        conn,
        int(world["main_room"]["id"]),
        actor=member,
    )
    admin_page = room_timeline.list_timeline(
        conn,
        int(world["main_room"]["id"]),
        actor=owner,
    )
    assert member_page is not None and admin_page is not None
    member_item = next(
        item for item in member_page["items"] if item["activity_id"] == event["id"]
    )
    admin_item = next(
        item for item in admin_page["items"] if item["activity_id"] == event["id"]
    )
    assert member_item["reference"]["available"] is False
    assert admin_item["reference"]["available"] is True
    assert admin_item["reference"]["receipt"] == f"/approvals/{request.id}"


def test_moved_room_reference_does_not_bypass_immutable_event_visibility(
    imported_history_world: dict[str, Any],
):
    world = imported_history_world
    conn: sqlite3.Connection = world["conn"]
    owner = world["owner"]
    projects.set_visibility(conn, int(world["project"]["id"]), "private")
    destination = users.create_user(
        conn,
        email="destination@room-history.example",
        name="Destination Owner",
        role="member",
    )
    destination_project = projects.create_project(
        conn,
        name="Destination Project",
        key="DST",
        created_by=int(destination["id"]),
    )
    projects.set_visibility(conn, int(destination_project["id"]), "private")
    work_room = rooms.ensure_work_item_room(
        conn,
        issue_id=int(world["issue"]["id"]),
    )
    assert work_room is not None
    conn.commit()
    old_event = room_commands.post_event(
        conn,
        actor=owner,
        room_id=int(work_room["id"]),
        event_kind="message",
        body="Source-project-only room history",
    )

    moved = issue_commands.update_issue(
        conn,
        actor=owner,
        issue_id=int(world["issue"]["id"]),
        project_id=int(destination_project["id"]),
    )
    assert moved["project_id"] == destination_project["id"]
    moved_room = rooms.get_room(conn, int(work_room["id"]))
    assert moved_room is not None
    assert rooms.can_see_room(conn, destination, moved_room)
    assert activity.get_visible_activity(conn, old_event["id"], destination) is None

    resolved = room_timeline._resolve_reference(
        conn,
        moved_room,
        destination,
        "activity",
        str(old_event["id"]),
    )
    assert resolved is None
