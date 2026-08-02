"""Workspace search — one ask across issues, pages, and comments.

The contract these encode:

  * one call reaches all three kinds, with the caller's own visibility — it can
    never show more than the module surfaces would;
  * the WORK QUERY GRAMMAR works here, and whether a query is grammar-shaped is
    decided by the parser the issue list already uses, never a second one;
  * an unknown atom is an ERROR naming the atom — never an empty result set,
    which would teach an agent that the label does not exist;
  * a pure-grammar query has no free text, so the page and comment groups are
    empty and the response SAYS what was text-searched rather than letting the
    silence read as "no matches";
  * results are grouped by kind, not globally ranked — two engines with two
    orders cannot honestly be interleaved into one score;
  * every group discloses its bound: `clipped` is measured by fetching one row
    past the limit, not guessed.
"""

import asyncio

from fastapi.testclient import TestClient
import pytest

from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.workflows import workspace_search

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(tmp_path / "ws.db")) as c:
        c.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "secret"},
            headers=H1,
        )
        c.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)
        yield c


@pytest.fixture()
def seeded(client):
    """One issue, one page, and a comment on each — every one says 'zebra'."""
    project = client.post(
        "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
    ).json()
    issue = client.post(
        "/issues",
        json={
            "title": "Zebra migration",
            "body": "the zebra plan",
            "project_id": project["id"],
        },
        headers=H1,
    ).json()
    client.post(
        f"/issues/{issue['id']}/comments", json={"body": "zebra comment"}, headers=H1
    )
    space = client.post(
        "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
    ).json()
    page = client.post(
        f"/spaces/{space['id']}/pages",
        json={"title": "Zebra runbook", "body": "how to zebra"},
        headers=H1,
    ).json()
    client.post(
        f"/pages/{page['id']}/comments", json={"body": "zebra note"}, headers=H1
    )
    return {"project": project, "issue": issue, "space": space, "page": page}


def _search(client, q, headers=H1, **params):
    return client.get("/search/workspace", params={"q": q, **params}, headers=headers)


# --- one ask, three kinds ---------------------------------------------------


def test_plain_text_reaches_issues_pages_and_comments(client, seeded):
    body = _search(client, "zebra").json()
    assert body["schema"] == workspace_search.SCHEMA
    assert [i["title"] for i in body["issues"]["items"]] == ["Zebra migration"]
    assert [p["title"] for p in body["pages"]["items"]] == ["Zebra runbook"]
    # A comment has no address of its own: each hit names the parent it hangs off
    # and borrows its title, so a client can link somewhere real.
    parents = {(c["parent_kind"], c["parent_id"]) for c in body["comments"]["items"]}
    assert parents == {
        ("issue", seeded["issue"]["id"]),
        ("page", seeded["page"]["id"]),
    }
    # Issue hits came from full-text search, so they carry a snippet and context.
    hit = body["issues"]["items"][0]
    assert hit["snippet"] and hit["key"] == "ATH-1" and hit["status"]
    assert body["issues"]["source"] == "text"
    assert body["grouped_by_kind"] is True


def test_grammar_routes_issues_and_still_text_searches_the_docs(client, seeded):
    # WHY: the decision this stage had to make and pin. A mixed query sends the
    # atoms to the issue grammar and the BARE WORDS to full-text search, so
    # "is:open zebra" still finds the runbook. Feeding "is:open" to FTS would
    # match the literal token and produce confident nonsense.
    body = _search(client, "is:open zebra").json()
    assert body["issues"]["source"] == "query"
    assert [i["title"] for i in body["issues"]["items"]] == ["Zebra migration"]
    assert body["query"]["atoms"] == ["is:open"]
    assert body["query"]["text"] == "zebra"
    assert [p["title"] for p in body["pages"]["items"]] == ["Zebra runbook"]
    # The grammar path matched structurally; there is no text excerpt to show, and
    # an empty string would read like "matched nothing".
    assert body["issues"]["items"][0]["snippet"] is None


def test_grammar_actually_filters_rather_than_being_decoration(client, seeded):
    client.patch(
        f"/issues/{seeded['issue']['id']}", json={"status": "done"}, headers=H1
    )
    assert _search(client, "is:open zebra").json()["issues"]["items"] == []
    assert [
        i["title"] for i in _search(client, "is:closed zebra").json()["issues"]["items"]
    ] == ["Zebra migration"]


def test_pure_grammar_leaves_the_doc_groups_empty_and_says_why(client, seeded):
    body = _search(client, "is:open").json()
    assert [i["title"] for i in body["issues"]["items"]] == ["Zebra migration"]
    assert body["pages"]["items"] == [] and body["comments"]["items"] == []
    # The response states what went to full-text search, so empty doc groups read
    # as "nothing was searched", not as "nothing matched".
    assert body["query"]["text"] == ""
    assert body["query"]["atoms"] == ["is:open"]


