"""The agents page: one agent, one section, and an action returns to it (U10).

WHY (COCKPIT-10): every agent's whole detail was open at once, so the page
was 7,500 px tall with a handful of agents, and every action redirected to the
top of it with a message that named no agent ("Paused: every authenticated
action is refused until resumed."). The operator scrolled back to find which
agent they had acted on.

Now each agent is one disclosure with its own address (``#agent-<id>``), shut
until opened. An action made in a section redirects to that section, opens
it, and says what happened to which agent, there and not at the top.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

from support import bearer, bootstrap, login, make_app, onboard_agent


@pytest.fixture
def cockpit(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        onboarded = onboard_agent(client)
        sol = {**onboarded["user"], "bearer": bearer(onboarded)}
        nyx = onboard_agent(client, email="nyx@e.com", name="Nyx")["user"]
        login(client, "a@e.com")
        yield client, sol, nyx, db_file


def _section(page: str, agent_id: int) -> tuple[str, str]:
    """An agent's disclosure: its opening tag and everything inside it."""
    found = re.search(
        rf'(<details class="agent-row" id="agent-{agent_id}"[^>]*>)(.*?)</details>'
        r"\s*(?=<details class=\"agent-row\"|</div>)",
        page,
        re.DOTALL,
    )
    assert found, f"no section for agent {agent_id}"
    return found.group(1), found.group(2)


def _before_agents(page: str) -> str:
    return page.split('<div class="agent-admin-list">', 1)[0]


def _returns_to(response, agent_id: int) -> dict[str, str]:
    assert response.status_code == 303, response.text
    parts = urlsplit(response.headers["location"])
    assert parts.path == "/admin/agents"
    assert parts.fragment == f"agent-{agent_id}"
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    assert query["agent"] == str(agent_id)
    return query


def test_each_agent_is_one_shut_section_with_its_own_address(cockpit):
    client, sol, nyx, _ = cockpit
    page = client.get("/admin/agents").text
    assert page.count('<details class="agent-row"') == 2
    for agent in (sol, nyx):
        opening, inside = _section(page, agent["id"])
        assert " open" not in opening
        # The line that stays visible says who this is and its posture.
        summary = inside.split("</summary>", 1)[0]
        assert agent["name"] in summary and agent["email"] in summary
        assert "1 live token" in summary
        # Its controls are still in the page, inside its own section.
        assert f'action="/admin/agents/{agent["id"]}/pause"' in inside


def test_an_action_returns_to_its_agent_and_names_it_there(cockpit):
    client, sol, nyx, _ = cockpit
    response = client.post(f"/admin/agents/{nyx['id']}/pause", follow_redirects=False)
    query = _returns_to(response, nyx["id"])
    assert query["paused"] == "1"

    page = client.get(response.headers["location"]).text
    opening, inside = _section(page, nyx["id"])
    assert " open" in opening
    assert "Paused Nyx: every authenticated action is refused until resumed." in inside
    # Said once, where it was done: not at the top, not on another agent.
    assert "Paused Nyx" not in _before_agents(page)
    other_opening, other_inside = _section(page, sol["id"])
    assert " open" not in other_opening
    assert "Paused" not in other_inside.split("</summary>", 1)[1].split("<form", 1)[0]


ACTIONS = [
    ("pause", {}, "paused", "Paused Sol"),
    ("resume", {}, "resumed", "Resumed Sol"),
    ("budget", {"action_limit": "5", "window": "day"}, "budget_set", "Budget set"),
    ("budget/clear", {}, "budget_cleared", "Budget cleared"),
    (
        "approval-policy",
        {"action_kind": "issue.close", "gate": "on"},
        "gate_changed",
        "Approval policy updated for Sol",
    ),
    ("revoke-tokens", {}, "revoked", "Revoked 1 live token of Sol"),
    ("offboard", {}, "offboarded", "Offboarded Sol"),
]


@pytest.mark.parametrize(
    ("action", "form", "outcome", "says"), ACTIONS, ids=[a[0] for a in ACTIONS]
)
def test_every_action_on_an_agent_returns_to_that_agent(
    cockpit, action, form, outcome, says
):
    client, sol, _, _ = cockpit
    response = client.post(
        f"/admin/agents/{sol['id']}/{action}", data=form, follow_redirects=False
    )
    query = _returns_to(response, sol["id"])
    assert outcome in query, query
    page = client.get(response.headers["location"]).text
    opening, inside = _section(page, sol["id"])
    assert " open" in opening
    assert says in inside, inside[:600]
    assert "Sol" in inside.split('class="success"', 1)[1].split("</div>", 1)[0]


def test_a_refused_action_says_so_in_the_agents_section(cockpit):
    client, sol, _, _ = cockpit
    response = client.post(
        f"/admin/agents/{sol['id']}/budget",
        data={"action_limit": "many"},
        follow_redirects=False,
    )
    query = _returns_to(response, sol["id"])
    assert query["error"] == "Action limit must be a whole number."
    page = client.get(response.headers["location"]).text
    opening, inside = _section(page, sol["id"])
    assert " open" in opening
    assert '<div class="error">Action limit must be a whole number.</div>' in inside
    assert 'class="error"' not in _before_agents(page)


def test_removing_an_agent_names_it_at_the_top_as_it_has_no_section_left(cockpit):
    client, sol, nyx, _ = cockpit
    response = client.post(f"/admin/agents/{sol['id']}/remove", follow_redirects=False)
    assert response.status_code == 303
    parts = urlsplit(response.headers["location"])
    assert dict(parse_qsl(parts.query))["agent"] == str(sol["id"])
    assert parts.fragment == ""  # no section is left to scroll to
    page = client.get(response.headers["location"]).text
    assert f'id="agent-{sol["id"]}"' not in page
    assert "Removed Sol:" in _before_agents(page)
    assert " open" not in _section(page, nyx["id"])[0]


def test_a_worker_asked_to_stop_returns_to_its_agent(cockpit):
    client, sol, _, _ = cockpit
    # A worker registers by its agent's own heartbeat. A fresh client: this
    # one carries the operator's session.
    with TestClient(client.app) as as_agent:
        registered = as_agent.put(
            "/workers/heartbeat",
            json={"worker_key": "w-1", "node_label": "sol-laptop"},
            headers=sol["bearer"],
        )
    assert registered.status_code == 200, registered.text
    worker = registered.json()
    response = client.post(
        f"/admin/workers/{worker['id']}/kill", follow_redirects=False
    )
    query = _returns_to(response, sol["id"])
    assert "asked to stop" in query["notice"]
    page = client.get(response.headers["location"]).text
    opening, inside = _section(page, sol["id"])
    assert " open" in opening
    assert "sol-laptop (Sol) asked to stop" in inside


def test_an_agent_the_page_does_not_have_opens_nothing_and_breaks_nothing(cockpit):
    client, sol, nyx, _ = cockpit
    page = client.get("/admin/agents?paused=1&agent=999")
    assert page.status_code == 200
    assert "Paused: every authenticated action" in _before_agents(page.text)
    for agent in (sol, nyx):
        assert " open" not in _section(page.text, agent["id"])[0]
    # Not a number: refused as any malformed query is, not a server error.
    assert client.get("/admin/agents?agent=sol").status_code == 422
