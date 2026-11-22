"""Every tool a seat can carry answers within its response budget (A6).

WHY: a list or search call that returns whole bodies spends an agent's context
on text it did not ask for. Measured before this change: ``search_work`` returned
161,000 characters with one large issue and ``list_pages`` 203,000 over twelve
pages, so one routine call could fill a context window.

The contract pinned here: every tool in ``seat``, ``seat+knowledge`` and
``conductor`` answers within ``mcp_server.response_budget(name)`` characters
(16,000 unless the tool is on the commented exception list beside
``TOOL_SCOPES``) against a workspace seeded with one 200,000-character page, one
150,000-character issue description and sixty comments on that issue. The test
calls EVERY tool in the three profiles over the protocol, with default arguments
plus the minimum required ones, pointing at the large rows; a tool it cannot call
fails the test instead of being skipped.

What a client receives is measured: the text of the result's content blocks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from typing import Any

from fastapi.testclient import TestClient
import httpx
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from athena.main import create_app
from athena.mcp import results
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}
BUDGETED_PROFILES = ("seat", "seat+knowledge", "conductor")
BIG_PAGE_CHARS = 200_000
BIG_ISSUE_CHARS = 150_000
COMMENT_COUNT = 60
RUN_ID = "budget-run"


def _text(chars: int, word: str) -> str:
    """Searchable prose of exactly ``chars`` characters."""
    sentence = f"The {word} paragraph repeats so the row is large. "
    return (sentence * (chars // len(sentence) + 1))[:chars]


def _seed(admin: TestClient) -> dict[str, Any]:
    """The large-content workspace, built through REST as the admin (user 1)."""

    def ok(response, status=200):
        assert response.status_code == status, response.text
        return response.json()

    admin.post("/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"})
    admin.headers.update(H1)

    def onboard(email, name, scopes):
        return ok(
            admin.post(
                "/users/onboard_agent",
                json={"email": email, "name": name, "scopes": scopes},
            ),
            201,
        )

    seat = onboard("sol@e.com", "Sol", ["read", "issue:write", "docs:write"])
    reviewer = onboard("ray@e.com", "Ray", ["read", "issue:write"])
    sol, ray = seat["user"]["id"], reviewer["user"]["id"]
    project = ok(admin.post("/projects", json={"name": "Budget", "key": "BUD"}), 201)

    def issue(title, body="", **fields):
        return ok(
            admin.post(
                "/issues",
                json={
                    "title": title,
                    "body": body,
                    "project_id": project["id"],
                    **fields,
                },
            ),
            201,
        )

    big = issue("the large issue", _text(BIG_ISSUE_CHARS, "budget"))
    small = issue("a small budget issue", "short")
    for item in (big, small):
        ok(admin.post(f"/issues/{item['id']}/delegate", json={"user_id": sol}), 201)
    for number in range(COMMENT_COUNT):
        ok(
            admin.post(
                f"/issues/{big['id']}/comments",
                json={"body": f"comment {number}: " + _text(900, "comment")},
            ),
            201,
        )
    # One mention lands in the seat's inbox, for mark_notification_read.
    ok(
        admin.post(
            f"/issues/{big['id']}/comments",
            json={"body": f"please look [[user:{sol}]]"},
        ),
        201,
    )
    label = ok(admin.post("/labels", json={"name": "budgeted"}), 201)

    # An exact candidate for request_candidate_review: the admin submits it on
    # a build issue; Ray, delegated on a review issue, is the eligible reviewer.
    build = issue("build", "build it")
    etag = admin.get(f"/issues/{build['id']}").headers["ETag"]
    held = ok(
        admin.post(f"/issues/{build['id']}/claim", json={}, headers={"If-Match": etag}),
        201,
    )
    submitted = ok(
        admin.post(
            f"/issues/{build['id']}/complete",
            json={
                "generation": held["generation"],
                "candidate_submission": {
                    "identity": {
                        "kind": "git",
                        "source": "owner/repository",
                        "revision": "a" * 40,
                    },
                    "expected_previous_candidate_id": None,
                },
            },
        )
    )
    review = issue("review the build", "review it")
    for agent in (sol, ray):
        ok(admin.post(f"/issues/{review['id']}/delegate", json={"user_id": agent}), 201)

    space = ok(admin.post("/spaces", json={"key": "BUD", "name": "Budget"}), 201)
    big_page = ok(
        admin.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "the large page", "body": _text(BIG_PAGE_CHARS, "budget")},
        ),
        201,
    )
    for number in range(11):
        ok(
            admin.post(
                f"/spaces/{space['id']}/pages",
                json={"title": f"page {number}", "body": _text(300, "budget")},
            ),
            201,
        )
    return {
        "seat": seat,
        "sol": sol,
        "ray": ray,
        "project": project,
        "big": big,
        "small": small,
        "label": label,
        "candidate_id": submitted["candidate_submission"]["candidate_id"],
        "review": review,
        "space": space,
        "big_page": big_page,
    }


Step = tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]


def _plan(world: dict[str, Any], admin: TestClient) -> list[Step]:
    """One call per tool, in an order where each write finds the state it needs.
    Arguments are the minimum each tool requires, pointing at the large rows."""
    big, page = world["big"]["id"], world["big_page"]["id"]
    project = world["project"]["id"]

    def control(state):
        made = admin.post(
            "/run-controls",
            json={"run_id": RUN_ID, "kind": "steer", "payload": "use approach B"},
        )
        assert made.status_code == 201, made.text
        return made.json()["id"]

    return [
        ("my_desk", lambda s: {}),
        ("current_run", lambda s: {}),
        ("list_my_delegated_work", lambda s: {}),
        ("search_work", lambda s: {"q": "budget"}),
        ("search", lambda s: {"query": "budget"}),
        ("get_issue", lambda s: {"issue_id": big}),
        ("get_issue_work_context", lambda s: {"issue_id": big}),
        ("list_issue_comments", lambda s: {"issue_id": big}),
        ("get_issue_history", lambda s: {"issue_id": big}),
        ("get_issue_runbook", lambda s: {"issue_id": big}),
        ("list_subtasks", lambda s: {"issue_id": big}),
        ("list_notifications", lambda s: {}),
        ("list_projects", lambda s: {}),
        ("list_labels", lambda s: {}),
        ("get_page", lambda s: {"page_id": page}),
        ("list_pages", lambda s: {"space_id": world["space"]["id"]}),
        ("get_attention_ranking", lambda s: {}),
        ("get_project_floor", lambda s: {"project_id": project}),
        ("get_project_brief", lambda s: {"project_id": project}),
        ("get_project_review_queue", lambda s: {"project_id": project}),
        ("wait_for_events", lambda s: {}),
        # writes: the claim first, so the lease reads and the loop have a holder
        ("claim_issue", lambda s: {"issue_id": big, "if_match": s["etag"]()}),
        ("get_issue_lease", lambda s: {"issue_id": big}),
        ("heartbeat_agent_run", lambda s: {"run_id": RUN_ID}),
        ("comment_on_issue", lambda s: {"issue_id": big, "body": "noted"}),
        (
            "update_issue",
            lambda s: {"issue_id": big, "title": "the large issue, renamed"},
        ),
        ("attach_label", lambda s: {"issue_id": big, "label_id": world["label"]["id"]}),
        (
            "link_issues",
            lambda s: {
                "issue_id": big,
                "target_ref": world["small"]["key"],
                "relation": "relates",
            },
        ),
        ("assign_issue", lambda s: {"issue_id": big, "assignee_id": world["sol"]}),
        (
            "set_issue_parent",
            lambda s: {"issue_id": world["small"]["id"], "parent_id": big},
        ),
        (
            "delegate_issue",
            lambda s: {"issue_id": world["small"]["id"], "agent_user_id": world["ray"]},
        ),
        (
            "request_candidate_review",
            lambda s: {
                "review_issue_id": world["review"]["id"],
                "candidate_id": world["candidate_id"],
                "reviewer_id": world["ray"],
            },
        ),
        (
            "yield_claim",
            lambda s: {
                "issue_id": big,
                "generation": s["claim_issue"]["generation"],
                "reason": "needs_input",
                "attempted_work": "read the large issue",
                "evidence": ["the body is long"],
                "blocking_question": "which half first?",
                "resume_instructions": "start at the top",
            },
        ),
        (
            "resume_claim_handoff",
            lambda s: {
                "issue_id": big,
                "handoff_token": s["yield_claim"]["handoff_token"],
                "generation": s["reclaim"](),
            },
        ),
        ("complete_claim", lambda s: {"issue_id": big, "generation": s["generation"]}),
        ("decline_delegation", lambda s: {"issue_id": world["small"]["id"]}),
        ("claim_next_ready", lambda s: {}),
        ("create_issue", lambda s: {"title": "a new budget issue"}),
        ("create_page", lambda s: {"space_id": world["space"]["id"], "title": "new"}),
        (
            "update_page",
            lambda s: {"page_id": page, "title": "the large page, renamed"},
        ),
        ("record_run_learning", lambda s: {"issue_id": big, "summary": "learned"}),
        (
            "mark_notification_read",
            lambda s: {"notification_id": s["notification"]()},
        ),
        (
            "acknowledge_run_control",
            lambda s: {"control_id": s.setdefault("c1", control(s))},
        ),
        ("complete_run_control", lambda s: {"control_id": s["c1"], "summary": "done"}),
        ("decline_run_control", lambda s: {"control_id": control(s), "reason": "no"}),
        ("list_run_controls", lambda s: {}),
        ("advance_desk_cursor", lambda s: {"after_id": s["latest_event"]()}),
    ]


@pytest.fixture
def budget_run(tmp_path):
    """Every budgeted tool called once over the protocol against the seeded
    workspace: the size of each answer, and the answer itself."""
    app = create_app(tmp_path / "budget.db")
    with TestClient(app) as admin:
        world = _seed(admin)
        bearer = {"Authorization": f"Bearer {world['seat']['token']['token']}"}
        seat_http = TestClient(app, headers=bearer)
        client = AthenaClient(client=seat_http, run_id=RUN_ID)
        union = frozenset().union(
            *(mcp_server.PROFILES[name] for name in BUDGETED_PROFILES)
        )
        server = mcp_server.build_server(client, profile=union)
        state: dict[str, Any] = {}
        big = world["big"]["id"]

        def etag():
            return seat_http.get(f"/issues/{big}").headers["ETag"]

        def reclaim():
            held = seat_http.post(
                f"/issues/{big}/claim", json={}, headers={"If-Match": etag()}
            )
            assert held.status_code == 201, held.text
            state["generation"] = held.json()["generation"]
            return state["generation"]

        def notification():
            rows = seat_http.get("/notifications", params={"unread": True}).json()
            assert rows, "the seeded mention did not reach the seat's inbox"
            return rows[0]["id"]

        def latest_event():
            desk = seat_http.get("/desk").json()
            return desk["signals"]["latest_visible_event_id"]

        state.update(
            etag=etag,
            reclaim=reclaim,
            notification=notification,
            latest_event=latest_event,
        )
        plan = _plan(world, admin)
        planned = [name for name, _ in plan]
        assert sorted(planned) == sorted(union), (
            "every budgeted tool needs exactly one step in this test: "
            f"missing {sorted(union - set(planned))}, "
            f"extra {sorted(set(planned) - union)}"
        )
        sizes: dict[str, int] = {}
        answers: dict[str, list[Any]] = {}
        failures: dict[str, str] = {}

        async def run() -> None:
            async with create_connected_server_and_client_session(
                server._mcp_server  # noqa: SLF001 — the protocol server under test
            ) as session:
                for name, arguments in plan:
                    result = await session.call_tool(name, arguments(state))
                    texts = [getattr(block, "text", "") for block in result.content]
                    sizes[name] = sum(len(text) for text in texts)
                    answers[name] = [json.loads(text) for text in texts]
                    if result.isError:
                        failures[name] = "".join(texts)[:300]
                    elif name in {"claim_issue", "yield_claim"}:
                        state[name] = answers[name][0]

        asyncio.run(run())
        # A tool this test cannot call fails it: an answer that is an error
        # measures nothing about the tool's budget.
        assert failures == {}, failures
        yield {
            "sizes": sizes,
            "answers": answers,
            "world": world,
            "server": server,
            "seat_http": seat_http,
        }


def _largest_per_profile(sizes: dict[str, int]) -> dict[str, tuple[str, int]]:
    return {
        profile: max(
            ((name, sizes[name]) for name in mcp_server.PROFILES[profile]),
            key=lambda pair: pair[1],
        )
        for profile in BUDGETED_PROFILES
    }


def test_every_budgeted_tool_answers_within_its_budget(budget_run):
    sizes = budget_run["sizes"]
    over = {
        name: (size, mcp_server.response_budget(name))
        for name, size in sorted(sizes.items())
        if size > mcp_server.response_budget(name)
    }
    assert over == {}, (
        f"answers above their budget (size, budget): {over}; "
        f"largest per profile: {_largest_per_profile(sizes)}"
    )
    # The seeded rows really are large: without the budget these answers are not.
    assert len(budget_run["world"]["big"]["body"]) >= BIG_ISSUE_CHARS
    assert len(budget_run["world"]["big_page"]["body"]) >= BIG_PAGE_CHARS


def test_the_budget_table_is_the_default_and_a_short_reviewed_list():
    assert mcp_server.DEFAULT_RESPONSE_BUDGET == 16_000
    assert set(mcp_server.RESPONSE_BUDGET_EXCEPTIONS) <= mcp_server.PROFILES["seat"]
    assert len(mcp_server.RESPONSE_BUDGET_EXCEPTIONS) <= 3
    for name, chars in mcp_server.RESPONSE_BUDGET_EXCEPTIONS.items():
        assert chars > mcp_server.DEFAULT_RESPONSE_BUDGET, name
        assert mcp_server.response_budget(name) == chars
    # A tool no budgeted profile carries has no budget: the full surface answers
    # as its REST route does.
    assert mcp_server.response_budget("list_issues") is None
    assert mcp_server.response_budget("search_work") == 16_000


def _marks(value: Any, path: str = "") -> list[tuple[str, dict]]:
    """Every ``_truncated`` entry anywhere in an answer, by where it sits."""
    found: list[tuple[str, dict]] = []
    if isinstance(value, dict):
        for field, mark in (value.get("_truncated") or {}).items():
            found.append((f"{path}{field}", mark))
        for key, inner in value.items():
            if key != "_truncated":
                found += _marks(inner, f"{path}{key}.")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found += _marks(inner, f"{path}{index}.")
    return found


def test_nothing_the_budget_cut_is_cut_silently(budget_run):
    cut: dict[str, list[str]] = {}
    for name, (answer,) in budget_run["answers"].items():
        if answer.get("truncated") is True and set(answer) == {
            "items",
            "count",
            "truncated",
            "next",
        }:
            assert answer["next"], name
            cut.setdefault(name, []).append("items")
        carriers = [
            mcp_server.PROFILES[profile]
            for profile in BUDGETED_PROFILES
            if name in mcp_server.PROFILES[profile]
        ]
        for where, mark in _marks(answer):
            reader = mark["next"]
            assert reader is not None, (name, where)
            # The call that reads on is one every session holding this tool has.
            for carried in carriers:
                assert reader["tool"] in carried, (name, where, reader)
            assert isinstance(reader["arguments"], dict), (name, where)
            cut.setdefault(name, []).append(where)
    # The large rows were cut where they live, and each cut is on record.
    for name in (
        "get_issue",
        "update_issue",
        "get_page",
        "update_page",
        "get_issue_work_context",
        "list_issue_comments",
    ):
        assert name in cut, sorted(cut)


def _call(server, name: str, arguments: dict) -> dict:
    content, structured = asyncio.run(server.call_tool(name, arguments))
    assert len(content) == 1
    return structured


def test_following_next_reads_a_cut_body_whole(budget_run):
    server = budget_run["server"]
    world = budget_run["world"]
    for tool, key, row in (
        ("get_issue", "issue_id", world["big"]),
        ("get_page", "page_id", world["big_page"]),
    ):
        pieces, arguments, reads = [], {key: row["id"]}, 0
        while True:
            answer = _call(server, tool, arguments)
            reads += 1
            pieces.append(answer["body"])
            mark = (answer.get("_truncated") or {}).get("body")
            if mark is None or mark["next"] is None:
                break
            assert mark["total_chars"] == len(row["body"])
            assert mark["next"]["tool"] == tool
            arguments = mark["next"]["arguments"]
        assert "".join(pieces) == row["body"], tool
        assert reads >= len(row["body"]) // mcp_server.DEFAULT_RESPONSE_BUDGET


def test_list_and_search_rows_are_summaries(budget_run):
    server = budget_run["server"]
    world = budget_run["world"]
    hits = _call(server, "search_work", {"q": "budget"})["items"]
    assert hits and all("body" not in row and "title" in row for row in hits)
    pages = _call(server, "list_pages", {"space_id": world["space"]["id"]})["items"]
    rest = budget_run["seat_http"].get(f"/spaces/{world['space']['id']}/pages").json()
    assert [row["id"] for row in pages] == [row["id"] for row in rest]
    assert len(pages) >= 12
    assert all("body" not in row and "title" in row for row in pages)
    inbox = _call(server, "list_my_delegated_work", {})["items"]
    assert inbox and all("body" not in item["issue"] for item in inbox)
    desk = _call(server, "my_desk", {})
    assert all(
        "body" not in item["issue"] for item in desk["work"]["delegations"]["items"]
    )
    # The REST answers are unchanged: they still carry the bodies.
    assert all("body" in row for row in rest)


def test_comments_page_through_by_next_with_excerpts_or_bodies(budget_run):
    server = budget_run["server"]
    issue_id = budget_run["world"]["big"]["id"]
    rest = budget_run["seat_http"].get(f"/issues/{issue_id}/comments").json()
    seen, arguments = [], {"issue_id": issue_id}
    while True:
        page = _call(server, "list_issue_comments", arguments)
        for row in page["items"]:
            assert "body" not in row
            assert len(row["excerpt"]) <= 200
        seen += page["items"]
        if not page["truncated"]:
            break
        arguments = {"issue_id": issue_id, **page["next"]}
    assert [row["id"] for row in seen] == [row["id"] for row in rest]
    assert [row["body_chars"] for row in seen] == [len(row["body"]) for row in rest]
    bodies = _call(
        server,
        "list_issue_comments",
        {"issue_id": issue_id, "include_body": True, "max_chars": 100},
    )
    first = bodies["items"][0]
    assert first["body"] == rest[0]["body"][:100]
    assert first["_truncated"]["body"]["total_chars"] == len(rest[0]["body"])
    reread = first["_truncated"]["body"]["next"]
    assert reread["tool"] == "list_issue_comments"
    whole = _call(server, reread["tool"], reread["arguments"])["items"][0]
    assert whole["body"] == rest[0]["body"]


# --- the fitting rules, unit by unit ---------------------------------------------


def _size(value) -> int:
    return results.serialized_size(value)


def test_a_list_over_budget_keeps_what_fits_and_resumes_after_it():
    rows = [{"id": n, "title": "x" * 50} for n in range(40)]
    whole = results.listing(rows, resume=lambda kept: {"offset": kept})
    with results.budget(_size(whole) // 2):
        cut = results.sliced(rows, offset=0)
        paged = results.paged(rows, limit=40, offset=10)
    assert cut["truncated"] is True and 1 <= cut["count"] < 40
    assert cut["next"] == {"offset": cut["count"]}
    assert _size(cut) <= _size(whole) // 2
    assert paged["next"] == {"offset": 10 + paged["count"]}
    # One row over budget is returned whole: there is nothing to resume after.
    with results.budget(10):
        single = results.listing(rows[:1], resume=lambda kept: {"offset": kept})
    assert single == results.listing(rows[:1])
    # Without a budget nothing is cut.
    assert results.sliced(rows, offset=5)["count"] == 35
    with pytest.raises(ValueError, match="negative"):
        results.sliced(rows, offset=-1)


def test_a_capped_window_cut_by_the_budget_resumes_inside_the_cap():
    rows = [{"id": n, "title": "y" * 50} for n in range(10)]
    with results.budget(_size(results.listing(rows[:2])) + 40):
        page = results.window(lambda limit: rows[:limit], limit=4, offset=6, cap=10)
    assert page["truncated"] is True
    assert page["next"]["offset"] == 6 + page["count"]
    assert page["next"]["limit"] == 10 - (6 + page["count"])


def test_an_object_gives_up_its_least_valuable_places_first():
    packet = {
        "activity": [{"id": n, "detail": "a" * 100} for n in range(30)],
        "comments": [{"id": n, "text": "c" * 500} for n in range(10)],
        "description": "d" * 20_000,
    }
    cuts = [
        results.Cut(("activity",), lambda kept: {"tool": "feed", "arguments": {}}),
        results.Cut(
            ("comments",),
            lambda kept: {"tool": "comments", "arguments": {"max": kept}},
            each=("text",),
            floor=50,
        ),
        results.Cut(
            ("description",),
            lambda kept: {"tool": "read", "arguments": {"offset": kept}},
        ),
    ]
    with results.budget(12_000):
        fitted = results.fit_object(packet, cuts)
    assert _size(fitted) <= 12_000
    marks = fitted["_truncated"]
    assert marks["activity"]["total_items"] == 30
    assert len(fitted["activity"]) >= 1
    assert marks["comments[*].text"]["max_chars"] >= 50
    kept = len(fitted["description"])
    assert marks["description"] == {
        "total_chars": 20_000,
        "next": {"tool": "read", "arguments": {"offset": kept}},
    }
    assert packet["description"] == "d" * 20_000  # the input is not mutated
    # When the first place alone makes room, nothing after it is touched.
    small = {**packet, "description": "short"}
    with results.budget(_size(small) - 1_000):
        lean = results.fit_object(small, cuts)
    assert set(lean["_truncated"]) == {"activity"}
    assert lean["comments"] == packet["comments"]
    assert 1 < len(lean["activity"]) < 30


def test_an_object_no_cut_can_fit_is_cut_to_its_floors_and_never_elsewhere():
    answer = {"body": "b" * 5_000, "other": "o" * 5_000, "empty": ""}
    cuts = [
        results.Cut(("missing",), lambda kept: {"tool": "x", "arguments": {}}),
        results.Cut(("empty",), lambda kept: {"tool": "x", "arguments": {}}),
        results.Cut(
            ("body",), lambda kept: {"tool": "read", "arguments": {"offset": kept}}
        ),
    ]
    with results.budget(1_000):
        fitted = results.fit_object(answer, cuts)
    assert fitted["body"] == "b"
    assert fitted["other"] == answer["other"]  # unlisted: never cut
    assert set(fitted["_truncated"]) == {"body"}
    with results.budget(None):
        assert results.fit_object(answer, cuts) is answer


def test_a_body_read_from_an_offset_says_where_it_starts():
    issue = {"id": 1, "body": "0123456789"}
    assert results.from_offset(issue, "body", 0) is issue
    window = results.from_offset(issue, "body", 4)
    assert window["body"] == "456789"
    assert window["_truncated"] == {
        "body": {"total_chars": 10, "offset": 4, "next": None}
    }
    with pytest.raises(ValueError, match="negative"):
        results.from_offset(issue, "body", -1)
    assert results.from_offset({"id": 2}, "body", 3) == {"id": 2}


def _mocked(body) -> AthenaClient:
    return AthenaClient(
        client=httpx.Client(
            base_url="http://athena.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body)
            ),
        )
    )


def _delegation(number: int) -> dict:
    return {
        "issue": {"id": number, "title": f"issue {number}", "body": "b" * 2_000},
        "delegated_at": "2026-11-22 00:00:00",
        "warnings": ["w" * 300],
    }


def test_a_desk_over_budget_gives_up_delegation_rows_to_the_inbox_tool():
    desk = {
        "identity": {"id": 2},
        "work": {
            "delegations": {
                "items": [_delegation(n) for n in range(40)],
                "limit": 40,
                "has_more": False,
                "next_offset": None,
            }
        },
    }
    server = mcp_server.build_server(_mocked(desk), profile=mcp_server.SEAT_PROFILE)
    answer = _call(server, "my_desk", {})
    assert results.serialized_size(answer) <= mcp_server.DEFAULT_RESPONSE_BUDGET
    lane = answer["work"]["delegations"]
    kept = len(lane["items"])
    assert 1 <= kept < 40
    assert all("body" not in item["issue"] for item in lane["items"])
    assert (lane["has_more"], lane["next_offset"]) == (True, kept)
    assert answer["_truncated"]["work.delegations.items"] == {
        "total_items": 40,
        "next": {"tool": "list_my_delegated_work", "arguments": {"offset": kept}},
    }


def test_the_delegation_inbox_over_budget_pages_on_from_its_offset():
    inbox = {
        "items": [_delegation(n) for n in range(60)],
        "limit": 60,
        "offset": 10,
        "has_more": False,
        "next_offset": None,
    }
    server = mcp_server.build_server(_mocked(inbox), profile=mcp_server.SEAT_PROFILE)
    answer = _call(server, "list_my_delegated_work", {"limit": 60, "offset": 10})
    assert results.serialized_size(answer) <= mcp_server.DEFAULT_RESPONSE_BUDGET
    kept = len(answer["items"])
    assert (answer["has_more"], answer["next_offset"]) == (True, 10 + kept)
    assert answer["_truncated"]["items"]["next"] == {
        "tool": "list_my_delegated_work",
        "arguments": {"offset": 10 + kept},
    }


def test_cuts_that_find_nothing_to_cut_leave_the_answer_and_add_no_mark():
    answer = {"other": "o" * 2_000, "rows": "not a list"}
    cuts = [
        results.Cut(
            ("rows",), lambda kept: {"tool": "x", "arguments": {}}, each=("t",)
        ),
        results.Cut(("missing", "deeper"), lambda kept: {"tool": "x", "arguments": {}}),
    ]
    with results.budget(100):
        fitted = results.fit_object(answer, cuts)
    assert fitted == answer
    assert "_truncated" not in fitted
