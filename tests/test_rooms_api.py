"""Strict REST transport coverage for Athena Rooms."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
import pytest

from athena.aegis import rooms
from athena.aegis.rooms_api import (
    BriefGroupOut,
    BriefItemOut,
    ContextBoundsOut,
    ContextRecordOut,
    ReceiptOut,
    RoomBriefOut,
    RoomContextIn,
    RoomContextOut,
    RoomDetailOut,
    RoomEventIn,
    RoomListPageOut,
    RoomOut,
    TimelineItemOut,
    TimelineOut,
    VisibleAgentGroupOut,
)
from athena.core import access, db
from athena.main import create_app


@dataclass
class RoomsApi:
    client: TestClient
    db_file: Path
    users: dict[str, dict]
    tokens: dict[str, dict]
    projects: dict[str, dict]
    issues: dict[str, dict]
    rooms: dict[str, dict]


def _actor(actor: dict) -> dict[str, str]:
    return {"X-Athena-Actor": str(actor["id"])}


def _bearer(token: dict, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token['token']}", **extra}


def _aliases(model: type[BaseModel]) -> set[str]:
    return {
        field.alias if field.alias is not None else name
        for name, field in model.model_fields.items()
    }


def _assert_private(response) -> None:
    assert response.headers["cache-control"] == "private, no-store"
    vary = {
        item.strip().casefold()
        for item in response.headers.get("vary", "").split(",")
        if item.strip()
    }
    assert {"authorization", "x-athena-actor"} <= vary


def _opaque_cursor(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _create_user(
    client: TestClient,
    *,
    email: str,
    name: str,
    admin: dict | None = None,
    role: str = "member",
    is_agent: bool = False,
) -> dict:
    response = client.post(
        "/users",
        json={
            "email": email,
            "name": name,
            "password": "pw",
            "role": role,
            "is_agent": is_agent,
        },
        headers=_actor(admin) if admin is not None else None,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mint(
    client: TestClient,
    actor: dict,
    *,
    name: str,
    scopes: list[str],
) -> dict:
    response = client.post(
        "/tokens",
        json={"name": name, "scopes": scopes},
        headers=_actor(actor),
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["scopes"] == scopes
    assert payload["token"].startswith("ath_")
    return payload


@pytest.fixture
def api(tmp_path: Path) -> RoomsApi:
    db_file = tmp_path / "rooms-api.db"
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
            admin=admin,
        )
        member = _create_user(
            client,
            email="member@example.com",
            name="Member",
            admin=admin,
        )
        outsider = _create_user(
            client,
            email="outsider@example.com",
            name="Outsider",
            admin=admin,
        )
        agent = _create_user(
            client,
            email="agent@example.com",
            name="Agent",
            admin=admin,
            is_agent=True,
        )
        second_agent = _create_user(
            client,
            email="agent-two@example.com",
            name="Agent Two",
            admin=admin,
            is_agent=True,
        )
        paused_agent = _create_user(
            client,
            email="paused@example.com",
            name="Paused Agent",
            admin=admin,
            is_agent=True,
        )

        token_rows = {
            "owner_rooms": _mint(
                client, owner, name="owner rooms", scopes=["rooms:write"]
            ),
            "owner_read": _mint(client, owner, name="owner read", scopes=["read"]),
            "owner_issue": _mint(
                client, owner, name="owner issue", scopes=["issue:write"]
            ),
            "member_read": _mint(client, member, name="member read", scopes=["read"]),
            "outsider_read": _mint(
                client, outsider, name="outsider read", scopes=["read"]
            ),
            "outsider_rooms": _mint(
                client, outsider, name="outsider rooms", scopes=["rooms:write"]
            ),
            "agent_rooms": _mint(
                client, agent, name="agent rooms", scopes=["rooms:write"]
            ),
            "paused_rooms": _mint(
                client,
                paused_agent,
                name="paused rooms",
                scopes=["rooms:write"],
            ),
            "revoked_rooms": _mint(
                client,
                agent,
                name="revoked rooms",
                scopes=["rooms:write"],
            ),
        }
        revoked = client.delete(
            f"/tokens/{token_rows['revoked_rooms']['id']}",
            headers=_actor(agent),
        )
        assert revoked.status_code == 204, revoked.text

        projects: dict[str, dict] = {}
        for name, key, label in (
            ("Rooms One", "ONE", "one"),
            ("Rooms Two", "TWO", "two"),
            ("Hidden Rooms", "HID", "hidden"),
        ):
            response = client.post(
                "/projects",
                json={"name": name, "key": key, "description": f"{name} purpose"},
                headers=_actor(owner),
            )
            assert response.status_code == 201, response.text
            projects[label] = response.json()
        hidden = client.put(
            f"/projects/{projects['hidden']['id']}/visibility",
            json={"visibility": "private"},
            headers=_actor(owner),
        )
        assert hidden.status_code == 200, hidden.text

        issues: dict[str, dict] = {}
        for label in ("one", "two"):
            response = client.post(
                "/issues",
                json={
                    "title": f"ORBIT issue {label}",
                    "body": f"ORBIT evidence in project {label}",
                    "project_id": projects[label]["id"],
                },
                headers=_actor(owner),
            )
            assert response.status_code == 201, response.text
            issues[label] = response.json()

        conn = db.connect(db_file)
        try:
            access.add_project_member(
                conn,
                projects["one"]["id"],
                member["id"],
                added_by=owner["id"],
            )
        finally:
            conn.close()

        member_room_response = client.post(
            f"/projects/{projects['one']['id']}/rooms",
            json={
                "room_type": "agent",
                "title": "Members only agent room",
                "purpose": "Narrower than public project visibility",
                "visibility": "members",
                "slug": "members-agent",
                "agent_id": agent["id"],
            },
            headers=_bearer(token_rows["owner_rooms"]),
        )
        assert member_room_response.status_code == 201, member_room_response.text
        member_room = member_room_response.json()

        conn = db.connect(db_file)
        try:
            one_rooms = rooms.list_rooms(
                conn,
                projects["one"]["id"],
                include_archived=True,
            )
            two_rooms = rooms.list_rooms(
                conn,
                projects["two"]["id"],
                include_archived=True,
            )
            hidden_rooms = rooms.list_rooms(
                conn,
                projects["hidden"]["id"],
                include_archived=True,
            )
            room_rows = {
                "one_main": next(
                    room for room in one_rooms if room["room_type"] == "project"
                ),
                "one_brief": next(
                    room for room in one_rooms if room["room_type"] == "brief"
                ),
                "one_work": rooms.get_work_item_room(conn, issues["one"]["id"]),
                "two_main": next(
                    room for room in two_rooms if room["room_type"] == "project"
                ),
                "two_work": rooms.get_work_item_room(conn, issues["two"]["id"]),
                "hidden_main": next(
                    room for room in hidden_rooms if room["room_type"] == "project"
                ),
                "members": member_room,
            }
            assert all(room_rows.values())
        finally:
            conn.close()

        yield RoomsApi(
            client=client,
            db_file=db_file,
            users={
                "admin": admin,
                "owner": owner,
                "member": member,
                "outsider": outsider,
                "agent": agent,
                "second_agent": second_agent,
                "paused_agent": paused_agent,
            },
            tokens=token_rows,
            projects=projects,
            issues=issues,
            rooms=room_rows,
        )


def test_strict_room_list_detail_create_and_archive_schemas(api: RoomsApi):
    client = api.client
    auth = _bearer(api.tokens["owner_rooms"])
    project_id = api.projects["one"]["id"]

    listed_response = client.get(
        f"/projects/{project_id}/rooms",
        headers=auth,
    )
    assert listed_response.status_code == 200
    listed = listed_response.json()
    assert set(listed) == _aliases(RoomListPageOut)
    assert listed["items"]
    assert all(set(item) == _aliases(RoomOut) for item in listed["items"])
    RoomListPageOut.model_validate(listed)
    with pytest.raises(ValidationError):
        RoomListPageOut.model_validate({**listed, "unexpected": True})
    _assert_private(listed_response)

    detail_response = client.get(
        f"/rooms/{api.rooms['one_work']['id']}",
        headers=_bearer(api.tokens["owner_read"]),
    )
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert set(detail) == _aliases(RoomDetailOut)
    assert set(detail["room"]) == _aliases(RoomOut)
    assert set(detail["visible_agents"]) == _aliases(VisibleAgentGroupOut)
    RoomDetailOut.model_validate(detail)
    with pytest.raises(ValidationError):
        RoomDetailOut.model_validate({**detail, "unexpected": "forbidden"})
    _assert_private(detail_response)

    create_body = {
        "room_type": "agent",
        "title": "Second agent room",
        "purpose": "Created through strict REST",
        "visibility": "members",
        "slug": "second-agent",
        "agent_id": api.users["second_agent"]["id"],
    }
    extra = client.post(
        f"/projects/{project_id}/rooms",
        json={**create_body, "actor_id": api.users["admin"]["id"]},
        headers=auth,
    )
    assert extra.status_code == 422

    wrong_type = client.post(
        f"/projects/{project_id}/rooms",
        json={**create_body, "agent_id": str(api.users["second_agent"]["id"])},
        headers=auth,
    )
    assert wrong_type.status_code == 422

    created_response = client.post(
        f"/projects/{project_id}/rooms",
        json=create_body,
        headers=auth,
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    assert set(created) == _aliases(RoomOut)
    assert created["created_by"] == api.users["owner"]["id"]
    assert created["visibility"] == "members"
    _assert_private(created_response)

    archived_response = client.post(
        f"/rooms/{created['id']}/archive",
        headers=auth,
    )
    assert archived_response.status_code == 200
    archived = archived_response.json()
    assert set(archived) == _aliases(RoomOut)
    assert archived["archived"] is True
    assert archived["archived_at"] is not None
    _assert_private(archived_response)


def test_project_room_list_cursor_is_bound_to_scope_and_mode(api: RoomsApi):
    client = api.client
    auth = _bearer(api.tokens["owner_read"])
    project_id = api.projects["one"]["id"]

    first = client.get(
        f"/projects/{project_id}/rooms",
        params={"limit": 1},
        headers=auth,
    )
    assert first.status_code == 200
    first_page = first.json()
    assert first_page["has_more"] is True
    assert first_page["next_cursor"]
    assert len(first_page["items"]) == 1

    second = client.get(
        f"/projects/{project_id}/rooms",
        params={"limit": 1, "cursor": first_page["next_cursor"]},
        headers=auth,
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] != first_page["items"][0]["id"]

    malformed = client.get(
        f"/projects/{project_id}/rooms",
        params={"cursor": "not-a-canonical-cursor"},
        headers=auth,
    )
    assert malformed.status_code == 422
    assert malformed.json() == {"detail": "invalid room list cursor"}

    cross_project = client.get(
        f"/projects/{api.projects['two']['id']}/rooms",
        params={"cursor": first_page["next_cursor"]},
        headers=auth,
    )
    assert cross_project.status_code == 422

    cross_mode = client.get(
        f"/projects/{project_id}/rooms",
        params={
            "cursor": first_page["next_cursor"],
            "include_archived": True,
        },
        headers=auth,
    )
    assert cross_mode.status_code == 422
    for response in (first, second, malformed, cross_project, cross_mode):
        _assert_private(response)


def test_timeline_cursor_is_canonical_and_room_scoped(api: RoomsApi):
    client = api.client
    write_auth = _bearer(api.tokens["owner_rooms"])
    read_auth = _bearer(api.tokens["owner_read"])
    first_room = api.rooms["one_work"]["id"]
    second_room = api.rooms["two_work"]["id"]

    for room_id, prefix in ((first_room, "first"), (second_room, "second")):
        for index in range(3):
            response = client.post(
                f"/rooms/{room_id}/events",
                json={
                    "event_kind": "message",
                    "body": f"{prefix}-timeline-{index}",
                },
                headers=write_auth,
            )
            assert response.status_code == 201, response.text

    first = client.get(
        f"/rooms/{first_room}/timeline",
        params={"limit": 1},
        headers=read_auth,
    )
    assert first.status_code == 200
    page = first.json()
    assert set(page) == _aliases(TimelineOut)
    assert page["page"]["has_more"] is True
    assert page["page"]["next_cursor"]
    assert len(page["items"]) == 1

    continued = client.get(
        f"/rooms/{first_room}/timeline",
        params={"limit": 1, "cursor": page["page"]["next_cursor"]},
        headers=read_auth,
    )
    assert continued.status_code == 200
    assert continued.json()["items"][0]["activity_id"] < page["items"][0]["activity_id"]

    malformed = client.get(
        f"/rooms/{first_room}/timeline",
        params={"cursor": "bad-cursor"},
        headers=read_auth,
    )
    assert malformed.status_code == 422

    cross_room = client.get(
        f"/rooms/{second_room}/timeline",
        params={"cursor": page["page"]["next_cursor"]},
        headers=read_auth,
    )
    assert cross_room.status_code == 422
    for response in (first, continued, malformed, cross_room):
        _assert_private(response)


def test_room_api_rejects_ids_and_cursor_components_outside_sqlite_range(
    api: RoomsApi,
):
    client = api.client
    huge = rooms.MAX_SQLITE_ID + 1
    project_id = api.projects["one"]["id"]
    room_id = api.rooms["one_work"]["id"]
    read_auth = _bearer(api.tokens["owner_read"])
    write_auth = _bearer(api.tokens["owner_rooms"])

    path_responses = (
        client.get(f"/projects/{huge}/rooms", headers=read_auth),
        client.post(
            f"/projects/{huge}/rooms",
            json={"room_type": "project", "title": "overflow"},
            headers=write_auth,
        ),
        client.get(f"/rooms/{huge}", headers=read_auth),
        client.post(f"/rooms/{huge}/archive", headers=write_auth),
        client.get(f"/rooms/{huge}/timeline", headers=read_auth),
        client.post(
            f"/rooms/{huge}/events",
            json={"event_kind": "message", "body": "overflow"},
            headers=write_auth,
        ),
        client.post(
            f"/rooms/{huge}/context",
            json={"question": "overflow?"},
            headers=read_auth,
        ),
        client.get(f"/rooms/{huge}/brief", headers=read_auth),
    )
    for response in path_responses:
        assert response.status_code == 422
        _assert_private(response)

    body_responses = (
        client.post(
            f"/projects/{project_id}/rooms",
            json={
                "room_type": "work_item",
                "title": "overflow",
                "issue_id": huge,
            },
            headers=write_auth,
        ),
        client.post(
            f"/rooms/{room_id}/events",
            json={
                "event_kind": "message",
                "body": "overflow reference",
                "reference_kind": "issue",
                "reference_id": huge,
            },
            headers=write_auth,
        ),
        client.post(
            f"/rooms/{room_id}/events",
            json={
                "event_kind": "decision",
                "body": "overflow successor",
                "supersedes_event_id": huge,
            },
            headers=write_auth,
        ),
    )
    for response in body_responses:
        assert response.status_code == 422
        _assert_private(response)

    room_cursor = _opaque_cursor(f"athena.rooms-list.v1:{project_id}:0:{huge}")
    timeline_cursor = _opaque_cursor(f"athena.room-timeline.v1:{room_id}:{huge}")
    cursor_responses = (
        client.get(
            f"/projects/{project_id}/rooms",
            params={"cursor": room_cursor},
            headers=read_auth,
        ),
        client.get(
            f"/rooms/{room_id}/timeline",
            params={"cursor": timeline_cursor},
            headers=read_auth,
        ),
        client.get(
            f"/rooms/{room_id}/brief",
            params={"cursor": timeline_cursor},
            headers=read_auth,
        ),
    )
    for response in cursor_responses:
        assert response.status_code == 422
        _assert_private(response)


def test_hidden_missing_and_members_only_resources_are_indistinguishable(
    api: RoomsApi,
):
    client = api.client
    outsider_read = _bearer(api.tokens["outsider_read"])
    member_read = _bearer(api.tokens["member_read"])
    members_room = api.rooms["members"]["id"]
    missing_room = 999_999_999

    visible = client.get(f"/rooms/{members_room}", headers=member_read)
    assert visible.status_code == 200
    assert visible.json()["room"]["title"] == "Members only agent room"

    for method, suffix, kwargs in (
        ("get", "", {}),
        ("get", "/timeline", {}),
        ("post", "/context", {"json": {"question": "what changed?"}}),
        ("get", "/brief", {}),
    ):
        hidden = getattr(client, method)(
            f"/rooms/{members_room}{suffix}",
            headers=outsider_read,
            **kwargs,
        )
        missing = getattr(client, method)(
            f"/rooms/{missing_room}{suffix}",
            headers=outsider_read,
            **kwargs,
        )
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json() == {"detail": "no such room"}
        _assert_private(hidden)
        _assert_private(missing)

    hidden_project = client.get(
        f"/projects/{api.projects['hidden']['id']}/rooms",
        headers=outsider_read,
    )
    missing_project = client.get(
        "/projects/999999999/rooms",
        headers=outsider_read,
    )
    assert hidden_project.status_code == missing_project.status_code == 404
    assert (
        hidden_project.json() == missing_project.json() == {"detail": "no such project"}
    )

    outsider_write = _bearer(api.tokens["outsider_rooms"])
    hidden_write = client.post(
        f"/rooms/{members_room}/events",
        json={"event_kind": "message", "body": "must remain hidden"},
        headers=outsider_write,
    )
    missing_write = client.post(
        f"/rooms/{missing_room}/events",
        json={"event_kind": "message", "body": "must remain hidden"},
        headers=outsider_write,
    )
    assert hidden_write.status_code == missing_write.status_code == 404
    assert hidden_write.json() == missing_write.json() == {"detail": "no such room"}


def test_bearer_rooms_write_scope_revocation_and_pause_boundaries(api: RoomsApi):
    client = api.client
    room_id = api.rooms["one_work"]["id"]
    body = {"event_kind": "message", "body": "scope boundary probe"}

    readable = client.get(
        f"/rooms/{room_id}",
        headers=_bearer(api.tokens["owner_read"]),
    )
    assert readable.status_code == 200

    read_only = client.post(
        f"/rooms/{room_id}/events",
        json=body,
        headers=_bearer(api.tokens["owner_read"]),
    )
    issue_only = client.post(
        f"/rooms/{room_id}/events",
        json=body,
        headers=_bearer(api.tokens["owner_issue"]),
    )
    for response in (read_only, issue_only):
        assert response.status_code == 403
        assert response.json() == {"detail": "token scope required: rooms:write"}

    allowed = client.post(
        f"/rooms/{room_id}/events",
        json=body,
        headers=_bearer(api.tokens["owner_rooms"]),
    )
    assert allowed.status_code == 201
    assert allowed.json()["actor"]["id"] == api.users["owner"]["id"]

    revoked = client.post(
        f"/rooms/{room_id}/events",
        json=body,
        headers=_bearer(api.tokens["revoked_rooms"]),
    )
    assert revoked.status_code == 401

    pause = client.put(
        f"/users/{api.users['paused_agent']['id']}/paused",
        json={"paused": True},
        headers=_actor(api.users["admin"]),
    )
    assert pause.status_code == 200
    paused = client.post(
        f"/rooms/{room_id}/events",
        json={"event_kind": "check_in", "body": "cannot write while paused"},
        headers=_bearer(api.tokens["paused_rooms"]),
    )
    assert paused.status_code == 403
    assert paused.json() == {"detail": "account is paused"}


def test_event_idempotency_actor_run_attribution_and_extra_rejection(
    api: RoomsApi,
):
    client = api.client
    room_id = api.rooms["one_work"]["id"]
    run_id = "agent room/run with spaces"
    event = {
        "event_kind": "check_in",
        "body": "one idempotent agent check-in",
    }
    auth = _bearer(
        api.tokens["agent_rooms"],
        **{
            "Idempotency-Key": "rooms-event-one",
            "X-Athena-Run": run_id,
        },
    )

    extra = client.post(
        f"/rooms/{room_id}/events",
        json={**event, "dispatch": True},
        headers=_bearer(api.tokens["agent_rooms"]),
    )
    assert extra.status_code == 422
    with pytest.raises(ValidationError):
        RoomEventIn.model_validate({**event, "dispatch": True})

    first = client.post(f"/rooms/{room_id}/events", json=event, headers=auth)
    replay = client.post(f"/rooms/{room_id}/events", json=event, headers=auth)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    payload = first.json()
    assert set(payload) == _aliases(TimelineItemOut)
    assert payload["actor"] == {
        "id": api.users["agent"]["id"],
        "name": "Agent",
        "is_agent": True,
    }
    assert payload["run_id"] == run_id
    expected_receipt = f"/activity/runs/{quote(run_id, safe='')}/lineage"
    assert payload["run_receipt"] == expected_receipt
    _assert_private(first)
    _assert_private(replay)

    mismatch = client.post(
        f"/rooms/{room_id}/events",
        json={**event, "body": "different body"},
        headers=auth,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "idempotency_mismatch"

    timeline_response = client.get(
        f"/rooms/{room_id}/timeline",
        headers=_bearer(api.tokens["agent_rooms"]),
    )
    assert timeline_response.status_code == 200
    timeline = timeline_response.json()
    recorded = next(
        item
        for item in timeline["items"]
        if item["body"] == "one idempotent agent check-in"
    )
    assert recorded["run_receipt"] == expected_receipt
    assert recorded["actor"]["id"] == api.users["agent"]["id"]

    conn = db.connect(api.db_file)
    try:
        rows = conn.execute(
            "SELECT a.actor_id, a.run_id, a.delivery_eligible "
            "FROM room_events re JOIN activity a ON a.id = re.activity_id "
            "WHERE re.room_id = ? AND a.detail = ?",
            (room_id, event["body"]),
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in rows] == [(api.users["agent"]["id"], run_id, 0)]


def test_context_and_brief_are_strict_bounded_source_packets(api: RoomsApi):
    client = api.client
    room_id = api.rooms["one_work"]["id"]

    seeded = client.post(
        f"/rooms/{room_id}/events",
        json={
            "event_kind": "evidence",
            "body": "ORBIT transport evidence",
        },
        headers=_bearer(api.tokens["owner_rooms"]),
    )
    assert seeded.status_code == 201

    extra = client.post(
        f"/rooms/{room_id}/context",
        json={"question": "ORBIT", "limit": 1, "unexpected": True},
        headers=_bearer(api.tokens["owner_read"]),
    )
    assert extra.status_code == 422
    strict_type = client.post(
        f"/rooms/{room_id}/context",
        json={"question": "ORBIT", "limit": "1"},
        headers=_bearer(api.tokens["owner_read"]),
    )
    assert strict_type.status_code == 422
    with pytest.raises(ValidationError):
        RoomContextIn.model_validate(
            {"question": "ORBIT", "limit": 1, "unexpected": True}
        )

    context_response = client.post(
        f"/rooms/{room_id}/context",
        json={"question": "Where is the ORBIT evidence?", "limit": 1},
        headers=_bearer(api.tokens["owner_read"]),
    )
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert set(context) == _aliases(RoomContextOut)
    assert context["schema"] == "athena.room-context.v1"
    assert set(context["bounds"]) == _aliases(ContextBoundsOut)
    assert context["bounds"]["selection_limit"] == 1
    assert context["bounds"]["selected_count"] <= 1
    assert len(context["records"]) <= 1
    for record in context["records"]:
        assert set(record) == _aliases(ContextRecordOut)
        assert set(record["receipt"]) == _aliases(ReceiptOut)
        assert record["receipt"]["method"] == "GET"
    RoomContextOut.model_validate(context)
    with pytest.raises(ValidationError):
        RoomContextOut.model_validate({**context, "provider_answer": "invented"})
    _assert_private(context_response)

    brief_response = client.get(
        f"/rooms/{api.rooms['one_main']['id']}/brief",
        headers=_bearer(api.tokens["owner_read"]),
    )
    assert brief_response.status_code == 200, brief_response.text
    brief = brief_response.json()
    assert set(brief) == _aliases(RoomBriefOut)
    assert brief["schema"] == "athena.room-brief.v1"
    for group_name in (
        "open_priority",
        "blockers",
        "agents",
        "decisions",
        "knowledge",
        "recent_timeline",
    ):
        group = brief[group_name]
        assert set(group) == _aliases(BriefGroupOut)
        assert len(group["items"]) <= group["visible_total"] or group["clipped"]
        for item in group["items"]:
            assert set(item) == _aliases(BriefItemOut)
    RoomBriefOut.model_validate(brief)
    with pytest.raises(ValidationError):
        RoomBriefOut.model_validate({**brief, "generated_summary": "invented"})
    _assert_private(brief_response)
