"""The three lists that returned everything page in D2's vocabulary (H6).

WHY: ``GET /spaces/{id}/pages`` answered every page with its body (2.65 MB at
1,250 pages), and the MCP tool ``list_pages`` fetched that whole list again for
every continuation (31 calls, 82 MB to list 1,250 pages). ``GET
/issues/{id}/comments`` had no limit at all, ``GET /issues`` carried full bodies
behind an offset that repeats a row whenever one is inserted ahead of it, and no
route read one comment, so a comment longer than a list row could hold was
unreadable whole.

The contract pinned here, for the page list, an issue's comment list and the
issue list:

- each is bounded: a documented default ``limit`` and a maximum it refuses past;
- each answers ``next_cursor`` and ``has_more`` (decision D2), and following
  ``next_cursor`` to the end returns every row exactly once, in the list's
  order, even when rows are added between two pages;
- the page list carries no body (``GET /pages/{id}`` has it);
- ``GET /issues/{issue_id}/comments/{comment_id}`` and the MCP tool
  ``get_issue_comment`` read one comment whole;
- a hidden space, project or issue reads as missing (404) on every one of them;
- the MCP tools continue with the server's cursor: listing N rows costs about
  N/limit calls, and each row crosses the wire once.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    OUTSIDER,
    bearer,
    bootstrap_admin_creator_outsider,
    make_app,
    mcp_sessions,
    onboard_agent,
    run_agents,
)

from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

# The documented bounds (docs/AGENT_GUIDE.md, "Conventions every agent packet keeps").
PAGES_DEFAULT, PAGES_MAX = 100, 200
COMMENTS_DEFAULT, COMMENTS_MAX = 50, 200
ISSUES_DEFAULT, ISSUES_MAX = 50, 100


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text[:600]
    return response.json()


def _follow(
    client: TestClient, path: str, key: str, **params: Any
) -> tuple[list[dict], int]:
    """Every row of a D2 list, following next_cursor; and how many pages."""
    rows: list[dict] = []
    calls = 0
    cursor = None
    while True:
        answer = _ok(
            client.get(
                path, params={**params, **({"cursor": cursor} if cursor else {})}
            )
        )
        calls += 1
        assert set(answer) == {key, "next_cursor", "has_more"}, sorted(answer)
        rows += answer[key]
        assert answer["has_more"] is (answer["next_cursor"] is not None)
        if not answer["has_more"]:
            return rows, calls
        cursor = answer["next_cursor"]
        assert calls < 1_000


def _space(client: TestClient, key: str = "DOC") -> dict:
    return _ok(client.post("/spaces", json={"key": key, "name": key}), 201)


def _page(client: TestClient, space_id: int, title: str, body: str = "b") -> dict:
    return _ok(
        client.post(f"/spaces/{space_id}/pages", json={"title": title, "body": body}),
        201,
    )


def _issue(client: TestClient, title: str, **fields: Any) -> dict:
    return _ok(client.post("/issues", json={"title": title, **fields}), 201)


def _comment(client: TestClient, issue_id: int, body: str) -> dict:
    return _ok(client.post(f"/issues/{issue_id}/comments", json={"body": body}), 201)


@pytest.fixture
def world(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app, headers=ADMIN) as client:
        bootstrap_admin_creator_outsider(client)
        yield app, client


# --- the page list ---------------------------------------------------------------


def test_the_page_list_is_bounded_and_carries_no_body(world):
    _, client = world
    space = _space(client)
    for number in range(PAGES_DEFAULT + 5):
        _page(client, space["id"], f"page {number:03d}", body="x" * 500)
    first = _ok(client.get(f"/spaces/{space['id']}/pages"))
    assert len(first["pages"]) == PAGES_DEFAULT
    assert first["has_more"] is True and first["next_cursor"]
    assert all("body" not in row for row in first["pages"])
    assert {"id", "title", "parent_id", "updated_at"} <= set(first["pages"][0])
    # The body is the page's own read.
    assert _ok(client.get(f"/pages/{first['pages'][0]['id']}"))["body"] == "x" * 500
    biggest = _ok(client.get(f"/spaces/{space['id']}/pages?limit={PAGES_MAX}"))
    assert len(biggest["pages"]) == PAGES_DEFAULT + 5 and biggest["has_more"] is False
    assert biggest["next_cursor"] is None
    for limit in (0, PAGES_MAX + 1):
        response = client.get(f"/spaces/{space['id']}/pages?limit={limit}")
        assert response.status_code == 422, response.text


def test_following_the_page_cursor_returns_every_page_once_in_title_order(world):
    _, client = world
    space = _space(client)
    titles = [f"Page {chr(ord('a') + n % 26)}{n:02d}" for n in range(23)]
    made = {_page(client, space["id"], title)["id"]: title for title in titles}
    rows, calls = _follow(client, f"/spaces/{space['id']}/pages", "pages", limit=5)
    assert calls == 5
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)) == len(made)
    assert [row["title"] for row in rows] == sorted(titles, key=str.casefold)


def test_a_page_added_between_two_pages_repeats_and_loses_nothing(world):
    _, client = world
    space = _space(client)
    originals = {_page(client, space["id"], f"m{n:02d}")["id"] for n in range(10)}
    path = f"/spaces/{space['id']}/pages"
    first = _ok(client.get(path, params={"limit": 4}))
    # One page sorts before the cursor, one after it: an offset would repeat the
    # fourth row and a cursor must not.
    _page(client, space["id"], "a first")
    late = _page(client, space["id"], "z last")
    rest, _ = _follow(client, path, "pages", limit=4, cursor=first["next_cursor"])
    seen = [row["id"] for row in first["pages"] + rest]
    assert len(seen) == len(set(seen)), "a row was repeated"
    assert originals <= set(seen), "a row was lost"
    assert late["id"] in seen


def test_a_page_cursor_is_refused_where_it_was_not_handed_out(world):
    _, client = world
    space = _space(client)
    for n in range(3):
        _page(client, space["id"], f"p{n}")
    cursor = _ok(client.get(f"/spaces/{space['id']}/pages?limit=1"))["next_cursor"]
    for bad in ("not-a-cursor", "e30", cursor[:-2]):
        refused = client.get(f"/spaces/{space['id']}/pages", params={"cursor": bad})
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "validation_error"
    issue = _issue(client, "elsewhere")
    elsewhere = client.get(f"/issues/{issue['id']}/comments", params={"cursor": cursor})
    assert elsewhere.status_code == 422, elsewhere.text


def test_a_private_space_page_list_reads_as_missing_on_every_page(world):
    _, client = world
    space = _space(client, "PRIV")
    for n in range(3):
        _page(client, space["id"], f"p{n}")
    cursor = _ok(client.get(f"/spaces/{space['id']}/pages?limit=1"))["next_cursor"]
    _ok(client.put(f"/spaces/{space['id']}/visibility", json={"visibility": "private"}))
    for params in ({}, {"cursor": cursor}):
        hidden = client.get(
            f"/spaces/{space['id']}/pages", params=params, headers=OUTSIDER
        )
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["code"] == "not_found"


# --- an issue's comments ---------------------------------------------------------


def test_the_comment_list_is_bounded_and_pages_in_id_order(world):
    _, client = world
    issue = _issue(client, "talkative")
    made = [
        _comment(client, issue["id"], f"comment {n}")["id"]
        for n in range(COMMENTS_DEFAULT + 3)
    ]
    first = _ok(client.get(f"/issues/{issue['id']}/comments"))
    assert [row["id"] for row in first["comments"]] == made[:COMMENTS_DEFAULT]
    assert first["has_more"] is True
    rows, calls = _follow(
        client, f"/issues/{issue['id']}/comments", "comments", limit=7
    )
    assert [row["id"] for row in rows] == made
    assert calls == -(-len(made) // 7)
    for limit in (0, COMMENTS_MAX + 1):
        refused = client.get(f"/issues/{issue['id']}/comments?limit={limit}")
        assert refused.status_code == 422, refused.text


def test_a_comment_added_between_two_pages_repeats_and_loses_nothing(world):
    _, client = world
    issue = _issue(client, "thread")
    made = [_comment(client, issue["id"], f"c{n}")["id"] for n in range(6)]
    first = _ok(client.get(f"/issues/{issue['id']}/comments", params={"limit": 4}))
    late = _comment(client, issue["id"], "late")["id"]
    rest, _ = _follow(
        client,
        f"/issues/{issue['id']}/comments",
        "comments",
        limit=4,
        cursor=first["next_cursor"],
    )
    assert [row["id"] for row in first["comments"] + rest] == [*made, late]


def test_one_comment_is_read_whole_and_a_hidden_one_reads_as_missing(world):
    _, client = world
    project = _ok(client.post("/projects", json={"name": "Quiet", "key": "QT"}), 201)
    issue = _issue(client, "long talk", project_id=project["id"])
    other = _issue(client, "another")
    long_body = "word " * 20_000
    comment = _comment(client, issue["id"], long_body)
    stranger = _comment(client, other["id"], "elsewhere")
    read = _ok(client.get(f"/issues/{issue['id']}/comments/{comment['id']}"))
    assert read["body"] == long_body.strip()
    assert read["id"] == comment["id"] and read["issue_id"] == issue["id"]
    assert read["authored_by"] == {"id": 1, "is_agent": False}
    # A comment of another issue, and one that does not exist, are missing.
    for path in (
        f"/issues/{issue['id']}/comments/{stranger['id']}",
        f"/issues/{issue['id']}/comments/999999",
        f"/issues/999999/comments/{comment['id']}",
    ):
        missing = client.get(path)
        assert missing.status_code == 404, (path, missing.text)
        assert missing.json()["code"] == "not_found"
    _ok(
        client.put(
            f"/projects/{project['id']}/visibility", json={"visibility": "private"}
        )
    )
    # The outsider and an anonymous reader: hidden reads as missing, never 403.
    for reader in (TestClient(client.app, headers=OUTSIDER), TestClient(client.app)):
        for path in (
            f"/issues/{issue['id']}/comments/{comment['id']}",
            f"/issues/{issue['id']}/comments",
        ):
            hidden = reader.get(path)
            assert hidden.status_code == 404, (path, hidden.text)
            assert hidden.json()["code"] == "not_found"


# --- the issue list -------------------------------------------------------------


def test_the_issue_list_is_bounded_and_answers_a_cursor(world):
    _, client = world
    made = [_issue(client, f"issue {n}")["id"] for n in range(ISSUES_DEFAULT + 2)]
    first = _ok(client.get("/issues"))
    assert [row["id"] for row in first["issues"]] == made[:ISSUES_DEFAULT]
    assert first["has_more"] is True
    rows, _ = _follow(client, "/issues", "issues", limit=9)
    assert [row["id"] for row in rows] == made
    assert client.get(f"/issues?limit={ISSUES_MAX + 1}").status_code == 422
    # The offset is retired: a caller still sending it is told, not served page one.
    retired = client.get("/issues?offset=10")
    assert retired.status_code == 422, retired.text


@pytest.mark.parametrize(
    "sort", ["created-desc", "id-desc", "priority-desc", "status-asc", "idle-desc"]
)
def test_a_work_query_pages_every_sort_without_repeating_a_row(world, sort):
    _, client = world
    priorities = ["low", "high", "medium", "urgent"]
    made = {
        _issue(client, f"q{n}", priority=priorities[n % 4])["id"] for n in range(12)
    }
    q = f"is:open sort:{sort}"
    first = _ok(client.get("/issues", params={"q": q, "limit": 5}))
    # Newest-first sorts put this issue ahead of the cursor: an offset repeats a row.
    added = _issue(client, "arrived mid-scan", priority="urgent")["id"]
    rest, _ = _follow(
        client, "/issues", "issues", q=q, limit=5, cursor=first["next_cursor"]
    )
    seen = [row["id"] for row in first["issues"] + rest]
    assert len(seen) == len(set(seen)), f"{sort}: a row was repeated"
    assert made <= set(seen), f"{sort}: a row was lost"
    whole = _ok(client.get("/issues", params={"q": q, "limit": ISSUES_MAX}))
    order = [row["id"] for row in whole["issues"] if row["id"] != added]
    assert [row for row in seen if row != added] == order, f"{sort}: order changed"


def test_a_cursor_from_one_sort_is_refused_by_another(world):
    _, client = world
    for n in range(3):
        _issue(client, f"s{n}")
    cursor = _ok(client.get("/issues", params={"q": "sort:id-asc", "limit": 1}))[
        "next_cursor"
    ]
    for params in ({"q": "sort:id-desc"}, {}):
        refused = client.get("/issues", params={**params, "cursor": cursor})
        assert refused.status_code == 422, (params, refused.text)


def test_the_issue_list_never_shows_a_hidden_issue_on_any_page(world):
    _, client = world
    project = _ok(client.post("/projects", json={"name": "Hidden", "key": "HID"}), 201)
    hidden = {_issue(client, f"h{n}", project_id=project["id"])["id"] for n in range(4)}
    shown = {_issue(client, f"v{n}")["id"] for n in range(4)}
    _ok(
        client.put(
            f"/projects/{project['id']}/visibility", json={"visibility": "private"}
        )
    )
    outsider = TestClient(client.app, headers=OUTSIDER)
    for params in ({}, {"q": "is:open"}):
        rows, calls = _follow(outsider, "/issues", "issues", limit=3, **params)
        assert {row["id"] for row in rows} == shown
        assert not hidden & {row["id"] for row in rows}
        assert calls == 2  # four visible rows, three a page: no page is a hidden gap


# --- the MCP tools continue with the server's cursor -------------------------------


def _mcp_world(client: TestClient, pages: int, comments: int):
    """A seat, a space of ``pages`` pages and an issue of ``comments`` comments."""
    seat = onboard_agent(client, scopes=("read", "issue:write", "docs:write"))
    space = _space(client)
    for n in range(pages):
        _page(client, space["id"], f"page {n:03d}", body="y" * 2_000)
    issue = _issue(client, "the thread")
    for n in range(comments):
        _comment(client, issue["id"], f"comment {n} " + "z" * 300)
    return seat, space, issue


def _metered_server(app, seat, profile: str):
    """The seat's MCP server (as ``support.mcp_server_for`` builds it), with
    every REST response it receives counted: calls, bytes and list rows."""
    fetched = {"calls": 0, "rows": 0, "bytes": 0}
    http = TestClient(app, headers=bearer(seat))
    client = AthenaClient(client=http, run_id="h6-run")
    server = mcp_server.build_server(
        client,
        scopes=mcp_server.probe_scopes(client),
        profile=mcp_server.PROFILES[profile],
    )

    def meter(response):
        response.read()
        fetched["calls"] += 1
        fetched["bytes"] += len(response.content)
        body = response.json()
        if isinstance(body, dict):
            for key in ("pages", "comments", "issues"):
                if isinstance(body.get(key), list):
                    fetched["rows"] += len(body[key])
        elif isinstance(body, list):
            fetched["rows"] += len(body)

    http.event_hooks["response"].append(meter)
    return server, fetched


async def _drain(
    session, tool: str, arguments: dict, between: Any = None
) -> tuple[list[dict], int]:
    """Every row of a tool's list, following ``next``; ``between()`` runs once,
    after the first call."""
    rows: list[dict] = []
    calls = 0
    while True:
        result = await session.call_tool(tool, arguments)
        answer = json.loads("".join(block.text for block in result.content))
        assert not result.isError, answer
        calls += 1
        rows += answer["items"]
        if not answer["truncated"]:
            return rows, calls
        if calls == 1 and between is not None:
            between()
        arguments = {**arguments, **answer["next"]}
        assert calls < 200


def test_list_pages_follows_the_server_cursor_not_the_whole_list(world):
    app, client = world
    pages = 120
    seat, space, _ = _mcp_world(client, pages=pages, comments=0)
    server, fetched = _metered_server(app, seat, "seat+knowledge")
    whole = len(client.get(f"/spaces/{space['id']}/pages?limit=200").content)

    async def play():
        async with mcp_sessions(server) as (session,):
            return await _drain(session, "list_pages", {"space_id": space["id"]})

    rows, calls = run_agents(play())
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)) == pages
    assert all("body" not in row for row in rows)
    # N/limit calls (the tool's default limit is 50), and each row crosses the
    # wire once: the bytes are those of one listing, not one listing per call.
    assert calls == -(-pages // 50)
    assert fetched["calls"] == calls
    assert fetched["rows"] == pages
    assert fetched["bytes"] < 2 * whole


def test_list_issue_comments_follows_the_server_cursor(world):
    app, client = world
    comments = 130
    seat, _, issue = _mcp_world(client, pages=0, comments=comments)
    server, fetched = _metered_server(app, seat, "seat")

    async def play():
        async with mcp_sessions(server) as (session,):
            return await _drain(
                session, "list_issue_comments", {"issue_id": issue["id"]}
            )

    rows, calls = run_agents(play())
    ids = [row["id"] for row in rows]
    assert ids == sorted(set(ids)) and len(ids) == comments
    assert calls == -(-comments // 40)  # the tool asks for 40 excerpt rows a call
    assert fetched["calls"] == calls
    assert fetched["rows"] == comments


def test_search_work_follows_the_server_cursor(world):
    app, client = world
    seat, _, _ = _mcp_world(client, pages=0, comments=0)
    made = sorted(_issue(client, f"found {n}", body="b" * 400)["id"] for n in range(70))
    server, fetched = _metered_server(app, seat, "seat")

    async def play():
        async with mcp_sessions(server) as (session,):
            return await _drain(
                session,
                "search_work",
                {"q": "found sort:id-desc"},
                # Newest first: an issue found mid-scan sorts ahead of the cursor,
                # where an offset repeated the row at the page boundary.
                between=lambda: _issue(client, "found late"),
            )

    rows, calls = run_agents(play())
    assert [row["id"] for row in rows] == made[::-1]
    assert all("body" not in row for row in rows)
    # A budget cut asks the route again for what fits, and the next call asks for
    # that much: never the whole list again.
    assert fetched["calls"] <= calls + 2
    assert fetched["rows"] < 2 * len(made)


def test_a_page_the_budget_cuts_continues_from_the_servers_cursor(world):
    """Forty comments of 3,000 characters do not fit a seat's 16,000: the tool
    asks the route again for the rows that fit and names that limit in `next`,
    so every later call asks for what fits and no row is read twice or lost."""
    app, client = world
    seat, _, issue = _mcp_world(client, pages=0, comments=0)
    made = [
        _comment(client, issue["id"], f"{n} " + "w" * 3_000)["id"] for n in range(60)
    ]
    server, fetched = _metered_server(app, seat, "seat")
    arguments = {"issue_id": issue["id"], "include_body": True}

    async def play():
        async with mcp_sessions(server) as (session,):
            first = json.loads(
                (await session.call_tool("list_issue_comments", arguments))
                .content[0]
                .text
            )
            rows, calls = await _drain(
                session, "list_issue_comments", {**arguments, **first["next"]}
            )
            return first, first["items"] + rows, calls + 1

    first, rows, calls = run_agents(play())
    kept = first["count"]
    assert 1 < kept < 40 and first["truncated"] is True
    assert first["next"]["limit"] == kept and first["next"]["cursor"]
    assert [row["id"] for row in rows] == made
    # The first call read 40 and then what fits; every later call about what fits.
    assert fetched["rows"] <= len(made) + 40 + 2 * calls
    assert fetched["calls"] <= 2 * calls


def test_a_seat_reads_a_comment_the_list_cut_whole(world):
    app, client = world
    seat, _, issue = _mcp_world(client, pages=0, comments=0)
    long_body = "".join(f"sentence {n:05d}. " for n in range(4_000))
    comment = _comment(client, issue["id"], long_body)
    server, _ = _metered_server(app, seat, "seat")

    async def play():
        async with mcp_sessions(server) as (session,):
            listed = await session.call_tool(
                "list_issue_comments",
                {"issue_id": issue["id"], "include_body": True, "max_chars": 12_000},
            )
            row = json.loads(listed.content[0].text)["items"][0]
            text = row["body"]
            more = row["_truncated"]["body"]["next"]
            for _ in range(50):
                assert more["tool"] == "get_issue_comment", more
                result = await session.call_tool(more["tool"], more["arguments"])
                answer = json.loads(result.content[0].text)
                assert not result.isError, answer
                assert answer["id"] == comment["id"]
                text += answer["body"]
                more = ((answer.get("_truncated") or {}).get("body") or {}).get("next")
                if more is None:
                    return text
            raise AssertionError("the comment never ended")

    assert run_agents(play()) == long_body.strip()
