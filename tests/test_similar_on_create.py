"""Creating an issue returns up to three similar OPEN issues from the same project.

The list is a prompt to link instead of duplicating: it is additive, it never blocks the create,
and it is built from the existing search index, so it can only name issues the actor may see.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from athena.aegis import similar_issues
from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _client(tmp_path):
    client = TestClient(create_app(tmp_path / "similar.sqlite"))
    client.__enter__()
    client.post(
        "/users", json={"email": "a@e.com", "name": "A", "password": "pw"}, headers=H1
    )
    return client


def _project(client, name="Ops", key="OPS"):
    return client.post("/projects", json={"name": name, "key": key}, headers=H1).json()[
        "id"
    ]


def _create(client, title, project_id=None, headers=H1, **extra):
    response = client.post(
        "/issues",
        json={"title": title, "project_id": project_id, **extra},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _titles(created):
    return [row["title"] for row in created["similar_open"]]


def test_a_near_duplicate_title_is_listed_and_an_unrelated_one_is_not(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        dup = _create(client, "Login page crashes on submit", pid)
        _create(client, "Quarterly budget review", pid)
        _create(client, "Page layout refresh", pid)  # shares one word only

        created = _create(client, "Login page crash when submitting", pid)

        assert [row["id"] for row in created["similar_open"]] == [dup["id"]]
        row = created["similar_open"][0]
        assert set(row) == {"id", "key", "title", "status"}
        assert row["key"] == dup["key"] and row["status"] == dup["status"]
    finally:
        client.__exit__(None, None, None)


def test_no_similar_issue_gives_an_empty_list_and_the_create_still_succeeds(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _create(client, "Quarterly budget review", pid)
        created = _create(client, "Rotate the staging certificates", pid)
        assert created["similar_open"] == []
        assert client.get(f"/issues/{created['id']}", headers=H1).status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_a_similar_open_issue_never_blocks_the_create(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _create(client, "Login page crashes on submit", pid)
        again = _create(client, "Login page crashes on submit", pid)  # exact duplicate
        assert (
            again["similar_open"] and again["title"] == "Login page crashes on submit"
        )
        listed = client.get("/issues", params={"project": pid}, headers=H1).json()
        assert sum(i["title"] == "Login page crashes on submit" for i in listed) == 2
    finally:
        client.__exit__(None, None, None)


def test_a_closed_issue_is_never_listed_but_the_open_twin_is(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        closed = _create(client, "Login page crashes on submit", pid)
        open_ = _create(client, "Login page crashes on submit (still open)", pid)
        conn = db.connect(tmp_path / "similar.sqlite")
        try:
            conn.execute("UPDATE issues SET status='done' WHERE id=?", (closed["id"],))
            conn.commit()
        finally:
            conn.close()

        created = _create(client, "Login page crash when submitting", pid)

        ids = [row["id"] for row in created["similar_open"]]
        assert open_["id"] in ids  # positive control: matching still works
        assert closed["id"] not in ids
    finally:
        client.__exit__(None, None, None)


def test_an_archived_issue_is_never_listed(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        gone = _create(client, "Login page crashes on submit", pid)
        kept = _create(client, "Login page crashes on submit again", pid)
        conn = db.connect(tmp_path / "similar.sqlite")
        try:
            conn.execute(
                "UPDATE issues SET archived_at=datetime('now') WHERE id=?",
                (gone["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        ids = [
            row["id"]
            for row in _create(client, "Login page crash when submitting", pid)[
                "similar_open"
            ]
        ]
        assert kept["id"] in ids and gone["id"] not in ids
    finally:
        client.__exit__(None, None, None)


def test_only_the_same_project_is_searched(tmp_path):
    client = _client(tmp_path)
    try:
        ops = _project(client, "Ops", "OPS")
        web = _project(client, "Web", "WEB")
        elsewhere = _create(client, "Login page crashes on submit", web)
        backlog = _create(client, "Login page crashes on submit too")
        here = _create(client, "Login page crashes on submit here", ops)
        created = _create(client, "Login page crash when submitting", ops)
        ids = [row["id"] for row in created["similar_open"]]
        assert ids == [here["id"]]
        assert elsewhere["id"] not in ids and backlog["id"] not in ids
        # The backlog is its own scope: it finds only backlog issues.
        backlog_new = _create(client, "Login page crash when submitting")
        assert [row["id"] for row in backlog_new["similar_open"]] == [backlog["id"]]
    finally:
        client.__exit__(None, None, None)


def test_at_most_three_are_listed_and_the_new_issue_is_never_its_own_match(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        for n in range(5):
            _create(client, f"Login page crashes on submit variant {n}", pid)
        created = _create(client, "Login page crash when submitting", pid)
        assert len(created["similar_open"]) == 3
        assert created["id"] not in [row["id"] for row in created["similar_open"]]
    finally:
        client.__exit__(None, None, None)


def test_the_response_is_the_issue_representation_plus_only_similar_open(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _create(client, "Login page crashes on submit", pid)
        created = _create(client, "Login page crash when submitting", pid)
        fetched = client.get(f"/issues/{created['id']}", headers=H1).json()
        assert set(created) == set(fetched) | {"similar_open"}
        assert {k: v for k, v in created.items() if k != "similar_open"} == fetched
    finally:
        client.__exit__(None, None, None)


def test_a_single_significant_word_is_too_little_to_call_similar(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _create(client, "Crash", pid)
        assert _create(client, "Crash", pid)["similar_open"] == []
    finally:
        client.__exit__(None, None, None)


def test_mcp_create_issue_returns_the_same_field(tmp_path):
    client = _client(tmp_path)
    try:
        client.headers.update(H1)
        pid = _project(client)
        dup = _create(client, "Login page crashes on submit", pid)
        server = build_server(AthenaClient(client=client))
        result = asyncio.run(
            server.call_tool(
                "create_issue",
                {"title": "Login page crash when submitting", "project_id": pid},
            )
        )
        body = json.loads(result[0].text)
        assert [row["id"] for row in body["similar_open"]] == [dup["id"]]
        description = next(
            t.description
            for t in asyncio.run(server.list_tools())
            if t.name == "create_issue"
        )
        assert "similar_open" in description
    finally:
        client.__exit__(None, None, None)


def test_an_actor_who_cannot_see_the_project_gets_nothing(tmp_path):
    client = _client(tmp_path)
    try:
        client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw", "role": "member"},
            headers=H1,
        )
        pid = _project(client)
        mine = _create(client, "Login page crashes on submit", pid)
        conn = db.connect(tmp_path / "similar.sqlite")
        try:
            outsider = dict(conn.execute("SELECT * FROM users WHERE id = 2").fetchone())
            admin = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
            # Positive control: an actor who may see the project finds the issue.
            assert [
                row["id"]
                for row in similar_issues.similar_open(
                    conn,
                    actor=admin,
                    project_id=pid,
                    title="Login page crash when submitting",
                )
            ] == [mine["id"]]
            conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pid,))
            conn.commit()
            assert (
                similar_issues.similar_open(
                    conn,
                    actor=outsider,
                    project_id=pid,
                    title="Login page crash when submitting",
                )
                == []
            )
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("title", ["", "   ", "a an of", "!!! ???"])
def test_a_title_without_significant_words_lists_nothing(tmp_path, title):
    client = _client(tmp_path)
    try:
        conn = db.connect(tmp_path / "similar.sqlite")
        try:
            assert (
                similar_issues.similar_open(
                    conn, actor=None, project_id=None, title=title
                )
                == []
            )
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)
