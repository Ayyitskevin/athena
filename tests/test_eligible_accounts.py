"""A conductor can find the account it means to assign or delegate to (F9, D19).

WHY: a conductor on the ``conductor`` profile could not resolve an account id.
``delegate_issue`` told it to call ``list_users``, which the profile lacks and a
member is refused (``GET /users`` is admin-only), so the only way to learn an id
was to try writes: ``delegate_issue`` answered "no such user" for an id that
does not exist and "delegation target must be an agent" for a human, so a member
could enumerate accounts through a write.

The contract pinned here (decision D19):

- ``GET /issues/{id}/eligible-accounts`` (the ``list_eligible_accounts`` tool,
  ``conductor`` profile) lists, for a caller that can see one issue and holds a
  write role, the accounts that can see and work that issue: ``id``, ``name``
  and ``is_agent`` and nothing else.
- An issue the caller cannot see reads exactly like one that does not exist.
- The list asks the commands' own rule: every listed account is accepted by
  ``assign_issue``, every listed agent by ``delegate_issue``, and a listed human
  is refused delegation. The commands also take an account that cannot see the
  issue, which the list leaves out (it lists who can work the issue).
- A removed account is refused by every write that takes an account id, with
  the answer a missing id gets, and the list never shows it: removal hides an
  account from every list, picker and lookup, and a write no longer finds it.
- ``delegate_issue`` answers the same for an account that does not exist, was
  removed or is not an agent.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from support import (
    ADMIN,
    add_user,
    bootstrap,
    client_as,
    make_app,
    mcp_server_for,
    onboard_agent,
)

from athena.mcp import server as mcp_server

SEAT_SCOPES = ("read", "issue:write")
ROW_KEYS = {"id", "name", "is_agent"}


def _ok(response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _cast(admin: TestClient) -> dict[str, Any]:
    """The operator, a conductor agent, and one account of every kind the
    commands might treat differently."""
    cast: dict[str, Any] = {}
    cast["conductor"] = onboard_agent(
        admin, email="cora@e.com", name="Cora", scopes=SEAT_SCOPES
    )
    cast["worker"] = onboard_agent(
        admin, email="bo@e.com", name="Bo", scopes=SEAT_SCOPES
    )
    cast["human"] = add_user(admin, "hal@e.com", name="Hal")
    cast["viewer"] = add_user(admin, "vi@e.com", name="Vi", role="viewer")
    paused_agent = onboard_agent(
        admin, email="pat@e.com", name="Pat", scopes=SEAT_SCOPES
    )
    cast["paused_agent"] = paused_agent
    _ok(
        admin.put(
            f"/users/{paused_agent['user']['id']}/paused",
            json={"paused": True},
            headers=ADMIN,
        )
    )
    paused_human = add_user(admin, "pia@e.com", name="Pia")
    cast["paused_human"] = paused_human
    _ok(
        admin.put(
            f"/users/{paused_human['id']}/paused", json={"paused": True}, headers=ADMIN
        )
    )
    removed_agent = onboard_agent(
        admin, email="rem@e.com", name="Rem", scopes=SEAT_SCOPES
    )
    cast["removed_agent"] = removed_agent
    _ok(admin.post(f"/users/{removed_agent['user']['id']}/remove", headers=ADMIN))
    removed_human = add_user(admin, "rho@e.com", name="Rho")
    cast["removed_human"] = removed_human
    _ok(admin.post(f"/users/{removed_human['id']}/remove", headers=ADMIN))
    return cast


def _user_id(entry: dict[str, Any]) -> int:
    return entry["user"]["id"] if "user" in entry else entry["id"]


def _world(tmp_path):
    app, _ = make_app(tmp_path)
    admin = TestClient(app)
    admin.__enter__()
    bootstrap(admin)
    cast = _cast(admin)
    conductor = client_as(app, token=cast["conductor"]["token"])
    return app, admin, cast, conductor


def _issue(admin: TestClient, title: str, **fields) -> dict[str, Any]:
    return _ok(
        admin.post("/issues", json={"title": title, **fields}, headers=ADMIN), 201
    )


def _conducted_issue(admin, cast, title="slice", **fields) -> dict[str, Any]:
    """An issue the conductor may write: assigned to it."""
    made = _issue(admin, title, **fields)
    _ok(
        admin.put(
            f"/issues/{made['id']}/assignee",
            json={"assignee_id": _user_id(cast["conductor"])},
            headers=ADMIN,
        )
    )
    return made


# --- the read --------------------------------------------------------------------


def test_a_member_reads_id_name_and_is_agent_and_nothing_else(tmp_path):
    app, admin, cast, conductor = _world(tmp_path)
    try:
        issue = _conducted_issue(admin, cast)
        rows = _ok(conductor.get(f"/issues/{issue['id']}/eligible-accounts"))
        assert rows, "the eligible accounts are listed"
        for row in rows:
            # No email, role, token, pause or removal state: an admin read carries
            # those, and this read is for a member.
            assert set(row) == ROW_KEYS, row
        by_id = {row["id"]: row for row in rows}
        worker = _user_id(cast["worker"])
        human = _user_id(cast["human"])
        assert by_id[worker] == {"id": worker, "name": "Bo", "is_agent": True}
        assert by_id[human] == {"id": human, "name": "Hal", "is_agent": False}
        assert [row["id"] for row in rows] == sorted(by_id), "ordered by id"
    finally:
        admin.__exit__(None, None, None)


def test_a_hidden_issue_reads_exactly_like_a_missing_one(tmp_path):
    app, admin, cast, conductor = _world(tmp_path)
    try:
        project = _ok(
            admin.post(
                "/projects",
                json={"name": "Secret", "key": "SEC", "visibility": "private"},
                headers=ADMIN,
            ),
            201,
        )
        hidden = _issue(admin, "hidden", project_id=project["id"])
        # The admin sees it, so the issue exists and has eligible accounts.
        assert _ok(
            admin.get(f"/issues/{hidden['id']}/eligible-accounts", headers=ADMIN)
        )
        as_hidden = conductor.get(f"/issues/{hidden['id']}/eligible-accounts")
        as_missing = conductor.get("/issues/999999/eligible-accounts")
        assert as_hidden.status_code == as_missing.status_code == 404
        assert as_hidden.json() == as_missing.json()
        assert as_hidden.json()["code"] == "not_found"
    finally:
        admin.__exit__(None, None, None)


def test_the_read_needs_an_authenticated_member(tmp_path):
    app, admin, cast, conductor = _world(tmp_path)
    try:
        issue = _issue(admin, "open")
        anonymous = TestClient(app).get(f"/issues/{issue['id']}/eligible-accounts")
        assert anonymous.status_code == 401
        viewer = admin.get(
            f"/issues/{issue['id']}/eligible-accounts",
            headers={"X-Athena-Actor": str(_user_id(cast["viewer"]))},
        )
        assert viewer.status_code == 403 and viewer.json()["code"] == "forbidden"
    finally:
        admin.__exit__(None, None, None)


def test_every_listed_account_is_one_the_commands_accept(tmp_path):
    """Every account in the workspace, on an issue every account can see:
    listed exactly when assignment accepts it, and listed as an agent exactly
    when delegation accepts it. A removed account is neither."""
    app, admin, cast, conductor = _world(tmp_path)
    try:
        probe = _conducted_issue(admin, cast, "probe")
        listed = {
            row["id"]: row
            for row in _ok(conductor.get(f"/issues/{probe['id']}/eligible-accounts"))
        }
        everyone = _ok(admin.get("/users?include_removed=true", headers=ADMIN))
        assert len(everyone) == 9  # the admin and the eight in _cast
        for account in everyone:
            uid = account["id"]
            # Fresh issues, so one decision never depends on another.
            assigned = conductor.put(
                f"/issues/{_conducted_issue(admin, cast)['id']}/assignee",
                json={"assignee_id": uid},
            )
            delegated = conductor.post(
                f"/issues/{_conducted_issue(admin, cast)['id']}/delegate",
                json={"user_id": uid},
            )
            if account["removed_at"] is not None:
                assert uid not in listed
                assert assigned.status_code == 422, (account, assigned.text)
                assert delegated.status_code == 422, (account, delegated.text)
                continue
            assert (uid in listed) == (assigned.status_code == 200), (
                account,
                assigned.text,
            )
            delegable = uid in listed and listed[uid]["is_agent"]
            assert delegable == (delegated.status_code == 201), (
                account,
                delegated.text,
            )
        # Paused accounts are listed, with no pause state: the commands take them.
        assert _user_id(cast["paused_agent"]) in listed
        assert _user_id(cast["paused_human"]) in listed
    finally:
        admin.__exit__(None, None, None)


def test_on_a_private_issue_only_who_can_see_it_is_listed(tmp_path):
    """The list is who can see and work the issue: a non-member of a private
    project is not listed. Every listed account is still accepted by the
    commands (a subset), and the commands' own acceptance of a non-member is
    unchanged and pinned here."""
    app, admin, cast, conductor = _world(tmp_path)
    try:
        project = _ok(
            admin.post(
                "/projects",
                json={"name": "Secret", "key": "SEC", "visibility": "private"},
                headers=ADMIN,
            ),
            201,
        )
        for kind in ("conductor", "worker"):
            _ok(
                admin.post(
                    f"/projects/{project['id']}/members",
                    json={"user_id": _user_id(cast[kind])},
                    headers=ADMIN,
                ),
                201,
            )

        def private_issue():
            return _conducted_issue(admin, cast, project_id=project["id"])

        probe = private_issue()
        listed = {
            row["id"]: row
            for row in _ok(conductor.get(f"/issues/{probe['id']}/eligible-accounts"))
        }
        assert set(listed) == {1, _user_id(cast["conductor"]), _user_id(cast["worker"])}
        everyone = _ok(admin.get("/users?include_removed=true", headers=ADMIN))
        for account in everyone:
            uid = account["id"]
            assigned = conductor.put(
                f"/issues/{private_issue()['id']}/assignee", json={"assignee_id": uid}
            )
            delegated = conductor.post(
                f"/issues/{private_issue()['id']}/delegate", json={"user_id": uid}
            )
            if uid in listed:
                assert assigned.status_code == 200, (account, assigned.text)
                assert (delegated.status_code == 201) == listed[uid]["is_agent"]
        # A non-member: not listed, and (unchanged) the command still takes it.
        human = _user_id(cast["human"])
        assert human not in listed
        taken = conductor.put(
            f"/issues/{private_issue()['id']}/assignee", json={"assignee_id": human}
        )
        assert taken.status_code == 200
    finally:
        admin.__exit__(None, None, None)


def test_the_list_pages(tmp_path):
    app, admin, cast, conductor = _world(tmp_path)
    try:
        issue = _issue(admin, "paged")
        url = f"/issues/{issue['id']}/eligible-accounts"
        whole = _ok(conductor.get(url))
        first = _ok(conductor.get(url, params={"limit": 2}))
        rest = _ok(conductor.get(url, params={"limit": 100, "offset": 2}))
        assert first == whole[:2] and first + rest == whole
        assert conductor.get(url, params={"limit": 0}).status_code == 422
        assert conductor.get(url, params={"limit": 101}).status_code == 422
    finally:
        admin.__exit__(None, None, None)


# --- the write answers ---------------------------------------------------------------


def test_delegation_answers_alike_for_missing_removed_and_ineligible(tmp_path):
    app, admin, cast, conductor = _world(tmp_path)
    try:
        issue = _conducted_issue(admin, cast)
        url = f"/issues/{issue['id']}/delegate"
        missing = conductor.post(url, json={"user_id": 999_999})
        human = conductor.post(url, json={"user_id": _user_id(cast["human"])})
        removed = conductor.post(url, json={"user_id": _user_id(cast["removed_agent"])})
        assert missing.status_code == human.status_code == removed.status_code == 422
        assert missing.json() == human.json() == removed.json(), (
            missing.json(),
            human.json(),
            removed.json(),
        )
        assert missing.json()["code"] == "validation_error"
        # Nothing was added by either.
        assert _ok(conductor.get(f"/issues/{issue['id']}/contributors")) == []
    finally:
        admin.__exit__(None, None, None)


def test_a_removed_account_answers_as_a_missing_one_on_every_write(tmp_path):
    """Removal hides an account from every list, picker and lookup; a write that
    takes an account id must not find it either. Assignment, delegation, a
    contributor add and a composed create answer a removed id exactly as a
    missing one, and nothing is written."""
    app, admin, cast, conductor = _world(tmp_path)
    try:
        for kind in ("removed_agent", "removed_human"):
            gone = _user_id(cast[kind])
            issue = _conducted_issue(admin, cast)
            calls = {
                "assign": lambda uid: conductor.put(
                    f"/issues/{issue['id']}/assignee", json={"assignee_id": uid}
                ),
                "delegate": lambda uid: conductor.post(
                    f"/issues/{issue['id']}/delegate", json={"user_id": uid}
                ),
                "contributor": lambda uid: conductor.post(
                    f"/issues/{issue['id']}/contributors", json={"user_id": uid}
                ),
                "create assignee": lambda uid: conductor.post(
                    "/issues", json={"title": "x", "assignee_id": uid}
                ),
                "create delegate": lambda uid: conductor.post(
                    "/issues", json={"title": "x", "delegate_ids": [uid]}
                ),
            }
            for name, call in calls.items():
                removed, missing = call(gone), call(999_999)
                assert removed.status_code == missing.status_code == 422, (
                    kind,
                    name,
                    removed.text,
                )
                assert removed.json() == missing.json(), (kind, name)
                # The account refusal itself, not a body-shape refusal.
                assert removed.json()["detail"] in (
                    "no such user",
                    "no agent account with that id",
                ), (kind, name, removed.json())
            after = _ok(conductor.get(f"/issues/{issue['id']}"))
            assert after["assignee_id"] == _user_id(cast["conductor"])
            assert _ok(conductor.get(f"/issues/{issue['id']}/contributors")) == []
    finally:
        admin.__exit__(None, None, None)


def test_assignment_has_no_ineligible_class_to_leak(tmp_path):
    """Assignment accepts every account that exists and was not removed, so its
    only refusal is the missing (or removed) one: no second answer to compare."""
    app, admin, cast, conductor = _world(tmp_path)
    try:
        issue = _conducted_issue(admin, cast)
        missing = conductor.put(
            f"/issues/{issue['id']}/assignee", json={"assignee_id": 999_999}
        )
        assert missing.status_code == 422
        assert missing.json()["code"] == "validation_error"
        for kind in ("human", "worker", "viewer", "paused_agent"):
            other = _conducted_issue(admin, cast)
            assert (
                conductor.put(
                    f"/issues/{other['id']}/assignee",
                    json={"assignee_id": _user_id(cast[kind])},
                ).status_code
                == 200
            ), kind
    finally:
        admin.__exit__(None, None, None)


# --- the tool ------------------------------------------------------------------------


def _call(server, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    async def run():
        async with create_connected_server_and_client_session(
            server._mcp_server  # noqa: SLF001 — the protocol server under test
        ) as session:
            return await session.call_tool(name, arguments)

    result = asyncio.run(run())
    return result.isError, json.loads(result.content[0].text)


def test_the_conductor_profile_carries_the_read_and_a_seat_does_not():
    assert "list_eligible_accounts" in mcp_server.PROFILES["conductor"]
    assert "list_eligible_accounts" not in mcp_server.PROFILES["seat"]
    assert "list_eligible_accounts" not in mcp_server.PROFILES["seat+knowledge"]


def test_the_tool_answers_what_the_route_answers(tmp_path):
    app, admin, cast, conductor = _world(tmp_path)
    try:
        issue = _conducted_issue(admin, cast)
        server = mcp_server_for(
            app, cast["conductor"], run_id="cora-run", profile="conductor"
        )
        error, answer = _call(
            server, "list_eligible_accounts", {"issue_id": issue["id"]}
        )
        assert not error, answer
        rest = _ok(conductor.get(f"/issues/{issue['id']}/eligible-accounts"))
        assert answer["items"] == rest
        assert answer["truncated"] is False and answer["next"] is None
        error, page = _call(
            server, "list_eligible_accounts", {"issue_id": issue["id"], "limit": 2}
        )
        assert not error and page["items"] == rest[:2]
        assert page["truncated"] is True and page["next"] == {"offset": 2}
        error, hidden = _call(server, "list_eligible_accounts", {"issue_id": 999_999})
        assert error and hidden["code"] == "not_found"
    finally:
        admin.__exit__(None, None, None)


def test_delegate_issue_points_at_a_read_its_profile_carries():
    server = mcp_server.build_server(
        object(), scopes=None, profile=mcp_server.PROFILES["conductor"]
    )
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    text = tools["delegate_issue"].description or ""
    assert "list_users" not in text
    assert "list_eligible_accounts" in text
