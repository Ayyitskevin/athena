"""``my_desk`` answers within its budget with every lane at its maximum (F3).

PERF-2: with 23 ready issues the lead's ``my_desk`` answer over stdio was 23,546
characters against a 16,000 budget, ``_truncated`` present and still over; the
reviewer saw 21,136 with the ready lane full and no ``_truncated`` at all. The
budget only cut the places it was told about (the delegation lane), and the
all-tools budget test seeded one large issue, which never fills a desk lane.

The contract held here: one seat's desk is filled through REST with every lane
one row past its declared maximum and every text at the longest its write path
declares (``support.fill_desk_lanes``). ``my_desk`` must then answer within
``response_budget("my_desk")`` with each lane full on its own and with all of
them full at once; the identity block and the head of the ready lane must
survive; and every cut must be recorded under ``_truncated`` with a ``next``
call a seat holds, which reads on.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest
from support import (
    ADMIN,
    bootstrap,
    bearer,
    fill_desk_lanes,
    make_app,
    mcp_server_for,
    mcp_sessions,
    onboard_agent,
    run_agents,
)

from athena import config
from athena.mcp import results
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

BUDGET = mcp_server.response_budget("my_desk")
SEAT_TOOLS = mcp_server.PROFILES["seat"]
#: How many of the ready lane's first rows a cut desk keeps at least
#: (``server.DESK_READY_KEPT``): what claim_next_ready serves next.
READY_HEAD = 3


def _lanes(desk: dict[str, Any], path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every list in the desk reached through objects alone: its lanes."""
    found = []
    for key, value in desk.items():
        here = (*path, key)
        if isinstance(value, list):
            found.append(here)
        elif isinstance(value, dict):
            found += _lanes(value, here)
    return found


def _at(value: Any, path: tuple[str, ...]) -> Any:
    for step in path:
        value = value[step]
    return value


def _put(value: Any, path: tuple[str, ...], new: Any) -> None:
    for step in path[:-1]:
        value = value[step]
    value[path[-1]] = new


def _serve(desk: dict[str, Any]) -> mcp_server.FastMCP:
    """A seat session whose REST desk read answers ``desk``."""
    client = AthenaClient(
        client=httpx.Client(
            base_url="http://athena.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=desk)
            ),
        )
    )
    return mcp_server.build_server(client, profile=mcp_server.SEAT_PROFILE)


