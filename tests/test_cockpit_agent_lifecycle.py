"""Three things an operator could not do in the browser, and one on no surface (U5).

WHY (COCKPIT-3, COCKPIT-4):

- **A new token for an existing agent.** Revoking an agent's tokens said "new
  tokens can be minted later", and nothing could: a token is self-service, and
  an agent whose tokens are gone cannot ask. The only way back was to onboard
  a second account. Now an admin mints one for the agent, over REST and in the
  agent's section of the cockpit, and the trail says the admin did.
- **Restoring a removed agent.** The cockpit removed an agent and told the
  operator to send ``POST /users/{id}/restore`` themselves.
- **Starting a playbook.** A playbook page could be started only over REST.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

from support import (
    ADMIN,
    add_user,
    as_user,
    bearer,
    bootstrap,
    events,
    login,
    make_app,
    onboard_agent,
)

from athena.core import agent_commands, db, tokens, users
from athena.workflows import playbook_commands


@pytest.fixture
def instance(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        yield app, client, sol, db_file


def _section(page: str, agent_id: int) -> tuple[str, str]:
    found = re.search(
        rf'(<details class="agent-row" id="agent-{agent_id}"[^>]*>)(.*?)</details>'
        r"\s*(?=<details class=\"agent-row\"|</div>)",
        page,
        re.DOTALL,
    )
    assert found, f"no section for agent {agent_id}"
    return found.group(1), found.group(2)


# --- a token for an existing agent: REST -----------------------------------------


def test_an_admin_mints_a_token_for_an_existing_agent(instance):
    app, client, sol, db_file = instance
    agent_id = sol["user"]["id"]
    # Its only token is gone, as after "Revoke all tokens".
    assert client.delete(f"/users/{agent_id}/tokens", headers=ADMIN).status_code == 200
    assert client.get("/desk", headers=bearer(sol)).status_code == 401

    minted = client.post(
        f"/users/{agent_id}/tokens",
        json={"scopes": ["read", "issue:write"], "name": "second laptop"},
        headers=ADMIN,
    )
    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["user"]["id"] == agent_id
    assert body["token"]["name"] == "second laptop"
    assert set(body["token"]["scopes"]) == {"read", "issue:write"}
    secret = body["token"]["token"]
    assert secret.startswith(tokens.TOKEN_PREFIX)
    # The same ready-to-paste config onboarding prints, for the new token.
    server = body["mcp_config"]["mcpServers"]["athena"]
    assert server["env"]["ATHENA_TOKEN"] == secret
    assert server["env"]["ATHENA_MCP_PROFILE"] == "seat"

    # It is the agent's credential: the agent's own desk answers to it.
    desk = client.get("/desk", headers={"Authorization": f"Bearer {secret}"})
    assert desk.status_code == 200, desk.text
    assert desk.json()["identity"]["id"] == agent_id

    # The trail says who minted it and for whom, and never the secret.
    (event,) = [
        row
        for row in events(db_file, "minted_token")
        if row["target_id"] == body["token"]["id"]
    ]
    assert event["actor_id"] == 1
    assert "second laptop" in event["detail"]
    assert "for agent sol@e.com" in event["detail"]
    assert all(secret not in (row["detail"] or "") for row in events(db_file))


def test_a_token_name_left_out_is_the_agents_name(instance):
    _, client, sol, _ = instance
    minted = client.post(
        f"/users/{sol['user']['id']}/tokens", json={"scopes": ["read"]}, headers=ADMIN
    )
    assert minted.status_code == 201, minted.text
    assert minted.json()["token"]["name"] == "Sol"


def test_only_an_admin_mints_and_only_for_a_present_agent(instance):
    app, client, sol, db_file = instance
    agent_id = sol["user"]["id"]
    person = add_user(client, "m@e.com", name="Mel")
    body = {"scopes": ["read"]}
    before = len(events(db_file, "minted_token"))

    def refused(response, status, code):
        assert response.status_code == status, response.text
        assert response.json()["code"] == code, response.text

    # A member, and the agent itself, may not hand out an agent's credential.
    refused(
        client.post(
            f"/users/{agent_id}/tokens", json=body, headers=as_user(person["id"])
        ),
        403,
        "forbidden",
    )
    refused(
        client.post(f"/users/{agent_id}/tokens", json=body, headers=bearer(sol)),
        403,
        "forbidden",
    )
    # A person mints their own tokens; this route is for agent accounts.
    refused(
        client.post(f"/users/{person['id']}/tokens", json=body, headers=ADMIN),
        422,
        "validation_error",
    )
    refused(
        client.post("/users/999/tokens", json=body, headers=ADMIN), 404, "not_found"
    )
    # What a token may do must be said, in words the server knows.
    for bad in ({}, {"scopes": []}, {"scopes": ["everything"]}):
        refused(
            client.post(f"/users/{agent_id}/tokens", json=bad, headers=ADMIN),
            422,
            "validation_error",
        )
    # A profile the scopes cannot fully use is refused before anything is made.
    refused(
        client.post(
            f"/users/{agent_id}/tokens",
            json={"scopes": ["read"], "profile": "conductor"},
            headers=ADMIN,
        ),
        422,
        "validation_error",
    )
    # A removed account reads as missing here as everywhere.
    assert client.post(f"/users/{agent_id}/remove", headers=ADMIN).status_code == 200
    refused(
        client.post(f"/users/{agent_id}/tokens", json=body, headers=ADMIN),
        404,
        "not_found",
    )
    assert len(events(db_file, "minted_token")) == before


def test_the_command_itself_refuses_an_actor_who_is_not_an_admin(instance):
    # Each transport checks first; the command is what a new transport calls.
    app, client, sol, db_file = instance
    member = add_user(client, "m@e.com", name="Mel")
    before = len(events(db_file, "minted_token"))
    conn = db.connect(db_file)
    try:
        for actor in (None, users.get_user(conn, member["id"]), sol["user"]):
            with pytest.raises(agent_commands.AgentCommandError) as refused:
                agent_commands.mint_agent_token(
                    conn,
                    actor=actor,
                    target_user_id=sol["user"]["id"],
                    scopes=["read"],
                )
            assert refused.value.kind in ("unauthorized", "forbidden")
    finally:
        conn.close()
    assert len(events(db_file, "minted_token")) == before


def test_a_route_that_answers_with_a_secret_takes_no_idempotency_key(instance):
    # A replayed answer would hand the secret out a second time, from storage.
    _, client, sol, db_file = instance
    before = len(events(db_file, "minted_token"))
    response = client.post(
        f"/users/{sol['user']['id']}/tokens",
        json={"scopes": ["read"]},
        headers={**ADMIN, "Idempotency-Key": "mint-once"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "idempotency_key_unsupported"
    assert len(events(db_file, "minted_token")) == before


# --- a token for an existing agent: the cockpit ----------------------------------


def test_the_cockpit_mints_a_token_in_the_agents_own_section(instance):
    app, client, sol, db_file = instance
    agent_id = sol["user"]["id"]
    login(client, "a@e.com")
    page = client.get("/admin/agents").text
    assert f'action="/admin/agents/{agent_id}/tokens"' in _section(page, agent_id)[1]

    minted = client.post(
        f"/admin/agents/{agent_id}/tokens",
        data={"token_name": "laptop", "scope_read": "on", "scope_issue_write": "on"},
    )
    assert minted.status_code == 201, minted.text
    opening, inside = _section(minted.text, agent_id)
    assert " open" in opening
    assert "New token for Sol" in inside
    (secret,) = set(re.findall(rf"{tokens.TOKEN_PREFIX}[A-Za-z0-9_-]+", minted.text))
    # The token on its own line to copy, and again inside the config.
    assert f'<pre class="token-secret">{secret}</pre>' in inside
    assert "ATHENA_MCP_PROFILE" in inside and inside.count(secret) == 2
    # Shown once: the page read again does not carry it.
    assert tokens.TOKEN_PREFIX not in client.get("/admin/agents").text
    assert (
        client.get("/desk", headers={"Authorization": f"Bearer {secret}"}).status_code
        == 200
    )
    (event,) = [
        row for row in events(db_file, "minted_token") if "laptop" in row["detail"]
    ]
    assert event["actor_id"] == 1 and "for agent sol@e.com" in event["detail"]


def test_a_refused_mint_says_why_in_the_agents_section(instance):
    app, client, sol, db_file = instance
    agent_id = sol["user"]["id"]
    login(client, "a@e.com")
    before = len(events(db_file, "minted_token"))
    refused = client.post(f"/admin/agents/{agent_id}/tokens", data={"token_name": "x"})
    assert refused.status_code == 400
    opening, inside = _section(refused.text, agent_id)
    assert " open" in opening
    assert '<div class="error">at least one token scope is required</div>' in inside
    assert len(events(db_file, "minted_token")) == before


def test_the_cockpit_mint_is_the_admins_alone(instance):
    app, client, sol, db_file = instance
    agent_id = sol["user"]["id"]
    add_user(client, "m@e.com", name="Mel")
    login(client, "m@e.com")
    before = len(events(db_file, "minted_token"))
    response = client.post(
        f"/admin/agents/{agent_id}/tokens", data={"scope_read": "on"}
    )
    assert response.status_code == 403
    assert tokens.TOKEN_PREFIX not in response.text
    assert len(events(db_file, "minted_token")) == before


# --- restoring a removed agent ---------------------------------------------------


def test_a_removed_agent_is_restored_from_the_page_that_removed_it(instance):
    app, client, sol, db_file = instance
    agent_id = sol["user"]["id"]
    login(client, "a@e.com")
    assert 'id="removed-agents"' not in client.get("/admin/agents").text

    removed = client.post(f"/admin/agents/{agent_id}/remove", follow_redirects=False)
    page = client.get(removed.headers["location"]).text
    # The outcome points at the control, not at a request to send by hand.
    assert "POST /users" not in page
    assert "Removed Sol:" in page and "Restore it under Removed agents" in page
    panel = page.split('id="removed-agents"', 1)[1].split("</section>", 1)[0]
    assert "Sol" in panel and "sol@e.com" in panel
    assert f'action="/admin/agents/{agent_id}/restore"' in panel

    restored = client.post(f"/admin/agents/{agent_id}/restore", follow_redirects=False)
    assert restored.status_code == 303
    parts = urlsplit(restored.headers["location"])
    assert parts.fragment == f"agent-{agent_id}"
    assert dict(parse_qsl(parts.query)) == {"restored": "1", "agent": str(agent_id)}
    page = client.get(restored.headers["location"]).text
    opening, inside = _section(page, agent_id)
    assert " open" in opening
    # It says what came back and what did not: the next two steps are here.
    assert "Restored Sol: back as a read-only viewer with no token." in inside
    assert f'action="/admin/agents/{agent_id}/tokens"' in inside
    assert 'id="removed-agents"' not in page
    assert [row["target_id"] for row in events(db_file, "restored_user")] == [agent_id]


def test_the_removed_panel_lists_agents_only(instance):
    # A person is removed over REST and restored there; the agents page is
    # about agents, and a person under "Removed agents" would be a wrong label.
    app, client, sol, db_file = instance
    person = add_user(client, "m@e.com", name="Mel")
    assert (
        client.post(f"/users/{person['id']}/remove", headers=ADMIN).status_code == 200
    )
    login(client, "a@e.com")
    assert 'id="removed-agents"' not in client.get("/admin/agents").text


def test_only_an_admin_restores(instance):
    app, client, sol, db_file = instance
    agent_id = sol["user"]["id"]
    assert client.post(f"/users/{agent_id}/remove", headers=ADMIN).status_code == 200
    add_user(client, "m@e.com", name="Mel")
    login(client, "m@e.com")
    # Not followed: a redirect back to the page would also end in a 403.
    refused = client.post(f"/admin/agents/{agent_id}/restore", follow_redirects=False)
    assert refused.status_code == 403
    assert events(db_file, "restored_user") == []


# --- starting a playbook ---------------------------------------------------------

CHECKLIST = """Before a release:

