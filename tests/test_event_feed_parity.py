"""X2: real MCP/client/REST parity, provenance and honest cursor boundaries."""

import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from athena.core import activity, db, run_context
from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError
from athena.mcp.server import build_server

ADMIN = {"X-Athena-Actor": "1"}
VISIBLE = ["root", "child", "human", "page", "imported", "blank", "native_forge"]


def _mcp(server, **arguments):
    result = asyncio.run(server.call_tool("recent_events", arguments))
    return json.loads(result[0].text)


def _record(conn, actor, kind, target, verb, *, run=None, parent=None, imported=None):
    token = run_context.set_run_id(run)
    parent_token = run_context.set_parent_run_id(parent)
    try:
        return activity.record(
            conn,
            actor_id=actor,
            verb=verb,
            target_kind=kind,
            target_id=target,
            imported_at=imported,
        )
    finally:
        run_context.reset_parent_run_id(parent_token)
        run_context.reset_run_id(token)


def _snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
        for table in ("activity", "agent_cursors", "notifications", "issue_leases")
    }


@pytest.fixture
def feed(tmp_path):
    database = tmp_path / "feed.db"
    with TestClient(create_app(database)) as client:
        admin = client.post(
            "/users",
            json={
                "email": "operator@example.test",
                "name": "Operator",
                "password": "fixture-only-password",
            },
        )
        assert admin.status_code == 201, admin.text
        agent = client.post(
            "/users/onboard_agent",
            json={"email": "reader@example.test", "name": "Reader", "scopes": ["read"]},
            headers=ADMIN,
        )
        assert agent.status_code == 201, agent.text
        agent_id = agent.json()["user"]["id"]
        issues = []
        for title in ("A", "B"):
            response = client.post("/issues", json={"title": title}, headers=ADMIN)
            assert response.status_code == 201, response.text
            issues.append(response.json()["id"])
        space = client.post(
            "/spaces", json={"name": "Public", "key": "PUB"}, headers=ADMIN
        )
        assert space.status_code == 201, space.text
        page = client.post(
            f"/spaces/{space.json()['id']}/pages", json={"title": "Page"}, headers=ADMIN
        )
        assert page.status_code == 201, page.text
        project = client.post(
            "/projects", json={"name": "Private", "key": "PRV"}, headers=ADMIN
        )
        assert project.status_code == 201, project.text
        hidden = client.post(
            "/issues",
            json={"title": "Hidden", "project_id": project.json()["id"]},
            headers=ADMIN,
        )
        assert hidden.status_code == 201, hidden.text
        private = client.put(
            f"/projects/{project.json()['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        assert private.status_code == 200, private.text
        conn = db.connect(database)
        baseline = conn.execute("SELECT max(id) FROM activity").fetchone()[0]
        # Literal cases: expected sets below do not use the retrieval predicate.
        records = {
            "root": _record(conn, agent_id, "issue", issues[0], "noted", run="root"),
            "child": _record(
                conn,
                agent_id,
                "issue",
                issues[0],
                "commented",
                run="child",
                parent="root",
            ),
            "human": _record(conn, 1, "issue", issues[1], "commented"),
            "page": _record(conn, 1, "page", page.json()["id"], "updated"),
            "imported": _record(
                conn,
                agent_id,
                "issue",
                issues[0],
                "commented",
                imported="2026-10-01 12:34:56",
            ),
            "blank": _record(
                conn, agent_id, "issue", issues[0], "forge_commit", imported=""
            ),
            "native_forge": _record(conn, agent_id, "issue", issues[1], "forge_commit"),
            "hidden": _record(conn, 1, "issue", hidden.json()["id"], "noted"),
        }
        client.headers.update(
            {"Authorization": "Bearer " + agent.json()["token"]["token"]}
        )
        api = AthenaClient(client=client)
        server = build_server(api, scopes={"read"})
        try:
            yield client, api, server, conn, baseline, records, agent_id, issues
        finally:
            conn.close()


@pytest.mark.parametrize(
    "query,expected",
    [
        ({}, VISIBLE),
        ({"kind": "page"}, ["page"]),
        ({"kind": "issue", "target": "A"}, ["root", "child", "imported", "blank"]),
        ({"actor_id": "agent"}, ["root", "child", "imported", "blank", "native_forge"]),
        (
            {"actor_type": "agent"},
            ["root", "child", "imported", "blank", "native_forge"],
        ),
        ({"actor_type": "human"}, ["human", "page"]),
        ({"run_id": "root"}, ["root"]),
        ({"run_id": "child"}, ["child"]),
        ({"parent_run_id": "root"}, ["child"]),
        ({"verb": "forge_commit"}, ["blank", "native_forge"]),
        (
            {
                "kind": "issue",
                "target": "A",
                "actor_id": "agent",
                "actor_type": "agent",
                "run_id": "child",
                "parent_run_id": "root",
                "verb": "commented",
            },
            ["child"],
        ),
        ({"kind": "issue", "target": "hidden"}, []),
        ({"after": "root"}, VISIBLE[1:]),
        ({"limit": 1}, ["root"]),
    ],
)
def test_each_filter_and_combination_has_real_chain_parity(feed, query, expected):
    client, api, server, conn, baseline, records, agent_id, issues = feed
    arguments = {"after": baseline, **query}
    if arguments.get("target") == "A":
        arguments["target"] = issues[0]
    if arguments.get("target") == "hidden":
        arguments["target"] = records["hidden"]["target_id"]
    if arguments.get("actor_id") == "agent":
        arguments["actor_id"] = agent_id
    if arguments.get("after") == "root":
        arguments["after"] = records["root"]["id"]
    before = _snapshot(conn)
    response = client.get("/events", params=arguments)
    assert response.status_code == 200, response.text
    rest = response.json()
    assert [row["id"] for row in rest["events"]] == [
        records[name]["id"] for name in expected
    ]
    assert api.recent_events(**arguments) == rest
    assert _mcp(server, **arguments) == rest
    assert set(rest) == {"events", "next_after", "has_more"}
    assert rest["has_more"] is (query == {"limit": 1})
    assert rest["next_after"] == (
        records[expected[-1]]["id"] if expected else arguments["after"]
    )
    assert before == _snapshot(conn)


def test_import_provenance_is_stored_value_not_verb_guess(feed):
    client, api, server, conn, baseline, records, _, _ = feed
    result = _mcp(server, after=baseline)
    by_id = {row["id"]: row for row in result["events"]}
    assert by_id[records["imported"]["id"]]["imported_at"] == "2026-10-01 12:34:56"
    assert by_id[records["blank"]["id"]]["imported_at"] == ""
    assert by_id[records["native_forge"]["id"]]["imported_at"] is None
    assert by_id[records["root"]["id"]]["imported_at"] is None


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": 201},
        {"limit": -1},
        {"actor_type": "robot"},
        {"target": 1},
    ],
)
def test_invalid_queries_are_refused_through_all_surfaces(feed, query):
    client, api, server, conn, _, _, _, _ = feed
    before = _snapshot(conn)
    assert client.get("/events", params=query).status_code == 422
    with pytest.raises(AthenaError) as client_error:
        api.recent_events(**query)
    assert client_error.value.status_code == 422
    with pytest.raises(ToolError):
        _mcp(server, **query)
    assert before == _snapshot(conn)


