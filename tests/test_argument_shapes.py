"""Argument shapes an agent can read and refusals that show them (AD9, D31).

WHY: in docs/plans/reports/agent-day-2026-11-26.md (DAY-7) evidence was a list
of strings on yield_claim and a list of objects on complete_claim, the item
shape of results lived only in prose, and both wrong guesses were refused
"Input should be a valid list" with no expected shape and no next: two calls
wasted. No evidence kind fit "I ran this command".

Held here:
* every list-of-object argument publishes a typed item schema;
* a wrong shape for any of them is refused invalid_arguments with each bad
  field under violations, an example of the expected shape (which validates
  against the published schema) and next naming the same tool;
* complete_claim evidence takes {kind: command, command, outcome, scope}, with
  the credential guard; yield_claim evidence also takes that object form,
  stored as one line, and plain strings keep working.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
import jsonschema
from support import ADMIN, bearer, bootstrap, make_app, mcp_sessions, onboard_agent

from athena.mcp import server as mcp_server


class _NoServer:
    """Argument checks run before any request: no Athena is needed."""


def _object_item(schema: dict[str, Any]) -> bool:
    options = schema.get("anyOf", [schema])
    return any(option.get("type") == "object" for option in options)


def _lists_of_objects(tool) -> list[str]:
    found = []
    for name, schema in tool.inputSchema.get("properties", {}).items():
        options = schema.get("anyOf", [schema])
        for option in options:
            kind = option.get("type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "array" in kinds and _object_item(option.get("items", {})):
                found.append(name)
    return found


def _listed() -> dict[str, Any]:
    server = mcp_server.build_server(_NoServer(), scopes=None)
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def test_the_report_lists_and_yield_evidence_publish_their_item_shape():
    listed = _listed()
    found = {name: _lists_of_objects(tool) for name, tool in listed.items()}
    found = {name: args for name, args in found.items() if args}
    assert found == {
        "complete_claim": ["results", "evidence"],
        "yield_claim": ["evidence"],
    }
    results = listed["complete_claim"].inputSchema["properties"]["results"]
    item = results["items"]
    assert item["required"] == ["condition", "state", "note"]
    assert item["properties"]["state"]["enum"] == [
        "met",
        "not_met",
        "not_assessed",
        "inconclusive",
    ]


def test_a_wrong_shape_is_refused_with_the_field_an_example_and_next():
    listed = _listed()
    server = mcp_server.build_server(_NoServer(), scopes=None)
    cases = [
        (name, argument)
        for name, tool in listed.items()
        for argument in _lists_of_objects(tool)
    ]
    assert cases

    async def scene():
        answers = []
        async with mcp_sessions(server) as (session,):
            for name, argument in cases:
                required = {
                    "complete_claim": {"issue_id": 1, "generation": "0" * 32},
                    "yield_claim": {
                        "issue_id": 1,
                        "generation": "0" * 32,
                        "reason": "blocked",
                        "attempted_work": "a",
                        "evidence": [],
                        "blocking_question": "q",
                        "resume_instructions": "r",
                    },
                }[name]
                # An object where a list goes: the guess DAY-7 made.
                arguments = {**required, argument: {"kind": "command"}}
                result = await session.call_tool(name, arguments)
                assert result.isError, (name, argument)
                (block,) = result.content
                answers.append((name, argument, json.loads(block.text)))
        return answers

    for name, argument, refusal in asyncio.run(scene()):
        assert refusal["code"] == "invalid_arguments", refusal
        assert any(v["path"].startswith(argument) for v in refusal["violations"])
        assert refusal["next"]["tool"] == name
        schema = listed[name].inputSchema["properties"][argument]
        jsonschema.validate(refusal["examples"][argument], schema)
        # The rejected value is never repeated.
        assert '"command"' not in refusal["detail"]


def _claimed(client, agent, title):
    made = client.post(
        "/issues",
        json={"title": title, "assignee_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    tag = client.get(f"/issues/{issue_id}", headers=bearer(agent)).headers["ETag"]
    held = client.post(
        f"/issues/{issue_id}/claim", json={}, headers={**bearer(agent), "If-Match": tag}
    )
    assert held.status_code == 201, held.text
    return issue_id, held.json()["generation"]


COMMAND = {
    "kind": "command",
    "command": "pytest -q tests/test_argument_shapes.py",
    "outcome": "4 passed",
    "scope": "actual-run",
}


def test_complete_claim_takes_command_evidence_and_guards_credentials(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        issue_id, generation = _claimed(client, sol, "Command evidence")
        token = "gh" + "p_" + "aB3" * 12  # assembled at runtime
        refused = client.post(
            f"/issues/{issue_id}/complete",
            json={
                "generation": generation,
                "evidence": [{**COMMAND, "command": f"curl -H 'token {token}'"}],
            },
            headers=bearer(sol),
        )
        assert refused.status_code == 422, refused.text
        assert "evidence[0].command" in refused.text
        assert token not in refused.text
        missing = client.post(
            f"/issues/{issue_id}/complete",
            json={"generation": generation, "evidence": [{"kind": "command"}]},
            headers=bearer(sol),
        )
        assert missing.status_code == 422, missing.text
        done = client.post(
            f"/issues/{issue_id}/complete",
            json={"generation": generation, "evidence": [COMMAND]},
            headers=bearer(sol),
        )
        assert done.status_code == 200, done.text
        context = client.get(
            f"/issues/{issue_id}/work-context", headers=bearer(sol)
        ).json()
        assert COMMAND["command"] in json.dumps(context)


def test_yield_claim_takes_object_evidence_beside_strings(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        issue_id, generation = _claimed(client, sol, "Yield with evidence")
        body = {
            "generation": generation,
            "reason": "blocked",
            "attempted_work": "ran the suite",
            "blocking_question": "which fixture?",
            "resume_instructions": "pick the fixture and rerun",
        }
        refused = client.post(
            f"/issues/{issue_id}/yield",
            json={**body, "evidence": [{"kind": "command", "command": "x"}]},
            headers=bearer(sol),
        )
        assert refused.status_code == 422, refused.text
        yielded = client.post(
            f"/issues/{issue_id}/yield",
            json={**body, "evidence": ["log at /tmp/run.txt", COMMAND]},
            headers=bearer(sol),
        )
        assert yielded.status_code == 201, yielded.text
        handoff = yielded.json()
        assert handoff["evidence"] == [
            "log at /tmp/run.txt",
            "command (actual-run): pytest -q tests/test_argument_shapes.py => 4 passed",
        ]