- [ ] Run the migration rehearsal
- [x] Tell the operator
- [ ] Verify the chain after restore
"""


def _issue_count(db_file) -> int:
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    finally:
        conn.close()


def _page(client, *, body=CHECKLIST, playbook=True) -> dict:
    space = client.post("/spaces", json={"key": "OPS", "name": "Ops"}, headers=ADMIN)
    assert space.status_code == 201, space.text
    page = client.post(
        f"/spaces/{space.json()['id']}/pages",
        json={"title": "Release", "body": body},
        headers=ADMIN,
    ).json()
    if playbook:
        label = client.post(
            "/labels", json={"name": playbook_commands.PLAYBOOK_LABEL}, headers=ADMIN
        ).json()
        attached = client.post(
            f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=ADMIN
        )
        assert attached.status_code in (200, 201), attached.text
    return page


def test_a_playbook_starts_from_its_page(instance):
    app, client, _, db_file = instance
    page = _page(client)
    project = client.post(
        "/projects", json={"name": "Operations", "key": "OPS"}, headers=ADMIN
    ).json()
    login(client, "a@e.com")
    shown = client.get(f"/mentor/pages/{page['id']}").text
    assert f'action="/mentor/pages/{page["id"]}/start-playbook"' in shown
    # It says what starting will make before it is pressed.
    assert "2 steps" in shown
    assert f'<option value="{project["id"]}">OPS — Operations</option>' in shown

    started = client.post(
        f"/mentor/pages/{page['id']}/start-playbook",
        data={"project_id": str(project["id"]), "title": "Release 4.2"},
        follow_redirects=False,
    )
    assert started.status_code == 303, started.text
    location = started.headers["location"]
    parent_id = int(re.fullmatch(r"/aegis/issues/(\d+)", location).group(1))
    parent = client.get(f"/issues/{parent_id}", headers=ADMIN).json()
    assert parent["title"] == "Release 4.2"
    assert parent["project_id"] == project["id"]
    children = client.get(f"/issues/{parent_id}/children", headers=ADMIN).json()
    assert sorted(child["title"] for child in children) == [
        "Run the migration rehearsal",
        "Verify the chain after restore",
    ]


def test_a_page_that_is_not_a_playbook_offers_nothing_to_start(instance):
    app, client, _, db_file = instance
    page = _page(client, playbook=False)
    login(client, "a@e.com")
    assert "start-playbook" not in client.get(f"/mentor/pages/{page['id']}").text
    before = _issue_count(db_file)
    refused = client.post(f"/mentor/pages/{page['id']}/start-playbook")
    # Refused in the page that posted it, with the command's own words.
    assert refused.status_code == 400
    assert "<h1>Release</h1>" in refused.text
    assert 'class="error"' in refused.text and "not a playbook" in refused.text
    assert _issue_count(db_file) == before


def test_a_playbook_with_nothing_left_to_do_says_so_and_starts_nothing(instance):
    app, client, _, db_file = instance
    page = _page(client, body="- [x] Done already\n")
    login(client, "a@e.com")
    shown = client.get(f"/mentor/pages/{page['id']}").text
    assert "no unchecked" in shown
    assert f'action="/mentor/pages/{page["id"]}/start-playbook"' not in shown


def test_a_playbook_too_long_to_start_says_so_instead_of_offering_it(instance):
    app, client, _, db_file = instance
    over = playbook_commands.MAX_ITEMS + 1
    page = _page(client, body="".join(f"- [ ] step {n}\n" for n in range(over)))
    login(client, "a@e.com")
    shown = client.get(f"/mentor/pages/{page['id']}").text
    assert f"{over} unchecked steps; at most {playbook_commands.MAX_ITEMS}" in shown
    assert f'action="/mentor/pages/{page["id"]}/start-playbook"' not in shown


def test_a_project_that_is_not_one_is_refused_in_the_page(instance):
    app, client, _, db_file = instance
    page = _page(client)
    login(client, "a@e.com")
    before = _issue_count(db_file)
    for project, says in (("soon", "Choose a project."), ("999", "project")):
        refused = client.post(
            f"/mentor/pages/{page['id']}/start-playbook", data={"project_id": project}
        )
        assert refused.status_code in (400, 404), refused.text[:200]
        assert "<h1>Release</h1>" in refused.text
        error = refused.text.split('<div class="error">', 1)[1].split("</div>", 1)[0]
        assert says in error
    assert _issue_count(db_file) == before


def test_a_viewer_cannot_start_a_playbook(instance):
    app, client, _, db_file = instance
    page = _page(client)
    viewer = add_user(client, "v@e.com", name="Vee")
    assert (
        client.put(
            f"/users/{viewer['id']}/role", json={"role": "viewer"}, headers=ADMIN
        ).status_code
        == 200
    )
    login(client, "v@e.com")
    assert "start-playbook" not in client.get(f"/mentor/pages/{page['id']}").text
    before = _issue_count(db_file)
    assert client.post(f"/mentor/pages/{page['id']}/start-playbook").status_code == 403
    assert _issue_count(db_file) == before