@pytest.mark.parametrize("value", [True, 1.5, "many"])
def test_mcp_limit_refuses_non_integer_values(feed, value):
    _, _, server, conn, _, _, _, _ = feed
    before = _snapshot(conn)
    with pytest.raises(ToolError):
        _mcp(server, limit=value)
    assert before == _snapshot(conn)


def test_filtered_gap_does_not_advance_cursor_or_hide_the_required_next_read(feed):
    _, _, server, conn, baseline, records, _, _ = feed
    before = _snapshot(conn)
    filtered = _mcp(server, after=baseline, actor_type="agent")
    omitted = records["human"]["id"]
    assert omitted not in [row["id"] for row in filtered["events"]]
    assert omitted < filtered["next_after"]
    # Safe recipe: return to the original unfiltered boundary and process the
    # full visible prefix. Never advance from a filtered next_after value.
    unfiltered = _mcp(server, after=baseline)
    assert [row["id"] for row in unfiltered["events"]] == [
        records[name]["id"] for name in VISIBLE
    ]
    assert before == _snapshot(conn)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    description = tools["recent_events"].description
    assert "filtered" in description and "contiguous" in description
    assert "Reading never advances" in description


def test_fixed_379_event_window_takes_eight_default_or_two_200_calls(feed):
    _, _, server, conn, _, _, agent_id, issues = feed
    baseline = conn.execute("SELECT max(id) FROM activity").fetchone()[0]
    expected = [
        _record(conn, agent_id, "issue", issues[0], "fixture_event")["id"]
        for _ in range(379)
    ]
    before = _snapshot(conn)

    def drain(limit=None):
        cursor, ids, sizes = baseline, [], []
        for _ in range(9):
            page = _mcp(
                server, after=cursor, **({} if limit is None else {"limit": limit})
            )
            sizes.append(len(page["events"]))
            ids.extend(row["id"] for row in page["events"])
            cursor = page["next_after"]
            if not page["has_more"]:
                return ids, sizes
        pytest.fail("fixture drain exceeded finite nine-call bound")

    default_ids, default_sizes = drain()
    large_ids, large_sizes = drain(200)
    assert default_sizes == [50, 50, 50, 50, 50, 50, 50, 29]
    assert large_sizes == [200, 179]
    assert default_ids == large_ids == expected
    assert len(set(default_ids)) == 379
    assert before == _snapshot(conn)
    print(
        "X2_FIXED_WINDOW="
        + json.dumps(
            {
                "baseline": baseline,
                "last": expected[-1],
                "count": 379,
                "default_page_sizes": default_sizes,
                "limit200_page_sizes": large_sizes,
                "ids_sha256": hashlib.sha256(json.dumps(expected).encode()).hexdigest(),
                "scope": "frozen disposable fixture only; no live backlog claim",
            },
            sort_keys=True,
        )
    )
