"""Browser contract tests for project-scoped Athena Rooms.

These tests stay at the HTTP boundary for the behavior under test. Setup uses the
same command owners as production so room visibility, lifecycle, and activity
projections are real rather than template-shaped fixtures.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from athena.aegis import issue_commands, room_commands, room_timeline, rooms
from athena.core import access, db, users
from athena.main import create_app


@dataclass
class RoomsBrowser:
    client: TestClient
    db_file: Path
    users: dict[str, dict]
    project: dict
    private_project: dict
    issue: dict
    rooms: dict[str, dict]


def _vary(response) -> set[str]:
    return {
        dimension.strip().casefold()
        for dimension in response.headers.get("vary", "").split(",")
        if dimension.strip()
    }


def _assert_private(response) -> None:
    assert response.headers["cache-control"] == "private, no-store"
    assert "cookie" in _vary(response)


def _actor_headers(actor: dict, **extra: str) -> dict[str, str]:
    return {"X-Athena-Actor": str(actor["id"]), **extra}


def _room_card_ids(page_html: str) -> list[int]:
    return [
        int(room_id)
        for room_id in re.findall(
            r'<a href="/aegis/rooms/(\d+)" class="room-card-title">',
            page_html,
        )
    ]


def _opaque_cursor(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _create_user(
    client: TestClient,
    *,
    email: str,
    name: str,
    actor: dict | None = None,
    role: str = "member",
    is_agent: bool = False,
) -> dict:
    headers = _actor_headers(actor) if actor is not None else None
    response = client.post(
        "/users",
        json={
            "email": email,
            "name": name,
            "password": "pw",
            "role": role,
            "is_agent": is_agent,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(browser: RoomsBrowser, email: str) -> str:
    client = browser.client
    client.cookies.clear()
    for name in ("X-CSRF-Token", "X-Athena-Actor", "Authorization"):
        client.headers.pop(name, None)
    response = client.post(
        "/login",
        data={"email": email, "password": "pw"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    token = client.cookies.get("athena_csrf")
    assert token
    return token


def _inert_side_effect_snapshot(db_file: Path) -> dict[str, object]:
    conn = db.connect(db_file)
    try:
        count_tables = (
            "icarus_dispatches",
            "approval_requests",
            "webhooks",
            "automation_rules",
            "automation_schedule_firings",
            "automation_schedule_occurrences",
        )
        snapshot: dict[str, object] = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in count_tables
        }
        snapshot["automation_schedule_state"] = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, scan_cursor FROM automation_schedule_state ORDER BY id"
            ).fetchall()
        ]
        return snapshot
    finally:
        conn.close()


@pytest.fixture
def browser(tmp_path: Path) -> RoomsBrowser:
    db_file = tmp_path / "rooms-web.db"
    with TestClient(create_app(db_file)) as client:
        admin = _create_user(
            client,
            email="admin@example.com",
            name="Admin",
            role="admin",
        )
        owner = _create_user(
            client,
            email="owner@example.com",
            name="Owner",
            actor=admin,
        )
        member = _create_user(
            client,
            email="member@example.com",
            name="Project Member",
            actor=admin,
        )
        outsider = _create_user(
            client,
            email="outsider@example.com",
            name="Outsider",
            actor=admin,
        )
        agent = _create_user(
            client,
            email="agent@example.com",
            name="Room Agent",
            actor=admin,
            is_agent=True,
        )

        project_response = client.post(
            "/projects",
            json={
                "name": "Rooms Project",
                "key": "ROOM",
                "description": "Durable coordination test project",
            },
            headers=_actor_headers(owner),
        )
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()

        private_response = client.post(
            "/projects",
            json={"name": "Hidden Rooms", "key": "HIDE"},
            headers=_actor_headers(owner),
        )
        assert private_response.status_code == 201, private_response.text
        private_project = private_response.json()
        visibility = client.put(
            f"/projects/{private_project['id']}/visibility",
            json={"visibility": "private"},
            headers=_actor_headers(owner),
        )
        assert visibility.status_code == 200, visibility.text

        issue_response = client.post(
            "/issues",
            json={
                "title": "Room-backed issue",
                "body": "Initial evidence for the room context packet",
                "project_id": project["id"],
            },
            headers=_actor_headers(owner),
        )
        assert issue_response.status_code == 201, issue_response.text
        issue = issue_response.json()

        conn = db.connect(db_file)
        try:
            owner_actor = users.get_user(conn, owner["id"])
            assert owner_actor is not None
            access.add_project_member(
                conn,
                project["id"],
                member["id"],
                added_by=owner["id"],
            )
            member_room = room_commands.create_room(
                conn,
                actor=owner_actor,
                project_id=project["id"],
                room_type="agent",
                title="Members agent room",
                purpose="Explicit project-member coordination",
                visibility="members",
                slug="members-agent",
                agent_id=agent["id"],
            )
            project_rooms = rooms.list_rooms(
                conn,
                project["id"],
                actor=owner_actor,
                include_archived=True,
            )
            main_room = next(
                item for item in project_rooms if item["room_type"] == "project"
            )
            brief_room = next(
                item for item in project_rooms if item["room_type"] == "brief"
            )
            work_room = rooms.get_work_item_room(conn, issue["id"])
            assert work_room is not None
            hidden_main = rooms.get_room_by_slug(conn, private_project["id"], "main")
            assert hidden_main is not None
        finally:
            conn.close()

        harness = RoomsBrowser(
            client=client,
            db_file=db_file,
            users={
                "admin": admin,
                "owner": owner,
                "member": member,
                "outsider": outsider,
                "agent": agent,
            },
            project=project,
            private_project=private_project,
            issue=issue,
            rooms={
                "main": main_room,
                "brief": brief_room,
                "work": work_room,
                "members": member_room,
                "hidden_main": hidden_main,
            },
        )
        _login(harness, "owner@example.com")
        yield harness


def test_authenticated_project_hub_list_and_room_detail(browser: RoomsBrowser):
    client = browser.client
    project_id = browser.project["id"]
    work_room = browser.rooms["work"]
    csrf = client.cookies.get("athena_csrf")

    projects_page = client.get("/aegis/projects")
    hub = client.get(f"/aegis/projects/{project_id}")
    room_list = client.get(f"/aegis/projects/{project_id}/rooms")
    detail = client.get(f"/aegis/rooms/{work_room['id']}")

    assert projects_page.status_code == hub.status_code == room_list.status_code == 200
    assert detail.status_code == 200
    assert f'href="/aegis/projects/{project_id}"' in projects_page.text
    for response in (hub, room_list):
        assert "Rooms Project" in response.text
        assert "Rooms" in response.text
        assert f'href="/aegis/rooms/{work_room["id"]}"' in response.text
        _assert_private(response)

    assert "Room-backed issue" in detail.text
    assert "Record an update" in detail.text
    assert f'action="/aegis/rooms/{work_room["id"]}/events"' in detail.text
    assert f'action="/aegis/rooms/{work_room["id"]}/ask#room-context"' in detail.text
    assert f'hx-post="/aegis/rooms/{work_room["id"]}/ask"' in detail.text
    assert (
        f'hx-post="/aegis/rooms/{work_room["id"]}/ask#room-context"' not in detail.text
    )
    assert f'name="csrf_token" value="{csrf}"' in detail.text
    assert 'name="actor"' not in detail.text
    _assert_private(detail)


def test_project_room_list_uses_bounded_scoped_keyset_pages(
    browser: RoomsBrowser,
):
    client = browser.client
    project_id = browser.project["id"]
    conn = db.connect(browser.db_file)
    try:
        actor = users.get_user(conn, browser.users["owner"]["id"])
        assert actor is not None
        generated_rooms = []
        for index in range(49):
            issue = issue_commands.create_issue(
                conn,
                actor=actor,
                title=f"Paginated room issue {index:02d}",
                project_id=project_id,
            )
            generated_room = rooms.get_work_item_room(conn, issue["id"])
            assert generated_room is not None
            generated_rooms.append(generated_room)
        room_commands.archive_room(
            conn,
            actor=actor,
            room_id=generated_rooms[-1]["id"],
        )
    finally:
        conn.close()

    first = client.get(f"/aegis/projects/{project_id}")
    assert first.status_code == 200
    first_ids = _room_card_ids(first.text)
    assert len(first_ids) == 50
    assert first_ids == sorted(first_ids)
    _assert_private(first)

    rest = client.get(
        f"/projects/{project_id}/rooms",
        params={"limit": 50},
        headers=_actor_headers(browser.users["owner"]),
    )
    assert rest.status_code == 200, rest.text
    assert [item["id"] for item in rest.json()["items"]] == first_ids

    cursor_match = re.search(
        rf'href="/aegis/projects/{project_id}\?cursor=([^"&]+)"',
        first.text,
    )
    assert cursor_match is not None
    cursor = cursor_match.group(1)

    second = client.get(
        f"/aegis/projects/{project_id}",
        params={"cursor": cursor},
    )
    assert second.status_code == 200
    second_ids = _room_card_ids(second.text)
    assert len(second_ids) == 2
    assert second_ids == sorted(second_ids)
    assert set(first_ids).isdisjoint(second_ids)
    assert "First rooms" in second.text
    assert f'href="/aegis/rooms/{browser.rooms["brief"]["id"]}"' in second.text
    _assert_private(second)

    archived_first = client.get(
        f"/aegis/projects/{project_id}",
        params={"archived": "1"},
    )
    archived_cursor_match = re.search(
        rf'href="/aegis/projects/{project_id}\?cursor=([^"&]+)&amp;archived=1"',
        archived_first.text,
    )
    assert archived_cursor_match is not None
    archived_second = client.get(
        f"/aegis/projects/{project_id}",
        params={
            "cursor": archived_cursor_match.group(1),
            "archived": "1",
        },
    )
    assert archived_second.status_code == 200
    assert f'href="/aegis/projects/{project_id}?archived=1"' in archived_second.text
    _assert_private(archived_second)

    invalid_responses = (
        client.get(
            f"/aegis/projects/{project_id}",
            params={"cursor": "not-a-cursor"},
        ),
        client.get(
            f"/aegis/projects/{project_id}",
            params={"cursor": f"{cursor}="},
        ),
        client.get(
            f"/aegis/projects/{browser.private_project['id']}",
            params={"cursor": cursor},
        ),
        client.get(
            f"/aegis/projects/{project_id}",
            params={"cursor": cursor, "archived": "1"},
        ),
    )
    for response in invalid_responses:
        assert response.status_code == 400
        assert "Invalid room-list cursor" in response.text
        assert "another project or archive view" in response.text
        _assert_private(response)
    assert (
        f'href="/aegis/projects/{project_id}?archived=1"' in invalid_responses[-1].text
    )

    _login(browser, "outsider@example.com")
    outsider_first = client.get(f"/aegis/projects/{project_id}")
    outsider_ids = _room_card_ids(outsider_first.text)
    assert len(outsider_ids) == 50
    assert browser.rooms["members"]["id"] not in outsider_ids
    outsider_cursor_match = re.search(
        rf'href="/aegis/projects/{project_id}\?cursor=([^"&]+)"',
        outsider_first.text,
    )
    assert outsider_cursor_match is not None
    outsider_second = client.get(
        f"/aegis/projects/{project_id}",
        params={"cursor": outsider_cursor_match.group(1)},
    )
    assert len(_room_card_ids(outsider_second.text)) == 1

    hidden_invalid = client.get(
        f"/aegis/projects/{browser.private_project['id']}",
        params={"cursor": "not-a-cursor"},
    )
    assert hidden_invalid.status_code == 404
    assert "Invalid room-list cursor" not in hidden_invalid.text
    _assert_private(hidden_invalid)


def test_browser_ids_and_decoded_cursors_stay_within_sqlite_bounds(
    browser: RoomsBrowser,
):
    client = browser.client
    huge_id = 1 << 63
    project_id = browser.project["id"]
    room_id = browser.rooms["work"]["id"]

    missing_project = client.get("/aegis/projects/999999999")
    huge_project_responses = (
        client.get(f"/aegis/projects/{huge_id}"),
        client.get(
            f"/aegis/projects/{huge_id}/rooms",
            params={"cursor": "not-a-cursor"},
        ),
    )
    for response in huge_project_responses:
        assert response.status_code == 404
        assert response.text == missing_project.text
        _assert_private(response)

    missing_room = client.get("/aegis/rooms/999999999")
    csrf = client.cookies.get("athena_csrf")
    huge_room_responses = (
        client.get(f"/aegis/rooms/{huge_id}"),
        client.get(
            f"/aegis/rooms/{huge_id}/timeline",
            params={"cursor": "not-a-cursor"},
        ),
        client.post(
            f"/aegis/rooms/{huge_id}/events",
            data={
                "event_kind": "message",
                "body": "must not reach a command",
                "csrf_token": csrf,
            },
        ),
        client.post(
            f"/aegis/rooms/{huge_id}/ask",
            data={"question": "Must this reach a projection?", "csrf_token": csrf},
        ),
        client.post(
            f"/aegis/rooms/{huge_id}/archive",
            data={"csrf_token": csrf},
        ),
    )
    for response in huge_room_responses:
        assert response.status_code == 404
        assert response.text == missing_room.text
        _assert_private(response)

    huge_room_list_cursor = _opaque_cursor(
        f"athena.web-room-list.v1:{project_id}:0:{huge_id}"
    )
    invalid_room_list = client.get(
        f"/aegis/projects/{project_id}",
        params={"cursor": huge_room_list_cursor},
    )
    assert invalid_room_list.status_code == 400
    assert "Invalid room-list cursor" in invalid_room_list.text
    assert "another project or archive view" in invalid_room_list.text
    _assert_private(invalid_room_list)

    huge_timeline_cursor = _opaque_cursor(
        f"athena.room-timeline.v1:{room_id}:{huge_id}"
    )
    invalid_timeline_responses = (
        client.get(
            f"/aegis/rooms/{room_id}",
            params={"cursor": huge_timeline_cursor},
        ),
        client.get(
            f"/aegis/rooms/{room_id}/timeline",
            params={"cursor": huge_timeline_cursor},
        ),
    )
    for response in invalid_timeline_responses:
        assert response.status_code == 400
        assert "Invalid timeline cursor" in response.text
        assert "outside the bounded contract" in response.text
        _assert_private(response)


def test_members_and_private_resources_hide_exactly_like_missing(
    browser: RoomsBrowser,
):
    client = browser.client
    members_room_id = browser.rooms["members"]["id"]

    _login(browser, "member@example.com")
    member_view = client.get(f"/aegis/rooms/{members_room_id}")
    assert member_view.status_code == 200
    assert "Members agent room" in member_view.text
    _assert_private(member_view)

    _login(browser, "outsider@example.com")
    hidden_room = client.get(f"/aegis/rooms/{members_room_id}")
    missing_room = client.get("/aegis/rooms/999999999")
    assert hidden_room.status_code == missing_room.status_code == 404
    assert hidden_room.text == missing_room.text
    _assert_private(hidden_room)
    _assert_private(missing_room)

    hidden_project = client.get(f"/aegis/projects/{browser.private_project['id']}")
    missing_project = client.get("/aegis/projects/999999999")
    assert hidden_project.status_code == missing_project.status_code == 404
    assert hidden_project.text == missing_project.text
    assert "Hidden Rooms" not in hidden_project.text
    _assert_private(hidden_project)
    _assert_private(missing_project)


def test_capability_unavailable_reason_is_humanized(browser: RoomsBrowser):
    room_id = browser.rooms["work"]["id"]
    conn = db.connect(browser.db_file)
    try:
        agent = users.get_user(conn, browser.users["agent"]["id"])
        assert agent is not None
        access.add_project_member(
            conn,
            browser.project["id"],
            agent["id"],
            added_by=browser.users["owner"]["id"],
        )
    finally:
        conn.close()

    detail = browser.client.get(f"/aegis/rooms/{room_id}")
    assert detail.status_code == 200
    assert re.search(
        r"Capability details unavailable:\s+admin only\.",
        detail.text,
    )
    assert "admin_only" not in detail.text
    _assert_private(detail)


def test_event_post_requires_csrf_and_has_no_control_plane_side_effects(
    browser: RoomsBrowser,
):
    client = browser.client
    room_id = browser.rooms["work"]["id"]
    before_side_effects = _inert_side_effect_snapshot(browser.db_file)

    conn = db.connect(browser.db_file)
    before_events = conn.execute(
        "SELECT COUNT(*) AS n FROM room_events WHERE room_id = ?", (room_id,)
    ).fetchone()["n"]
    conn.close()

    refused = client.post(
        f"/aegis/rooms/{room_id}/events",
        data={"event_kind": "decision", "body": "forged room decision"},
    )
    assert refused.status_code == 403
    _assert_private(refused)

    token = client.cookies.get("athena_csrf")
    posted = client.post(
        f"/aegis/rooms/{room_id}/events",
        data={
            "event_kind": "decision",
            "body": "Inert decision: do not dispatch, approve, webhook, or schedule",
        },
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert posted.status_code == 200
    assert "Inert decision" in posted.text
    assert 'id="room-stream"' in posted.text
    assert "<!DOCTYPE html>" not in posted.text
    _assert_private(posted)

    native = client.post(
        f"/aegis/rooms/{room_id}/events",
        data={
            "event_kind": "message",
            "body": "Native inert coordination note",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert native.status_code == 303
    assert native.headers["location"] == f"/aegis/rooms/{room_id}"
    _assert_private(native)

    assert _inert_side_effect_snapshot(browser.db_file) == before_side_effects
    conn = db.connect(browser.db_file)
    try:
        rows = conn.execute(
            "SELECT a.actor_id, a.delivery_eligible, a.detail "
            "FROM room_events re JOIN activity a ON a.id = re.activity_id "
            "WHERE re.room_id = ? ORDER BY a.id",
            (room_id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == before_events + 2
    assert [row["detail"] for row in rows[-2:]] == [
        "Inert decision: do not dispatch, approve, webhook, or schedule",
        "Native inert coordination note",
    ]
    assert all(row["actor_id"] == browser.users["owner"]["id"] for row in rows[-2:])
    assert all(row["delivery_eligible"] == 0 for row in rows[-2:])


def test_timeline_cursor_pagination_has_htmx_and_native_paths(
    browser: RoomsBrowser,
):
    client = browser.client
    room_id = browser.rooms["work"]["id"]
    conn = db.connect(browser.db_file)
    try:
        actor = users.get_user(conn, browser.users["owner"]["id"])
        assert actor is not None
        for index in range(55):
            room_commands.post_event(
                conn,
                actor=actor,
                room_id=room_id,
                event_kind="message",
                body=f"timeline-event-{index:02d}",
            )
        first_page = room_timeline.list_timeline(
            conn,
            room_id,
            actor=actor,
            limit=50,
        )
        assert first_page is not None
        cursor = first_page["page"]["next_cursor"]
        assert first_page["page"]["has_more"] is True
        assert cursor
    finally:
        conn.close()

    newest = client.get(f"/aegis/rooms/{room_id}")
    assert newest.status_code == 200
    assert "timeline-event-54" in newest.text
    assert "Older activity" in newest.text
    assert "timeline-event-00" not in newest.text
    _assert_private(newest)

    fragment = client.get(
        f"/aegis/rooms/{room_id}/timeline",
        params={"cursor": cursor},
        headers={"HX-Request": "true"},
    )
    assert fragment.status_code == 200
    assert "timeline-event-00" in fragment.text
    assert "<!DOCTYPE html>" not in fragment.text
    assert 'id="room-stream"' not in fragment.text
    _assert_private(fragment)

    native = client.get(
        f"/aegis/rooms/{room_id}/timeline",
        params={"cursor": cursor},
    )
    assert native.status_code == 200
    assert "<!DOCTYPE html>" in native.text
    assert "timeline-event-00" in native.text
    assert "Return to newest activity" in native.text
    _assert_private(native)


def test_ask_room_renders_v1_packet_and_source_receipts_without_writes(
    browser: RoomsBrowser,
):
    client = browser.client
    room_id = browser.rooms["work"]["id"]
    conn = db.connect(browser.db_file)
    try:
        actor = users.get_user(conn, browser.users["owner"]["id"])
        assert actor is not None
        room_commands.post_event(
            conn,
            actor=actor,
            room_id=room_id,
            event_kind="evidence",
            body="ORBIT evidence belongs in the bounded context packet",
        )
        before = tuple(
            conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("activity", "room_events")
        )
    finally:
        conn.close()

    token = client.cookies.get("athena_csrf")
    fragment = client.post(
        f"/aegis/rooms/{room_id}/ask",
        data={"question": "Where is the ORBIT evidence?"},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert fragment.status_code == 200
    assert "<!DOCTYPE html>" not in fragment.text
    assert "athena.room-context.v1" in fragment.text
    assert "Sources assembled — no generated answer." in fragment.text
    assert "Where is the ORBIT evidence?" in fragment.text
    assert "ORBIT evidence belongs" in fragment.text
    assert 'href="/events?after=' in fragment.text
    assert "<code>GET</code>" in fragment.text
    _assert_private(fragment)

    native = client.post(
        f"/aegis/rooms/{room_id}/ask",
        data={
            "question": "Where is the ORBIT evidence?",
            "csrf_token": token,
        },
    )
    assert native.status_code == 200
    assert "<!DOCTYPE html>" in native.text
    assert "athena.room-context.v1" in native.text
    _assert_private(native)

    conn = db.connect(browser.db_file)
    try:
        after = tuple(
            conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("activity", "room_events")
        )
    finally:
        conn.close()
    assert after == before


def test_archive_and_invariant_rooms_are_read_only(browser: RoomsBrowser):
    client = browser.client
    token = client.cookies.get("athena_csrf")
    work_room = browser.rooms["work"]

    for invariant in (browser.rooms["main"], browser.rooms["brief"]):
        page = client.get(f"/aegis/rooms/{invariant['id']}")
        assert f'action="/aegis/rooms/{invariant["id"]}/archive"' not in page.text
        refused = client.post(
            f"/aegis/rooms/{invariant['id']}/archive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert refused.status_code == 403
        _assert_private(refused)

    brief_page = client.get(f"/aegis/rooms/{browser.rooms['brief']['id']}")
    assert "Read-only live brief" in brief_page.text
    assert (
        f'action="/aegis/rooms/{browser.rooms["brief"]["id"]}/events"'
        not in brief_page.text
    )
    brief_post = client.post(
        f"/aegis/rooms/{browser.rooms['brief']['id']}/events",
        data={
            "event_kind": "message",
            "body": "briefs are projections",
            "csrf_token": token,
        },
    )
    assert brief_post.status_code == 403

    archived = client.post(
        f"/aegis/rooms/{work_room['id']}/archive",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert archived.status_code == 303
    _assert_private(archived)

    archived_page = client.get(f"/aegis/rooms/{work_room['id']}")
    assert archived_page.status_code == 200
    assert "Archived room." in archived_page.text
    assert f'action="/aegis/rooms/{work_room["id"]}/events"' not in archived_page.text
    _assert_private(archived_page)

    archived_post = client.post(
        f"/aegis/rooms/{work_room['id']}/events",
        data={
            "event_kind": "message",
            "body": "cannot append",
            "csrf_token": token,
        },
    )
    assert archived_post.status_code == 409


def test_detached_work_item_room_is_historical_and_rejects_posts(
    browser: RoomsBrowser,
):
    client = browser.client
    room_id = browser.rooms["work"]["id"]
    conn = db.connect(browser.db_file)
    try:
        actor = users.get_user(conn, browser.users["owner"]["id"])
        assert actor is not None
        moved = issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=browser.issue["id"],
            project_id=None,
        )
        assert moved["project_id"] is None
        degraded = rooms.get_room(conn, room_id)
        assert degraded is not None
        assert degraded["is_detached"] is True
        assert degraded["link_state"] == "linked_work_moved"
    finally:
        conn.close()

    page = client.get(f"/aegis/rooms/{room_id}")
    assert page.status_code == 200
    assert "Historical linked room." in page.text
    assert "read-only until re-scoped" in page.text
    assert f'action="/aegis/rooms/{room_id}/events"' not in page.text
    _assert_private(page)

    token = client.cookies.get("athena_csrf")
    refused = client.post(
        f"/aegis/rooms/{room_id}/events",
        data={
            "event_kind": "message",
            "body": "stale linked work",
            "csrf_token": token,
        },
    )
    assert refused.status_code == 409
    assert "Historical linked room." in refused.text
    _assert_private(refused)


def test_run_navigation_uses_projected_receipt_and_raw_ids_stay_inert(
    browser: RoomsBrowser,
):
    client = browser.client
    issue_id = browser.issue["id"]
    room_id = browser.rooms["work"]["id"]
    run_id = "room web/run"

    domain_event = client.post(
        f"/issues/{issue_id}/comments",
        json={"body": "work performed under a tagged run"},
        headers=_actor_headers(
            browser.users["owner"],
            **{"X-Athena-Run": run_id},
        ),
    )
    assert domain_event.status_code == 201, domain_event.text

    minted = client.post(
        "/tokens",
        json={"name": "room web run", "scopes": ["rooms:write"]},
        headers=_actor_headers(browser.users["owner"]),
    )
    assert minted.status_code == 201, minted.text
    run_event = client.post(
        f"/rooms/{room_id}/events",
        json={
            "event_kind": "evidence",
            "body": "room-native evidence under a tagged run",
        },
        headers={
            "Authorization": f"Bearer {minted.json()['token']}",
            "X-Athena-Run": run_id,
        },
    )
    assert run_event.status_code == 201, run_event.text

    token = client.cookies.get("athena_csrf")
    linked = client.post(
        f"/aegis/rooms/{room_id}/events",
        data={
            "event_kind": "evidence",
            "body": "Follow the supplied run receipt",
            "reference_kind": "run",
            "reference_id": run_id,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert linked.status_code == 303

    conn = db.connect(browser.db_file)
    try:
        actor = users.get_user(conn, browser.users["owner"]["id"])
        assert actor is not None
        timeline = room_timeline.list_timeline(conn, room_id, actor=actor)
        assert timeline is not None
        reference = next(
            item["reference"]
            for item in timeline["items"]
            if item["body"] == "Follow the supplied run receipt"
        )
        assert reference is not None
        assert reference["available"] is True
        receipt = reference["receipt"]
        assert receipt
    finally:
        conn.close()

    detail = client.get(f"/aegis/rooms/{room_id}")
    assert f'href="{receipt}"' in detail.text
    assert re.search(
        rf"room-native evidence under a tagged run.*?· run\s*"
        rf'<a href="{re.escape(receipt)}"><code>{re.escape(run_id)}</code></a>',
        detail.text,
        re.DOTALL,
    )
    assert f"<code>{run_id}</code>" in detail.text
    assert f'href="/activity/runs/{run_id}"' not in detail.text
    assert "Follow the supplied run receipt" in detail.text
    _assert_private(detail)
