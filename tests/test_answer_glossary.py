"""Lean answers and the answer glossary (AD2, D29).

WHY: docs/plans/reports/agent-day-2026-11-26.md (DAY-1) measured a desk that
was 46% fixed explanation, with the work protocol in it twice, and a work
context with 1,242 bytes of null-valued keys. An agent paid for every byte on
every call. AD2 moves those texts into one document read once and serves the
desk and packet without them: the default on MCP, ``?lean=true`` on REST,
whose existing consumers keep the full answer by default.

These tests hold what makes that safe:

- REST answers in full by default, and ``?lean=true`` is exactly
  ``answer_glossary.lean`` of that answer, under the lean schema name and
  with the same ETag;
- every text a lean answer leaves out is the glossary's text for that path,
  every glossary entry is something an answer carries, and a lean answer has
  no explanation key and no null left anywhere, so a new ``meaning`` fails
  here until the glossary lists it;
- the glossary is served once, the same text, at ``GET /agent/glossary``, as
  docs/ANSWER_GLOSSARY.md, and as its packaged MCP copy.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import client_as

from athena import demo
from athena.aegis import answer_glossary
from athena.main import create_app

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_DOC = ROOT / "docs" / "ANSWER_GLOSSARY.md"
PACKAGED = ROOT / "src" / "athena" / "mcp" / "documents" / "ANSWER_GLOSSARY.md"


@pytest.fixture
def workspace(tmp_path, clock):
    """The demo seed, with Sol's notebook written so every desk text shows: Sol
    holds a lease (its progress block), has a notebook, and sits in the office."""
    seeded = demo.seed_demo(tmp_path / "lean.db", attach_dir=tmp_path / "attachments")
    app = create_app(Path(seeded["db_path"]))
    with TestClient(app) as anonymous:
        sol = client_as(app, token=seeded["agent_token"])
        assert sol.post("/desk/notebook").status_code == 201
        yield seeded, sol, anonymous


def _ok(response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def _routes(seeded: dict) -> dict[str, str]:
    issue = seeded["ids"]["command_issue"]
    return {
        "desk": "/desk",
        "packet": f"/issues/{issue}/work-context",
        "brief": f"/issues/{issue}/work-context?brief=true",
    }


def _with_lean(route: str) -> str:
    return f"{route}{'&' if '?' in route else '?'}lean=true"


def _keys_and_values(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}", key, item
            yield from _keys_and_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _keys_and_values(item, f"{path}[{index}]")


def test_rest_answers_in_full_by_default_and_lean_on_request(workspace):
    seeded, sol, _anonymous = workspace
    for name, route in _routes(seeded).items():
        full = sol.get(route)
        lean = sol.get(_with_lean(route))
        answer = _ok(full)
        shape = answer_glossary.SHAPES[answer["schema"]]
        assert _ok(lean) == answer_glossary.lean(answer), name
        assert lean.json()["schema"] == shape.lean_schema, name
        # The tag is the snapshot's, whichever shape carries it.
        assert lean.headers.get("ETag") == full.headers.get("ETag"), name
        assert lean.headers.get("Cache-Control") == full.headers.get("Cache-Control"), (
            name
        )
    desk = _ok(sol.get("/desk"))
    assert desk["schema"] == "athena.agent_desk.v2"
    assert _ok(sol.get("/desk?lean=false")) == desk
    assert _ok(sol.get("/desk?lean=true"))["schema"] == "athena.agent_desk.v3"
    packet = _ok(sol.get(_routes(seeded)["packet"]))
    assert packet["schema"] == "athena.issue_work_context.v1"
    assert "semantics" in packet and "protocol" in packet


def test_every_text_a_lean_answer_leaves_out_is_in_the_glossary(workspace):
    seeded, sol, _anonymous = workspace
    rendered = answer_glossary.render()
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for name, route in _routes(seeded).items():
        full = _ok(sol.get(route))
        for entry, value in answer_glossary.explanations(full):
            path = answer_glossary.dotted(entry.path)
            assert value == entry.text, (name, path)
            seen.add((full["schema"], entry.path))
            for _where, _key, leaf in _keys_and_values({"": value}):
                if isinstance(leaf, str) and leaf:
                    assert leaf in rendered, (name, path, leaf)
        lean = answer_glossary.lean(full)
        found = [
            (where, key, item)
            for where, key, item in _keys_and_values(lean)
            if key in answer_glossary.EXPLANATION_KEYS or item is None
        ]
        assert found == [], name
        if "how_to_claim" in lean:
            assert lean["how_to_claim"] == {
                "claimant_prerequisite": {
                    key: full["how_to_claim"]["claimant_prerequisite"][key]
                    for key in ("satisfied_at_read", "next_operation")
                }
            }
    # Every entry is a text some answer carries: the glossary lists no ghosts.
    listed = {
        (shape.schema, entry.path)
        for shape in answer_glossary.SHAPES.values()
        for entry in shape.explained
    }
    assert listed - seen == set()


def test_lean_leaves_the_full_answer_alone():
    answer = {
        "schema": answer_glossary.PACKET.schema,
        "semantics": {"snapshot": "current_visible_state"},
        "protocol": {"claim_one_issue": True},
        "issue": {"id": 1, "archived_at": None, "labels": [{"a": None}]},
    }
    before = copy.deepcopy(answer)
    assert answer_glossary.lean(answer) == {
        "schema": answer_glossary.PACKET.lean_schema,
        "issue": {"id": 1, "labels": [{}]},
    }
    assert answer == before


def test_the_glossary_is_served_once_and_is_the_same_text_everywhere(workspace):
    _seeded, sol, anonymous = workspace
    assert anonymous.get(answer_glossary.ROUTE).status_code == 401
    served = _ok(sol.get(answer_glossary.ROUTE))
    rendered = answer_glossary.render()
    assert served["schema"] == answer_glossary.SCHEMA
    assert served["uri"] == "athena://answers/glossary"
    assert served["markdown"] == rendered
    assert {answer["schema"]: answer["lean_of"] for answer in served["answers"]} == {
        "athena.agent_desk.v3": "athena.agent_desk.v2",
        "athena.issue_work_context.v2": "athena.issue_work_context.v1",
    }
    # docs/ is the copy a reader opens; the packaged copy is what athena-mcp
    # serves (tests/test_mcp_resources.py holds the two byte-identical).
    assert GLOSSARY_DOC.read_text(encoding="utf-8") == rendered, (
        "docs/ANSWER_GLOSSARY.md is stale: `python scripts/generate_agent_api.py`"
    )
    assert PACKAGED.read_text(encoding="utf-8") == rendered
