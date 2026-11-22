"""An agent can learn a project's status names before and after a wrong one (A9).

Statuses are configured per project, so an agent cannot guess them. The
work-context packet lists the issue's project's statuses with their categories,
and a write that names a status the project lacks is a 422 whose body lists the
valid names in ``valid_statuses``. The list is given only once the project's
visibility is settled: a hidden project still reads as "no such project".
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server, tool_failure

ADMIN = {"X-Athena-Actor": "1"}
OTHER = {"X-Athena-Actor": "2"}
CUSTOM = ["open", "in_progress", "done", "in_review"]


@pytest.fixture
def world(tmp_path):
    client = TestClient(create_app(tmp_path / "statuses.sqlite"))
    client.__enter__()
    try:
        for email, name in (("a@example.com", "Ann"), ("b@example.com", "Bo")):
            created = client.post(
                "/users",
                json={"email": email, "name": name, "password": "pw"},
                headers=ADMIN,
            )
            assert created.status_code == 201, created.text
        project = client.post(
            "/projects", json={"key": "STA", "name": "Statuses"}, headers=ADMIN
        ).json()
        added = client.post(
            f"/projects/{project['id']}/statuses",
            json={"name": "in_review", "category": "doing"},
            headers=ADMIN,
        )
        assert added.status_code == 201, added.text
        issue = client.post(
            "/issues", json={"title": "t", "project_id": project["id"]}, headers=ADMIN
        ).json()
        yield client, project, issue
    finally:
        client.__exit__(None, None, None)


def test_the_work_context_lists_the_projects_statuses(world):
    client, _project, issue = world
    for brief in ("false", "true"):
        packet = client.get(
            f"/issues/{issue['id']}/work-context?brief={brief}", headers=ADMIN
        ).json()
        assert packet["issue"]["project_statuses"] == [
            {"name": "open", "category": "todo"},
            {"name": "in_progress", "category": "doing"},
            {"name": "done", "category": "done"},
            {"name": "in_review", "category": "doing"},
        ]
    backlog = client.post("/issues", json={"title": "b"}, headers=ADMIN).json()
    packet = client.get(f"/issues/{backlog['id']}/work-context", headers=ADMIN).json()
    assert [row["name"] for row in packet["issue"]["project_statuses"]] == [
        "open",
        "in_progress",
        "done",
    ]


def test_a_wrong_status_names_the_valid_ones(world):
    client, project, issue = world
    patched = client.patch(
        f"/issues/{issue['id']}", json={"status": "In Review"}, headers=ADMIN
    )
    assert patched.status_code == 422
    assert patched.json() == {
        "detail": "no such status for this project",
        "code": "validation_error",
        "valid_statuses": CUSTOM,
    }
    created = client.post(
        "/issues",
        json={"title": "t2", "project_id": project["id"], "status": "nope"},
        headers=ADMIN,
    )
    assert created.status_code == 422
    assert created.json()["valid_statuses"] == CUSTOM
    # One of the listed names is then accepted.
    assert (
        client.patch(
            f"/issues/{issue['id']}", json={"status": "in_review"}, headers=ADMIN
        ).status_code
        == 200
    )


def test_mcp_update_issue_carries_the_valid_names(world):
    client, _project, issue = world
    client.headers.update(ADMIN)
    server = build_server(AthenaClient(client=client))
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool(
                "update_issue", {"issue_id": issue["id"], "status": "bogus"}
            )
        )
    refusal = tool_failure(caught.value)
    assert refusal["status_code"] == 422
    assert refusal["valid_statuses"] == CUSTOM


def test_a_hidden_project_never_discloses_its_statuses(world):
    client, project, _issue = world
    private = client.put(
        f"/projects/{project['id']}/visibility",
        json={"visibility": "private"},
        headers=ADMIN,
    )
    assert private.status_code == 200, private.text
    refused = client.post(
        "/issues",
        json={"title": "probe", "project_id": project["id"], "status": "nope"},
        headers=OTHER,
    )
    assert refused.status_code == 422
    assert refused.json() == {"detail": "no such project", "code": "validation_error"}