def _call(server, arguments: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    content, _ = asyncio.run(server.call_tool("my_desk", arguments))
    (block,) = content
    return len(block.text), json.loads(block.text)


def _useful(answer: dict[str, Any], rest_desk: dict[str, Any], what: str) -> None:
    """The answer is still a desk: who you are, and what pickup serves next."""
    identity, whole = answer["identity"], rest_desk["identity"]
    assert set(identity) == set(whole), what
    for key in ("id", "name", "email", "role", "is_agent", "paused", "scopes"):
        assert identity[key] == whole[key], (what, key)
    ready = rest_desk["work"]["ready"]["items"]
    kept = [row["issue"]["id"] for row in answer["work"]["ready"]["items"]]
    head = min(len(ready), READY_HEAD)
    assert len(kept) >= head, (what, kept)
    assert kept == [row["issue"]["id"] for row in ready[: len(kept)]], what
    assert answer["work"]["ready"]["total"] == rest_desk["work"]["ready"]["total"]
    for name, mark in (answer.get("_truncated") or {}).items():
        reader = mark["next"]
        assert reader is not None and reader["tool"] in SEAT_TOOLS, (what, name)


@pytest.fixture
def full_desk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TOKEN_RATE_LIMIT_PER_MINUTE", 0)
    app, db_file = make_app(tmp_path, "desk-max.db")
    with TestClient(app) as anonymous:
        bootstrap(anonymous)
        admin = TestClient(app, headers=ADMIN)
        sol = onboard_agent(
            admin, email="sol@e.com", scopes=["read", "issue:write", "docs:write"]
        )
        rex = onboard_agent(admin, email="rex@e.com", name="Rex")
        project = admin.post("/projects", json={"name": "Max", "key": "MAX"})
        assert project.status_code == 201, project.text
        filled = fill_desk_lanes(
            app,
            db_file,
            admin=admin,
            seat=sol,
            other=rex,
            project_id=project.json()["id"],
        )
        desk = admin.get("/desk", headers=bearer(sol)).json()
        yield app, sol, desk, filled


def test_my_desk_fits_its_budget_with_each_lane_full_alone_and_all_together(
    full_desk,
):
    app, sol, desk, filled = full_desk
    lanes = _lanes(desk)
    full = [path for path in lanes if _at(desk, path)]
    # The fixture filled every lane the desk has, each to its own limit.
    assert {
        ("asks", "review_requests", "items"),
        ("asks", "run_controls", "items"),
        ("asks", "worker_kill_requests", "items"),
        ("asks", "claim_handoffs", "items"),
        ("work", "delegations", "items"),
        ("work", "ready", "items"),
        ("work", "waiting_on_operator", "items"),
        ("work", "waiting_on_operator", "not_offered", "items"),
        ("work", "waiting_on", "items"),
        ("work", "leases", "items"),
        ("work", "leases", "lapsed"),
        ("signals", "notifications"),
    } <= set(full), sorted(full)
    maxima = filled["maxima"]
    assert desk["work"]["ready"]["total"] > len(desk["work"]["ready"]["items"])
    assert len(desk["work"]["ready"]["items"]) == maxima["ready"]
    assert len(desk["signals"]["notifications"]) == maxima["notifications"]
    assert len(desk["asks"]["review_requests"]["items"]) == maxima["review_requests"]
    assert len(desk["work"]["delegations"]["items"]) == maxima["delegations"]
    assert desk["identity"]["notebook"]["excerpt_truncated"] is True
    assert (
        desk["work"]["ready"]["items"][0]["issue"]["title"].__len__()
        == (maxima["title_chars"])
    )

    # One lane at a time: every other lane empty, the notebook absent.
    empty = copy.deepcopy(desk)
    for path in lanes:
        _put(empty, path, [])
    empty["identity"]["notebook"] = None
    sizes: dict[str, tuple[int, int]] = {}
    over: dict[str, int] = {}
    for path in [*full, ("identity", "notebook")]:
        variant = copy.deepcopy(empty)
        _put(variant, path, copy.deepcopy(_at(desk, path)))
        name = ".".join(path)
        size, answer = _call(_serve(variant), {})
        sizes[name] = (results.serialized_size(variant), size)
        if size > BUDGET:
            over[name] = size
            continue
        _useful(answer, variant, name)
    # All of them at once, through the same seat session.
    size, answer = _call(_serve(desk), {})
    sizes["all lanes"] = (results.serialized_size(desk), size)
    print(f"my_desk at lane maxima, (uncut, answered) characters: {sizes}")
    if size > BUDGET:
        over["all lanes"] = size
    assert over == {}, f"over the {BUDGET} budget: {over}"
    _useful(answer, desk, "all lanes")
    assert answer["_truncated"]
    assert mcp_server.DESK_READY_KEPT == READY_HEAD

    # Over the protocol, against the real server: the same, and the cuts read on.
    async def scene() -> None:
        server = mcp_server_for(app, sol, run_id="desk-max", profile="seat")
        async with mcp_sessions(server) as (session,):
            result = await session.call_tool("my_desk", {})
            (block,) = result.content
            assert not result.isError and len(block.text) <= BUDGET
            live = json.loads(block.text)
            ready = live["_truncated"]["work.ready.items"]
            assert ready["total_items"] == maxima["ready"]
            kept = len(live["work"]["ready"]["items"])
            assert live["work"]["ready"]["clipped"] is True
            assert ready["next"] == {
                "tool": "my_desk",
                "arguments": {"section": "work.ready.items", "offset": kept},
            }
            more = await session.call_tool("my_desk", ready["next"]["arguments"])
            (block,) = more.content
            assert not more.isError and len(block.text) <= BUDGET
            page = json.loads(block.text)
            assert page["section"] == "work.ready.items"
            assert page["offset"] == kept and page["total_items"] == maxima["ready"]
            assert (
                page["items"][0]["issue"]["id"]
                == (desk["work"]["ready"]["items"][kept]["issue"]["id"])
            )
            notebook = live["_truncated"]["identity.notebook.excerpt"]
            text = await session.call_tool("my_desk", notebook["next"]["arguments"])
            (block,) = text.content
            assert not text.isError and len(block.text) <= BUDGET
            read_on = json.loads(block.text)
            whole = desk["identity"]["notebook"]["excerpt"]
            assert read_on["total_chars"] == len(whole)
            assert (
                read_on["text"]
                == whole[notebook["next"]["arguments"]["offset"] :][
                    : len(read_on["text"])
                ]
            )

    run_agents(scene())


def test_a_section_that_is_not_in_the_desk_is_refused():
    desk = {"identity": {"id": 2}, "work": {"ready": {"items": [1, 2, 3]}}}
    server = _serve(desk)
    _size, page = _call(server, {"section": "work.ready.items", "offset": 1})
    assert page == {
        "section": "work.ready.items",
        "offset": 1,
        "total_items": 3,
        "items": [2, 3],
    }
    assert _call(server, {"section": "work.ready.items.0"})[1] == {
        "section": "work.ready.items.0",
        "value": 1,
    }
    with pytest.raises(Exception, match="no section"):
        _call(server, {"section": "work.nothing"})