def test_unknown_atom_is_an_error_naming_the_atom(client, seeded):
    # WHY: the contract that already exists for /issues, extended here. Returning
    # [] for 'labl:infra' would teach an agent the label does not exist.
    refused = _search(client, "labl:infra")
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["code"] == "invalid_query" and detail["atom"] == "labl"
    # A closed vocabulary refuses its own bad value the same way.
    bad_value = _search(client, "is:sideways")
    assert bad_value.status_code == 422
    assert bad_value.json()["detail"]["atom"] == "is:sideways"


def test_empty_query_is_refused_not_answered_with_everything(client, seeded):
    for blank in ("", "   ", '""'):
        assert _search(client, blank).status_code == 422


# --- bounds -----------------------------------------------------------------


def test_each_group_discloses_its_bound(client, seeded):
    space = seeded["space"]
    for n in range(4):
        client.post(
            f"/spaces/{space['id']}/pages",
            json={"title": f"Zebra {n}", "body": "zebra"},
            headers=H1,
        )
    body = _search(client, "zebra", limit_per_kind=2).json()
    assert body["pages"]["returned"] == 2
    assert body["pages"]["clipped"] is True  # measured: limit+1 was fetched
    assert body["limit_per_kind"] == 2
    # The un-clipped group says so rather than leaving the caller to infer it.
    assert body["issues"]["clipped"] is False


def test_limit_per_kind_bounds_are_enforced_at_both_layers(client, seeded):
    assert _search(client, "zebra", limit_per_kind=0).status_code == 422
    assert (
        _search(
            client, "zebra", limit_per_kind=workspace_search.MAX_LIMIT_PER_KIND + 1
        ).status_code
        == 422
    )
    # The command refuses on its own too, so a non-HTTP caller cannot slip past.
    with pytest.raises(workspace_search.WorkspaceSearchError) as caught:
        workspace_search.search_workspace(
            client.app.state.conn if hasattr(client.app.state, "conn") else None,
            actor={"id": 1, "role": "admin"},
            q="zebra",
            limit_per_kind=0,
        )
    assert caught.value.kind == "invalid"


# --- visibility -------------------------------------------------------------


def test_a_member_sees_neither_a_private_project_nor_a_private_space(client, seeded):
    # WHY: the composed read must not become a way around either module's gate.
    # Visibility is its own endpoint on both containers — not a PATCH field, which
    # is why a PATCH carrying it answers "no fields to update".
    assert (
        client.put(
            f"/projects/{seeded['project']['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/spaces/{seeded['space']['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        ).status_code
        == 200
    )
    mine = _search(client, "zebra", headers=H1).json()
    assert mine["issues"]["items"] and mine["pages"]["items"]  # the creator sees them

    theirs = _search(client, "zebra", headers=H2).json()
    assert theirs["issues"]["items"] == []
    assert theirs["pages"]["items"] == []
    assert theirs["comments"]["items"] == []
    # ...and the grammar path is gated too, not just full-text.
    assert _search(client, "is:open zebra", headers=H2).json()["issues"]["items"] == []


def test_anonymous_is_refused(client, seeded):
    assert client.get("/search/workspace", params={"q": "zebra"}).status_code == 401


# --- MCP --------------------------------------------------------------------


def test_mcp_tool_forwards_and_enforces_the_limit_bound():
    from mcp.server.fastmcp.exceptions import ToolError

    class RecordingAthenaClient:
        def __init__(self):
            self.calls: list[tuple] = []

        def search_workspace(self, query, *, limit_per_kind=None):
            self.calls.append((query, limit_per_kind))
            return {"schema": workspace_search.SCHEMA}

    recorder = RecordingAthenaClient()
    server = mcp_server.build_server(recorder)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["search_workspace"].inputSchema["properties"]["limit_per_kind"]
    bounds = {
        (option.get("minimum"), option.get("maximum"))
        for option in schema["anyOf"]
        if option.get("type") == "integer"
    }
    assert bounds == {
        (workspace_search.MIN_LIMIT_PER_KIND, workspace_search.MAX_LIMIT_PER_KIND)
    }
    # The description has to tell an agent the two things it cannot infer from a
    # schema: the grammar works here, and the groups are not cross-ranked.
    description = tools["search_workspace"].description.lower()
    assert "is:open" in description and "grouped by kind" in description

    asyncio.run(server.call_tool("search_workspace", {"query": "is:open zebra"}))
    assert recorder.calls == [("is:open zebra", None)]
    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "search_workspace",
                {
                    "query": "zebra",
                    "limit_per_kind": workspace_search.MAX_LIMIT_PER_KIND + 1,
                },
            )
        )
