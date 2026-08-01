"""Domain and security invariants for Athena Rooms.

These tests stay below HTTP/MCP. They pin the SQLite and command-layer reasons the
Rooms feature is safe: stable identities, immutable historical visibility envelopes,
live actor re-authorization, append-only events, inert delivery, and bounded reads.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
import threading

import pytest

from athena.aegis import (
    automation,
    contributors,
    issues,
    issue_commands,
    projects,
    room_commands,
    rooms,
)
from athena.core import access, activity, db, search, tokens, users, webhooks


@dataclass
class RoomWorld:
    conn: sqlite3.Connection
    db_file: Path
    users: dict[str, dict]
    actors: dict[str, dict]
    tokens: dict[str, dict]
    projects: dict[str, dict]
    issues: dict[str, dict]
    room_rows: dict[str, dict]


def _token_actor(
    conn: sqlite3.Connection,
    user: dict,
    *,
    name: str,
    scopes: list[str],
) -> tuple[dict, dict]:
    token = tokens.create_token(
        conn,
        user_id=user["id"],
        name=name,
        scopes=scopes,
    )
    actor = {
        **user,
        "_token_id": token["id"],
        "_token_scopes": token["scopes"],
    }
    return actor, token


@pytest.fixture
def world(tmp_path: Path):
    db_file = tmp_path / "rooms-domain.db"
    conn = db.connect(db_file)
    db.migrate(conn)

    people = {
        "admin": users.create_user(
            conn,
            email="admin@example.com",
            name="Admin",
            role="admin",
        ),
        "owner": users.create_user(
            conn,
            email="owner@example.com",
            name="Owner",
            role="member",
        ),
        "alpha": users.create_user(
            conn,
            email="alpha@example.com",
            name="Alpha Member",
            role="member",
        ),
        "beta": users.create_user(
            conn,
            email="beta@example.com",
            name="Beta Member",
            role="member",
        ),
        "outsider": users.create_user(
            conn,
            email="outsider@example.com",
            name="Outsider",
            role="member",
        ),
        "viewer": users.create_user(
            conn,
            email="viewer@example.com",
            name="Viewer",
            role="viewer",
        ),
        "agent": users.create_user(
            conn,
            email="agent@example.com",
            name="Agent",
            role="member",
            is_agent=True,
        ),
        "spare_agent": users.create_user(
            conn,
            email="spare@example.com",
            name="Spare Agent",
            role="member",
            is_agent=True,
        ),
    }

    project_rows = {
        "alpha": projects.create_project(
            conn,
            name="Alpha Project",
            key="ALP",
            description="Alpha purpose",
            created_by=people["owner"]["id"],
        ),
        "beta": projects.create_project(
            conn,
            name="Beta Project",
            key="BET",
            description="Beta purpose",
            created_by=people["owner"]["id"],
        ),
    }
    for project in project_rows.values():
        projects.set_visibility(conn, project["id"], "private")
        rooms.ensure_project_rooms(conn, project_id=project["id"])

    access.add_project_member(
        conn,
        project_rows["alpha"]["id"],
        people["alpha"]["id"],
        people["owner"]["id"],
    )
    access.add_project_member(
        conn,
        project_rows["beta"]["id"],
        people["beta"]["id"],
        people["owner"]["id"],
    )
    access.add_project_member(
        conn,
        project_rows["alpha"]["id"],
        people["agent"]["id"],
        people["owner"]["id"],
    )

    issue_rows = {
        "alpha": issues.create_issue(
            conn,
            title="Alpha work",
            body="",
            created_by=people["owner"]["id"],
            project_id=project_rows["alpha"]["id"],
        ),
        "beta": issues.create_issue(
            conn,
            title="Beta work",
            body="",
            created_by=people["owner"]["id"],
            project_id=project_rows["beta"]["id"],
        ),
    }
    room_rows = {
        "alpha_main": rooms.get_room_by_slug(conn, project_rows["alpha"]["id"], "main"),
        "alpha_brief": rooms.get_room_by_slug(
            conn, project_rows["alpha"]["id"], "brief"
        ),
        "beta_main": rooms.get_room_by_slug(conn, project_rows["beta"]["id"], "main"),
        "alpha_work": rooms.ensure_work_item_room(
            conn, issue_id=issue_rows["alpha"]["id"]
        ),
        "beta_work": rooms.ensure_work_item_room(
            conn, issue_id=issue_rows["beta"]["id"]
        ),
        "alpha_agent": rooms.ensure_agent_room(
            conn,
            project_id=project_rows["alpha"]["id"],
            agent_id=people["agent"]["id"],
            created_by=people["owner"]["id"],
        ),
    }
    assert all(room_rows.values())
    conn.commit()

    actors: dict[str, dict] = {}
    token_rows: dict[str, dict] = {}
    for label, user_label, scopes in (
        ("owner_rooms", "owner", [tokens.ROOMS_WRITE_SCOPE]),
        ("owner_read", "owner", [tokens.READ_SCOPE]),
        ("alpha_rooms", "alpha", [tokens.ROOMS_WRITE_SCOPE]),
        ("beta_rooms", "beta", [tokens.ROOMS_WRITE_SCOPE]),
        ("outsider_rooms", "outsider", [tokens.ROOMS_WRITE_SCOPE]),
        ("viewer_rooms", "viewer", [tokens.ROOMS_WRITE_SCOPE]),
        ("agent_rooms", "agent", [tokens.ROOMS_WRITE_SCOPE]),
    ):
        actors[label], token_rows[label] = _token_actor(
            conn,
            people[user_label],
            name=label,
            scopes=scopes,
        )

    value = RoomWorld(
        conn=conn,
        db_file=db_file,
        users=people,
        actors=actors,
        tokens=token_rows,
        projects=project_rows,
        issues=issue_rows,
        room_rows=room_rows,
    )
    try:
        yield value
    finally:
        conn.close()


def test_migration_backfills_defaults_participants_and_delivery_bit(
    tmp_path: Path,
    migration_inventory_through,
    monkeypatch,
):
    """WHY: upgrading an existing database must not lose participating agents or
    accidentally make pre-Rooms activity ineligible for delivery."""
    full_inventory = db.MIGRATIONS_DIR
    migration_inventory_through("0069_event_sources.sql")
    conn = db.connect(tmp_path / "rooms-upgrade.db")
    assert db.migrate(conn)[-1] == "0069_event_sources.sql"

    owner = users.create_user(
        conn, email="owner@upgrade.example", name="Owner", role="admin"
    )
    member_agent = users.create_user(
        conn,
        email="member-agent@upgrade.example",
        name="Member Agent",
        is_agent=True,
    )
    assignee_agent = users.create_user(
        conn,
        email="assignee-agent@upgrade.example",
        name="Assignee Agent",
        is_agent=True,
    )
    contributor_agent = users.create_user(
        conn,
        email="contributor-agent@upgrade.example",
        name="Contributor Agent",
        is_agent=True,
    )
    issue_creator_agent = users.create_user(
        conn,
        email="issue-creator-agent@upgrade.example",
        name="Issue Creator Agent",
        is_agent=True,
    )
    project = projects.create_project(
        conn,
        name="Upgrade Project",
        key="UPG",
        description="Existing purpose",
        created_by=owner["id"],
    )
    access.add_project_member(conn, project["id"], member_agent["id"], owner["id"])
    focused = issues.create_issue(
        conn,
        title="Existing work",
        body="",
        created_by=owner["id"],
        project_id=project["id"],
    )
    issues.create_issue(
        conn,
        title="Agent-created work",
        body="",
        created_by=issue_creator_agent["id"],
        project_id=project["id"],
    )
    issues.set_assignee(conn, focused["id"], assignee_agent["id"])
    contributors.add_contributor(
        conn,
        focused["id"],
        contributor_agent["id"],
        owner["id"],
    )
    backlog = issues.create_issue(
        conn,
        title="Backlog only",
        body="",
        created_by=owner["id"],
    )
    conn.execute(
        "INSERT INTO activity "
        "(actor_id, verb, target_kind, target_id, detail) "
        "VALUES (?, 'created', 'project', ?, 'pre-rooms')",
        (owner["id"], project["id"]),
    )
    conn.commit()

    monkeypatch.setattr(db, "MIGRATIONS_DIR", full_inventory)
    assert db.migrate(conn) == ["0070_rooms.sql"]
    assert db.migrate(conn) == []

    project_rooms = [
        dict(row)
        for row in conn.execute(
            "SELECT room_type, slug, title, visibility, issue_id, agent_id "
            "FROM rooms WHERE project_scope_key = ? ORDER BY id",
            (project["activity_scope_key"],),
        ).fetchall()
    ]
    assert [(row["room_type"], row["slug"]) for row in project_rooms[:3]] == [
        ("project", "main"),
        ("brief", "brief"),
        ("work_item", f"work-item-{focused['id']}"),
    ]
    assert project_rooms[0]["title"] == "Upgrade Project"
    assert project_rooms[0]["visibility"] == "project"
    assert project_rooms[1]["title"] == "Upgrade Project live brief"
    assert project_rooms[2]["issue_id"] == focused["id"]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM rooms WHERE room_type = 'work_item' AND issue_id = ?",
            (backlog["id"],),
        ).fetchone()[0]
        == 0
    )
    assert {
        row["agent_id"] for row in project_rooms if row["room_type"] == "agent"
    } == {
        member_agent["id"],
        assignee_agent["id"],
        contributor_agent["id"],
        issue_creator_agent["id"],
    }
    assert (
        conn.execute(
            "SELECT delivery_eligible FROM activity WHERE detail = 'pre-rooms'"
        ).fetchone()["delivery_eligible"]
        == 1
    )
    conn.close()


def test_default_ensures_are_idempotent_and_refresh_only_generated_fields(
    world: RoomWorld,
):
    """WHY: retries and title edits must converge on one stable room identity,
    while a deliberately customized agent-room purpose protects its custom title."""
    conn = world.conn
    project = world.projects["alpha"]
    issue = world.issues["alpha"]
    agent = world.users["agent"]

    before = rooms.list_rooms(conn, project["id"])
    first_defaults = rooms.ensure_project_rooms(conn, project_id=project["id"])
    second_defaults = rooms.ensure_project_rooms(conn, project_id=project["id"])
    first_work = rooms.ensure_work_item_room(conn, issue_id=issue["id"])
    second_work = rooms.ensure_work_item_room(conn, issue_id=issue["id"])
    first_agent = rooms.ensure_agent_room(
        conn,
        project_id=project["id"],
        agent_id=agent["id"],
        created_by=world.users["owner"]["id"],
    )
    second_agent = rooms.ensure_agent_room(
        conn,
        project_id=project["id"],
        agent_id=agent["id"],
        created_by=world.users["owner"]["id"],
    )
    conn.commit()

    assert first_defaults["project"]["id"] == second_defaults["project"]["id"]
    assert first_defaults["brief"]["id"] == second_defaults["brief"]["id"]
    assert first_work["id"] == second_work["id"]
    assert first_agent["id"] == second_agent["id"]
    assert len(rooms.list_rooms(conn, project["id"])) == len(before)

    projects.update_project(
        conn,
        project["id"],
        name="Alpha Renamed",
        description="Renamed purpose",
    )
    refreshed = rooms.ensure_project_rooms(conn, project_id=project["id"])
    issues.update_issue(conn, issue["id"], title="Work Renamed")
    refreshed_work = rooms.ensure_work_item_room(conn, issue_id=issue["id"])
    conn.execute("UPDATE users SET name = 'Agent Renamed' WHERE id = ?", (agent["id"],))
    refreshed_agent = rooms.ensure_agent_room(
        conn,
        project_id=project["id"],
        agent_id=agent["id"],
        created_by=world.users["owner"]["id"],
    )
    assert refreshed["project"]["title"] == "Alpha Renamed"
    assert refreshed["project"]["purpose"] == "Renamed purpose"
    assert refreshed["brief"]["title"] == "Alpha Renamed live brief"
    assert refreshed_work["title"] == "Work Renamed"
    assert refreshed_agent["title"] == "Agent Renamed"

    conn.execute(
        "UPDATE rooms SET purpose = 'Custom agent purpose' WHERE id = ?",
        (refreshed_agent["id"],),
    )
    conn.execute("UPDATE users SET name = 'Agent Again' WHERE id = ?", (agent["id"],))
    protected = rooms.ensure_agent_room(
        conn,
        project_id=project["id"],
        agent_id=agent["id"],
        created_by=world.users["owner"]["id"],
    )
    conn.commit()
    assert protected["title"] == "Agent Renamed"
    assert protected["purpose"] == "Custom agent purpose"


def test_room_visibility_cross_project_writes_and_references_fail_closed(
    world: RoomWorld,
):
    """WHY: a valid Rooms token is not project authority, and even an admin may
    not attach a record from another project as if it belonged to this room."""
    conn = world.conn
    alpha_main = world.room_rows["alpha_main"]
    beta_main = world.room_rows["beta_main"]
    alpha_actor = world.actors["alpha_rooms"]

    assert rooms.can_see_room(conn, world.users["alpha"], alpha_main)
    assert not rooms.can_see_room(conn, world.users["alpha"], beta_main)
    assert (
        rooms.get_visible_room(
            conn, actor=world.users["alpha"], room_id=beta_main["id"]
        )
        is None
    )

    posted = room_commands.post_event(
        conn,
        actor=alpha_actor,
        room_id=alpha_main["id"],
        event_kind="evidence",
        body="Alpha evidence",
        reference_kind="issue",
        reference_id=world.issues["alpha"]["id"],
    )
    assert posted["reference_id"] == str(world.issues["alpha"]["id"])

    for hidden_room_id in (beta_main["id"], 999_999):
        with pytest.raises(room_commands.RoomCommandError) as exc:
            room_commands.post_event(
                conn,
                actor=alpha_actor,
                room_id=hidden_room_id,
                event_kind="message",
                body="must not land",
            )
        assert exc.value.kind == "not_found"
        assert exc.value.detail == "no such room"

    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.users["admin"],
            room_id=alpha_main["id"],
            event_kind="evidence",
            body="cross-project reference",
            reference_kind="issue",
            reference_id=world.issues["beta"]["id"],
        )
    assert exc.value.kind == "invalid"
    assert exc.value.detail == "referenced record is unavailable"


def test_imported_activity_and_run_cannot_masquerade_as_native_room_references(
    world: RoomWorld,
):
    """WHY: imported history is evidence Athena was told about, not a native
    authoritative record that a Room may cite as its own activity or run."""
    conn = world.conn
    room = world.room_rows["alpha_main"]
    actor = world.actors["alpha_rooms"]
    native = activity.record(
        conn,
        actor_id=world.users["owner"]["id"],
        verb="worked",
        target_kind="issue",
        target_id=world.issues["alpha"]["id"],
        detail="native evidence",
    )
    accepted = room_commands.post_event(
        conn,
        actor=actor,
        room_id=room["id"],
        event_kind="evidence",
        body="cite native evidence",
        reference_kind="activity",
        reference_id=native["id"],
    )
    assert accepted["reference_id"] == str(native["id"])

    imported = activity.record(
        conn,
        actor_id=world.users["owner"]["id"],
        verb="worked",
        target_kind="issue",
        target_id=world.issues["alpha"]["id"],
        detail="foreign evidence",
        imported_at="2026-07-31T12:00:00Z",
    )
    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=actor,
            room_id=room["id"],
            event_kind="evidence",
            body="must not cite imported activity",
            reference_kind="activity",
            reference_id=imported["id"],
        )
    assert exc.value.kind == "invalid"
    assert exc.value.detail == "referenced record is unavailable"

    forged_run = conn.execute(
        "INSERT INTO activity "
        "(actor_id, verb, target_kind, target_id, detail, run_id, "
        "visibility_restricted, imported_at) "
        "VALUES (?, 'worked', 'issue', ?, 'foreign run', ?, 0, ?)",
        (
            world.users["owner"]["id"],
            world.issues["alpha"]["id"],
            "foreign-run",
            "2026-07-31T12:00:00Z",
        ),
    ).lastrowid
    assert forged_run is not None
    conn.execute(
        "INSERT INTO activity_visibility_projects "
        "(event_id, project_scope_key) VALUES (?, ?)",
        (forged_run, world.projects["alpha"]["activity_scope_key"]),
    )
    native_run = conn.execute(
        "INSERT INTO activity "
        "(actor_id, verb, target_kind, target_id, detail, run_id, "
        "visibility_restricted) "
        "VALUES (?, 'worked', 'issue', ?, 'native run', ?, 0)",
        (
            world.users["owner"]["id"],
            world.issues["alpha"]["id"],
            "foreign-run",
        ),
    ).lastrowid
    assert native_run is not None
    conn.execute(
        "INSERT INTO activity_visibility_projects "
        "(event_id, project_scope_key) VALUES (?, ?)",
        (native_run, world.projects["alpha"]["activity_scope_key"]),
    )
    conn.commit()

    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=actor,
            room_id=room["id"],
            event_kind="evidence",
            body="must not cite imported run",
            reference_kind="run",
            reference_id="foreign-run",
        )
    assert exc.value.kind == "invalid"
    assert exc.value.detail == "referenced record is unavailable"


def test_live_actor_reauthorization_defeats_scope_role_and_identity_spoofing(
    world: RoomWorld,
):
    """WHY: command authorization must use the live user/token rows inside the
    write transaction, not caller-supplied role, is_agent, or scope claims."""
    conn = world.conn
    main = world.room_rows["alpha_main"]

    forged_scope = {
        **world.users["owner"],
        "_token_id": world.tokens["owner_read"]["id"],
        "_token_scopes": [tokens.ROOMS_WRITE_SCOPE],
    }
    wrong_owner = {
        **world.users["owner"],
        "_token_id": world.tokens["outsider_rooms"]["id"],
        "_token_scopes": [tokens.ROOMS_WRITE_SCOPE],
    }
    forged_agent = {**world.users["owner"], "is_agent": True}
    for actor, event_kind in (
        (forged_scope, "message"),
        (wrong_owner, "message"),
        (world.actors["viewer_rooms"], "message"),
        (world.users["agent"], "check_in"),
        (forged_agent, "check_in"),
    ):
        with pytest.raises(room_commands.RoomCommandError) as exc:
            room_commands.post_event(
                conn,
                actor=actor,
                room_id=main["id"],
                event_kind=event_kind,
                body="spoof attempt",
            )
        assert exc.value.kind == "forbidden"

    revoked_actor, revoked = _token_actor(
        conn,
        world.users["agent"],
        name="revoked-room-token",
        scopes=[tokens.ROOMS_WRITE_SCOPE],
    )
    assert tokens.revoke_token(
        conn,
        user_id=world.users["agent"]["id"],
        token_id=revoked["id"],
    )
    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=revoked_actor,
            room_id=world.room_rows["alpha_agent"]["id"],
            event_kind="check_in",
            body="revoked attempt",
        )
    assert exc.value.kind == "forbidden"

    users.set_paused(conn, world.users["alpha"]["id"], True)
    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.actors["alpha_rooms"],
            room_id=main["id"],
            event_kind="message",
            body="paused attempt",
        )
    assert exc.value.kind == "forbidden"

    valid = room_commands.post_event(
        conn,
        actor=world.actors["agent_rooms"],
        room_id=world.room_rows["alpha_agent"]["id"],
        event_kind="check_in",
        body="live agent check-in",
    )
    assert valid["actor_id"] == world.users["agent"]["id"]


def test_room_text_rejections_are_atomic_and_leave_no_search_or_audit_footprint(
    world: RoomWorld,
):
    """WHY: inert coordination prose must not become a filesystem, credential, or
    arbitrary structured-payload ingestion channel."""
    conn = world.conn
    room_id = world.room_rows["alpha_main"]["id"]
    bad_bodies = (
        '{"operation": "provider payload"}',
        "read /etc/passwd next",
        "read /data/private.key",
        "read /Users/alice/secret.txt",
        r"read \\server\share\secret.txt",
        "read src/athena/main.py",
        "read docs/ROOMS.md",
        "read ./secrets.env",
        r"read src\athena\main.py",
        "read secrets.env",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "control\x00character",
        "x" * (rooms.MAX_EVENT_BODY_CHARS + 1),
    )
    baseline = {
        "activity": conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM room_events").fetchone()[0],
        "search": conn.execute(
            "SELECT COUNT(*) FROM search_index WHERE kind = 'room_event'"
        ).fetchone()[0],
    }
    for body in bad_bodies:
        with pytest.raises(room_commands.RoomCommandError) as exc:
            room_commands.post_event(
                conn,
                actor=world.users["owner"],
                room_id=room_id,
                event_kind="message",
                body=body,
            )
        assert exc.value.kind == "invalid"
    assert (
        conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        == baseline["activity"]
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM room_events").fetchone()[0]
        == baseline["events"]
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM search_index WHERE kind = 'room_event'"
        ).fetchone()[0]
        == baseline["search"]
    )
    receipt_body = "Receipt: https://example.com/docs/ROOMS.md"
    accepted = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=room_id,
        event_kind="evidence",
        body=receipt_body,
    )
    assert accepted["body"] == receipt_body


def test_create_archive_policy_is_governor_only_and_archive_is_idempotent(
    world: RoomWorld,
):
    conn = world.conn
    owner = world.users["owner"]
    spare = world.users["spare_agent"]
    alpha = world.projects["alpha"]

    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.create_room(
            conn,
            actor={**world.actors["alpha_rooms"], "role": "admin"},
            project_id=world.projects["alpha"]["id"],
            room_type="agent",
            title="Forged governor",
            agent_id=spare["id"],
        )
    assert exc.value.kind == "forbidden"

    created = room_commands.create_room(
        conn,
        actor=owner,
        project_id=alpha["id"],
        room_type="agent",
        title="Custom agent room",
        purpose="Custom purpose",
        visibility="members",
        slug="custom-agent-room",
        agent_id=spare["id"],
    )
    assert created["slug"] == "custom-agent-room"
    assert created["visibility"] == "members"

    archived = room_commands.archive_room(conn, actor=owner, room_id=created["id"])
    repeated = room_commands.archive_room(conn, actor=owner, room_id=created["id"])
    assert archived["archived"] is True
    assert repeated["archived"] is True
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM activity "
            "WHERE verb = 'archived_room' AND target_id = ?",
            (created["id"],),
        ).fetchone()[0]
        == 1
    )

    for invariant in (
        world.room_rows["alpha_main"],
        world.room_rows["alpha_brief"],
    ):
        with pytest.raises(room_commands.RoomCommandError) as exc:
            room_commands.archive_room(conn, actor=owner, room_id=invariant["id"])
        assert exc.value.kind == "forbidden"


def test_room_events_are_append_only_digest_bound_and_superseded_once(
    world: RoomWorld,
):
    conn = world.conn
    main = world.room_rows["alpha_main"]
    body = "Original coordination decision"
    first = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=main["id"],
        event_kind="decision",
        body=body,
    )
    replacement = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=main["id"],
        event_kind="decision",
        body="Replacement decision",
        supersedes_event_id=first["id"],
    )
    assert first["body"] == body
    assert first["content_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert first["delivery_eligible"] is False
    assert replacement["supersedes_event_id"] == first["id"]
    assert (
        conn.execute(
            "SELECT detail FROM activity WHERE id = ?", (first["id"],)
        ).fetchone()["detail"]
        == body
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM search_index "
            "WHERE kind = 'room_event' AND source_id = ?",
            (first["id"],),
        ).fetchone()[0]
        == 1
    )

    cross_room_prior = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=main["id"],
        event_kind="message",
        body="cross-room predecessor",
    )
    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.users["owner"],
            room_id=world.room_rows["alpha_agent"]["id"],
            event_kind="message",
            body="cross-room successor",
            supersedes_event_id=cross_room_prior["id"],
        )
    assert exc.value.kind == "conflict"

    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.users["owner"],
            room_id=main["id"],
            event_kind="message",
            body="second successor",
            supersedes_event_id=first["id"],
        )
    assert exc.value.kind == "conflict"

    guarded_statements = (
        (
            "UPDATE room_events SET event_kind = 'message' WHERE activity_id = ?",
            first["id"],
        ),
        ("DELETE FROM room_events WHERE activity_id = ?", first["id"]),
        ("UPDATE activity SET detail = 'tampered' WHERE id = ?", first["id"]),
        ("DELETE FROM activity WHERE id = ?", first["id"]),
        (
            "UPDATE activity_visibility_projects "
            "SET project_scope_key = project_scope_key WHERE event_id = ?",
            first["id"],
        ),
        (
            "DELETE FROM activity_visibility_projects WHERE event_id = ?",
            first["id"],
        ),
        ("DELETE FROM rooms WHERE id = ?", main["id"]),
    )
    for sql, identifier in guarded_statements:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (identifier,))
        conn.rollback()

    assert rooms.get_room_event(conn, first["id"])["body"] == body
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM room_events WHERE supersedes_event_id = ?",
            (first["id"],),
        ).fetchone()[0]
        == 1
    )


def test_concurrent_supersession_serializes_to_one_successor(world: RoomWorld):
    """WHY: two writers racing the same predecessor must not both observe it as
    current and append two replacements."""
    first = room_commands.post_event(
        world.conn,
        actor=world.users["owner"],
        room_id=world.room_rows["alpha_main"]["id"],
        event_kind="message",
        body="race predecessor",
    )
    barrier = threading.Barrier(2)

    def attempt(index: int):
        conn = db.connect(world.db_file)
        try:
            barrier.wait()
            try:
                return room_commands.post_event(
                    conn,
                    actor=world.users["owner"],
                    room_id=world.room_rows["alpha_main"]["id"],
                    event_kind="message",
                    body=f"replacement {index}",
                    supersedes_event_id=first["id"],
                )["id"]
            except room_commands.RoomCommandError as exc:
                return exc.kind
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (1, 2)))
    assert sum(isinstance(result, int) for result in results) == 1
    assert results.count("conflict") == 1
    assert (
        world.conn.execute(
            "SELECT COUNT(*) FROM room_events WHERE supersedes_event_id = ?",
            (first["id"],),
        ).fetchone()[0]
        == 1
    )


def test_concurrent_default_ensure_converges_on_one_agent_room(world: RoomWorld):
    """WHY: assignment/contribution retries on separate connections must converge
    on the same project-local agent room rather than duplicate identity."""
    barrier = threading.Barrier(2)
    spare = world.users["spare_agent"]

    def ensure():
        conn = db.connect(world.db_file)
        try:
            barrier.wait()
            with db.transaction(conn, immediate=True):
                return rooms.ensure_agent_room(
                    conn,
                    project_id=world.projects["alpha"]["id"],
                    agent_id=spare["id"],
                    created_by=world.users["owner"]["id"],
                )["id"]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        room_ids = list(pool.map(lambda _index: ensure(), (1, 2)))
    assert room_ids[0] == room_ids[1]
    assert (
        world.conn.execute(
            "SELECT COUNT(*) FROM rooms "
            "WHERE project_scope_key = ? AND room_type = 'agent' AND agent_id = ?",
            (world.projects["alpha"]["activity_scope_key"], spare["id"]),
        ).fetchone()[0]
        == 1
    )


def test_work_room_moves_detaches_rescopes_and_outlives_deleted_targets(
    world: RoomWorld,
):
    conn = world.conn
    issue = world.issues["alpha"]
    room = world.room_rows["alpha_work"]
    original_slug = room["slug"]
    old_event = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=room["id"],
        event_kind="message",
        body="Alpha historical prose",
    )

    with db.transaction(conn, immediate=True):
        issues.set_project(conn, issue["id"], None, commit=False)
        detached = rooms.move_work_item_room(
            conn, issue_id=issue["id"], project_id=None
        )
    assert detached["id"] == room["id"]
    assert detached["project_id"] == world.projects["alpha"]["id"]
    assert detached["slug"] == original_slug
    assert detached["link_state"] == "linked_work_moved"
    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.users["owner"],
            room_id=room["id"],
            event_kind="message",
            body="detached write",
        )
    assert exc.value.kind == "conflict"

    with db.transaction(conn, immediate=True):
        issues.update_issue(conn, issue["id"], title="Detached title", commit=False)
        title_synced = rooms.ensure_work_item_room(conn, issue_id=issue["id"])
    assert title_synced["title"] == "Detached title"
    assert title_synced["link_state"] == "linked_work_moved"

    with db.transaction(conn, immediate=True):
        issues.set_project(
            conn, issue["id"], world.projects["beta"]["id"], commit=False
        )
        moved = rooms.move_work_item_room(
            conn,
            issue_id=issue["id"],
            project_id=world.projects["beta"]["id"],
        )
    assert moved["id"] == room["id"]
    assert moved["slug"] == original_slug
    assert moved["project_id"] == world.projects["beta"]["id"]
    old_scope = conn.execute(
        "SELECT project_scope_key FROM activity_visibility_projects WHERE event_id = ?",
        (old_event["id"],),
    ).fetchone()["project_scope_key"]
    assert old_scope == world.projects["alpha"]["activity_scope_key"]

    new_event = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=room["id"],
        event_kind="message",
        body="Beta current prose",
    )
    new_scope = conn.execute(
        "SELECT project_scope_key FROM activity_visibility_projects WHERE event_id = ?",
        (new_event["id"],),
    ).fetchone()["project_scope_key"]
    assert new_scope == world.projects["beta"]["activity_scope_key"]

    conn.execute("DELETE FROM issues WHERE id = ?", (issue["id"],))
    conn.commit()
    degraded = rooms.get_room(conn, room["id"])
    assert degraded["link_state"] == "linked_work_moved"
    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.users["owner"],
            room_id=room["id"],
            event_kind="message",
            body="deleted target write",
        )
    assert exc.value.kind == "conflict"

    empty_project = projects.create_project(
        conn,
        name="Disposable",
        key="DSP",
        created_by=world.users["owner"]["id"],
    )
    defaults = rooms.ensure_project_rooms(conn, project_id=empty_project["id"])
    conn.commit()
    assert projects.delete_project(conn, empty_project["id"])
    assert (
        rooms.get_room(conn, defaults["project"]["id"])["link_state"]
        == "owning_project_unavailable"
    )

    agent_room = world.room_rows["alpha_agent"]
    conn.execute(
        "UPDATE users SET is_agent = 0 WHERE id = ?",
        (world.users["agent"]["id"],),
    )
    conn.commit()
    assert (
        rooms.get_room(conn, agent_room["id"])["link_state"]
        == "linked_agent_unavailable"
    )
    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.users["owner"],
            room_id=agent_room["id"],
            event_kind="message",
            body="former agent write",
        )
    assert exc.value.kind == "conflict"


def test_issue_move_ensures_destination_rooms_for_all_agent_contributors(
    world: RoomWorld,
):
    """WHY: migration backfills contributor rooms, so the live move path must
    preserve the same invariant without silently granting project membership."""
    conn = world.conn
    issue = world.issues["alpha"]
    agent_ids = {
        world.users["agent"]["id"],
        world.users["spare_agent"]["id"],
    }
    for agent_id in sorted(agent_ids):
        issue_commands.add_contributor(
            conn,
            actor=world.users["owner"],
            issue_id=issue["id"],
            user_id=agent_id,
        )

    beta = world.projects["beta"]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM rooms WHERE project_scope_key = ? "
            "AND room_type = 'agent' AND agent_id IN (?, ?)",
            (beta["activity_scope_key"], *sorted(agent_ids)),
        ).fetchone()[0]
        == 0
    )

    moved = issue_commands.update_issue(
        conn,
        actor=world.users["owner"],
        issue_id=issue["id"],
        project_id=beta["id"],
    )
    assert moved["project_id"] == beta["id"]
    destination_agents = {
        row["agent_id"]
        for row in conn.execute(
            "SELECT agent_id FROM rooms WHERE project_scope_key = ? "
            "AND room_type = 'agent'",
            (beta["activity_scope_key"],),
        ).fetchall()
    }
    assert destination_agents == agent_ids
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM project_members WHERE project_id = ? "
            "AND user_id IN (?, ?)",
            (beta["id"], *sorted(agent_ids)),
        ).fetchone()[0]
        == 0
    )


def test_search_applies_current_room_access_and_immutable_event_envelope_before_limit(
    world: RoomWorld,
):
    """WHY: moving a stable room to B must not let a B-only reader recover old A
    prose through FTS, even when the hidden hit would otherwise fill LIMIT 1."""
    conn = world.conn
    issue = world.issues["alpha"]
    room = world.room_rows["alpha_work"]
    old = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=room["id"],
        event_kind="message",
        body="sharedrank Alpha secret",
    )
    with db.transaction(conn, immediate=True):
        issues.set_project(
            conn, issue["id"], world.projects["beta"]["id"], commit=False
        )
        rooms.move_work_item_room(
            conn,
            issue_id=issue["id"],
            project_id=world.projects["beta"]["id"],
        )

    assert (
        search.search(
            conn,
            "sharedrank",
            kind="room_event",
            limit=1,
            actor=world.actors["beta_rooms"],
        )
        == []
    )
    assert [
        hit["source_id"]
        for hit in search.search(
            conn,
            "sharedrank",
            kind="room_event",
            actor=world.users["admin"],
        )
    ] == [old["id"]]

    current = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=room["id"],
        event_kind="message",
        body="sharedrank Beta visible",
    )
    visible = search.search(
        conn,
        "sharedrank",
        kind="room_event",
        limit=1,
        actor=world.actors["beta_rooms"],
    )
    assert [hit["source_id"] for hit in visible] == [current["id"]]

    with pytest.raises(room_commands.RoomCommandError) as exc:
        room_commands.post_event(
            conn,
            actor=world.actors["beta_rooms"],
            room_id=room["id"],
            event_kind="message",
            body="blind replacement",
            supersedes_event_id=old["id"],
        )
    assert exc.value.kind == "conflict"

    room_commands.archive_room(conn, actor=world.users["owner"], room_id=room["id"])
    assert (
        search.search(
            conn,
            "Beta visible",
            kind="room_event",
            actor=world.actors["beta_rooms"],
        )
        == []
    )
    assert [
        hit["source_id"]
        for hit in search.search(
            conn,
            "Beta visible",
            kind="room_event",
            include_archived=True,
            actor=world.actors["beta_rooms"],
        )
    ] == [current["id"]]


def test_imported_activity_cannot_masquerade_as_room_state_or_search_hit(
    world: RoomWorld,
):
    """WHY: even corrupted legacy metadata must not turn foreign activity into a
    native Room event for raw reads or visibility-gated FTS consumers."""
    conn = world.conn
    room = world.room_rows["alpha_main"]
    marker = "importedmask"
    native = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=room["id"],
        event_kind="message",
        body=f"{marker} native room state",
    )
    imported = activity.record(
        conn,
        actor_id=world.users["owner"]["id"],
        verb="room_message",
        target_kind="room",
        target_id=room["id"],
        detail=f"{marker} foreign room state",
        delivery_eligible=False,
        imported_at="2026-07-31T12:00:00Z",
    )
    digest = hashlib.sha256(imported["detail"].encode("utf-8")).hexdigest()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO room_events "
            "(activity_id, room_id, event_kind, content_sha256) "
            "VALUES (?, ?, 'message', ?)",
            (imported["id"], room["id"], digest),
        )
    conn.rollback()

    # Simulate a corrupt/legacy database that predates the write trigger. The
    # read/search predicates remain a second line of defense.
    conn.execute("DROP TRIGGER room_event_activity_required")
    conn.execute(
        "INSERT INTO room_events "
        "(activity_id, room_id, event_kind, content_sha256) "
        "VALUES (?, ?, 'message', ?)",
        (imported["id"], room["id"], digest),
    )
    search.index_document(
        conn,
        kind="room_event",
        source_id=imported["id"],
        commit=False,
    )
    conn.commit()

    assert rooms.get_room_event(conn, imported["id"]) is None
    listed_ids = {
        event["id"] for event in rooms.list_room_events(conn, room["id"], limit=200)
    }
    assert native["id"] in listed_ids
    assert imported["id"] not in listed_ids
    for actor in (world.users["admin"], world.actors["alpha_rooms"]):
        hits = search.search(
            conn,
            marker,
            kind="room_event",
            actor=actor,
        )
        assert [hit["source_id"] for hit in hits] == [native["id"]]


class _Poster:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, body, headers):
        self.calls.append({"url": url, "body": body, "headers": headers})
        return True, None


def test_room_events_are_inert_for_automation_and_webhook_delivery(
    world: RoomWorld,
):
    """WHY: room prose is coordination evidence, never an execution trigger or an
    outbound-delivery event, and an ineligible gap must not starve later work."""
    conn = world.conn
    tip = webhooks.current_tip(conn)
    conn.execute("UPDATE automation_state SET cursor = ? WHERE id = 1", (tip,))
    rule = automation.create_rule(
        conn,
        name="all issue events",
        trigger_verb="*",
        target_kind="issue",
        action_type="comment",
        action_params={"body": "unused"},
        created_by=world.users["owner"]["id"],
    )
    webhook = webhooks.create_webhook(
        conn,
        url="https://93.184.216.34/hook",
        created_by=world.users["admin"]["id"],
        event_kind=None,
        start_cursor=tip,
    )

    room_event = room_commands.post_event(
        conn,
        actor=world.users["owner"],
        room_id=world.room_rows["alpha_main"]["id"],
        event_kind="message",
        body="inert coordination only",
    )
    calls: list[tuple[int, int]] = []
    assert (
        automation.process_pending(
            conn,
            executor=lambda _conn, fired_rule, event: calls.append(
                (fired_rule["id"], event["id"])
            ),
        )
        == 0
    )
    assert calls == []
    assert automation.get_cursor(conn) == tip

    poster = _Poster()
    assert webhooks.deliver_pending(conn, poster=poster) == 0
    assert poster.calls == []
    assert webhooks.get_webhook(conn, webhook["id"])["cursor"] == tip
    assert room_event["id"] not in {
        event["id"]
        for event in activity.list_events(conn, delivery_eligible=True, limit=200)
    }
    assert room_event["id"] in {
        event["id"]
        for event in activity.list_events(conn, delivery_eligible=False, limit=200)
    }

    eligible = activity.record(
        conn,
        actor_id=world.users["owner"]["id"],
        verb="created",
        target_kind="issue",
        target_id=world.issues["alpha"]["id"],
        detail="eligible event",
    )
    assert (
        automation.process_pending(
            conn,
            executor=lambda _conn, fired_rule, event: calls.append(
                (fired_rule["id"], event["id"])
            ),
        )
        == 1
    )
    assert calls == [(rule["id"], eligible["id"])]
    assert automation.get_cursor(conn) == eligible["id"]

    assert webhooks.deliver_pending(conn, poster=poster) == 1
    assert len(poster.calls) == 1
    assert poster.calls[0]["headers"]["X-Athena-Event-Id"] == str(eligible["id"])
    assert webhooks.get_webhook(conn, webhook["id"])["cursor"] == eligible["id"]


def test_room_list_keyset_is_bounded_and_filters_members_before_limit(
    world: RoomWorld,
):
    """WHY: a hidden members-only row between two visible ids must not consume the
    page slot or make a caller believe the visible tail is empty."""
    conn = world.conn
    public = projects.create_project(
        conn,
        name="Public Rooms",
        key="PUB",
        created_by=world.users["owner"]["id"],
    )
    defaults = rooms.ensure_project_rooms(conn, project_id=public["id"])
    hidden_agent = rooms.ensure_agent_room(
        conn,
        project_id=public["id"],
        agent_id=world.users["spare_agent"]["id"],
        created_by=world.users["owner"]["id"],
    )
    issue = issues.create_issue(
        conn,
        title="Visible focused room",
        body="",
        created_by=world.users["owner"]["id"],
        project_id=public["id"],
    )
    visible_work = rooms.ensure_work_item_room(conn, issue_id=issue["id"])
    conn.commit()
    assert defaults["brief"]["id"] < hidden_agent["id"] < visible_work["id"]

    outsider = world.users["outsider"]
    first = rooms.list_rooms_page(conn, public["id"], actor=outsider, limit=1)
    second = rooms.list_rooms_page(
        conn,
        public["id"],
        actor=outsider,
        after_id=first[0]["id"],
        limit=1,
    )
    tail = rooms.list_rooms_page(
        conn,
        public["id"],
        actor=outsider,
        after_id=second[0]["id"],
        limit=1,
    )
    assert [first[0]["id"], second[0]["id"], tail[0]["id"]] == [
        defaults["project"]["id"],
        defaults["brief"]["id"],
        visible_work["id"],
    ]
    assert hidden_agent["id"] not in {
        room["id"]
        for room in rooms.list_rooms_page(conn, public["id"], actor=outsider, limit=20)
    }
    assert [
        room["id"]
        for room in rooms.list_rooms_page(
            conn,
            public["id"],
            actor=None,
            after_id=defaults["brief"]["id"],
            limit=1,
        )
    ] == [visible_work["id"]]
    assert (
        rooms.list_rooms_page(
            conn,
            public["id"],
            actor=outsider,
            after_id=visible_work["id"],
            limit=1,
        )
        == []
    )

    for invalid_limit in (True, 0, 201):
        with pytest.raises(ValueError):
            rooms.list_rooms_page(
                conn, public["id"], actor=outsider, limit=invalid_limit
            )
    for invalid_cursor in (True, -1):
        with pytest.raises(ValueError):
            rooms.list_rooms_page(
                conn,
                public["id"],
                actor=outsider,
                after_id=invalid_cursor,
            )
