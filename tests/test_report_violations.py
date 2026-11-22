"""A completion report's problems arrive together, and its grammar is published (A9).

An agent used to learn one report error per round trip. The shared command now
collects every violation of the report grammar and refuses them together, each
by field path, in a structured ``violations`` list beside the usable ``detail``,
without repeating a rejected value. Validation stays in the command: REST and MCP
reach the same refusal. The grammar's closed sets are published, from the
command's own constants, in the OpenAPI document and in complete_claim's input
schema, as documentation only.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from athena.aegis import lease_commands
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server, tool_failure

HEADERS = {"X-Athena-Actor": "1", "X-Athena-Run": "report-violations-test"}
# A value no rule message could contain by accident: if it comes back, the
# refusal echoed what the caller sent.
SENTINEL = "SENTINEL-7Qx-not-a-word"

BROKEN = {
    "verdict": SENTINEL,
    "findings": {"P1": -1, SENTINEL: 2},
    "results": [
        {"condition": "", "state": SENTINEL, "note": "n"},
        {"condition": "c", "state": "met", "note": "n", SENTINEL: SENTINEL},
    ],
    "unfinished": ["   "],
    "evidence": [
        {"kind": "attachment", "source": "s", "ref": SENTINEL, "scope": SENTINEL}
    ],
    "usage": {"input_tokens": -1, SENTINEL: 1},
}
EXPECTED_PATHS = [
    "usage",
    "usage.input_tokens",
    "verdict",
    "findings",
    "findings.P1",
    "results[0].state",
    "results[0].condition",
    "results[1]",
    "unfinished[0]",
    "evidence[0].scope",
    "evidence[0].ref",
]


@pytest.fixture
def claimed(tmp_path):
    client = TestClient(create_app(tmp_path / "violations.sqlite"))
    client.__enter__()
    try:
        assert (
            client.post(
                "/users",
                json={"email": "r@example.com", "name": "Reporter", "password": "pw"},
            ).status_code
            == 201
        )
        client.headers.update(HEADERS)
        issue = client.post("/issues", json={"title": "Reported work"}).json()
        etag = client.get(f"/issues/{issue['id']}").headers["etag"]
        claim = client.post(
            f"/issues/{issue['id']}/claim", json={}, headers={"If-Match": etag}
        )
        assert claim.status_code == 201, claim.text
        yield client, issue, claim.json()["generation"]
    finally:
        client.__exit__(None, None, None)


def _paths(body: dict) -> list[str]:
    return [item["path"] for item in body["violations"]]


def test_every_report_violation_arrives_at_once_by_path_without_values(claimed):
    client, issue, generation = claimed
    refused = client.post(
        f"/issues/{issue['id']}/complete",
        json={"generation": generation, **BROKEN},
    )
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert body["code"] == "validation_error"
    assert _paths(body) == EXPECTED_PATHS
    assert all(set(item) == {"path", "message"} for item in body["violations"])
    # The detail stays a usable message that names every path too.
    assert body["detail"].startswith(
        f"the completion report has {len(EXPECTED_PATHS)} problems: "
    )
    assert all(path in body["detail"] for path in EXPECTED_PATHS)
    assert SENTINEL not in refused.text
    # Nothing was written and the lease is untouched, so the corrected report
    # completes on the same generation.
    lease = client.get(f"/issues/{issue['id']}/lease").json()
    assert lease["generation"] == generation and lease["active"] is True
    fixed = client.post(
        f"/issues/{issue['id']}/complete",
        json={
            "generation": generation,
            "verdict": "PASS",
            "results": [{"condition": "c", "state": "met", "note": ""}],
        },
    )
    assert fixed.status_code == 200, fixed.text


def test_one_violation_keeps_its_plain_detail_and_still_lists_it(claimed):
    client, issue, generation = claimed
    refused = client.post(
        f"/issues/{issue['id']}/complete",
        json={"generation": generation, "verdict": "pass"},
    )
    assert refused.status_code == 422
    assert refused.json() == {
        "detail": "verdict must be one of PASS, PASS-WITH-NOTES, HOLD, BLOCKED",
        "code": "validation_error",
        "violations": [
            {
                "path": "verdict",
                "message": "must be one of PASS, PASS-WITH-NOTES, HOLD, BLOCKED",
            }
        ],
    }


def test_mcp_returns_the_same_violations_as_rest(claimed):
    client, issue, generation = claimed
    rest = client.post(
        f"/issues/{issue['id']}/complete",
        json={"generation": generation, **BROKEN},
    ).json()
    server = build_server(AthenaClient(client=client))
    arguments = {"issue_id": issue["id"], "generation": generation, **BROKEN}
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("complete_claim", arguments))
    refusal = tool_failure(caught.value)
    assert refusal["status_code"] == 422
    assert refusal["code"] == "validation_error"
    assert [item["path"] for item in refusal["violations"]] == _paths(rest)
    assert SENTINEL not in json.dumps(refusal)


def test_the_closed_sets_are_published_from_the_commands_constants(tmp_path):
    closed = {
        "verdict": lease_commands.COMPLETION_VERDICTS,
        "findings": lease_commands.COMPLETION_FINDING_SEVERITIES,
        "results": lease_commands.COMPLETION_RESULT_KEYS
        + lease_commands.COMPLETION_RESULT_OPTIONAL
        + lease_commands.COMPLETION_RESULT_STATES,
        "evidence": lease_commands.COMPLETION_EVIDENCE_KEYS
        + lease_commands.COMPLETION_EVIDENCE_OPTIONAL
        + lease_commands.COMPLETION_EVIDENCE_KINDS
        + lease_commands.COMPLETION_EVIDENCE_SCOPES,
    }
    schema = create_app(tmp_path / "openapi.sqlite").openapi()
    body = schema["components"]["schemas"]["CompleteClaimIn"]["properties"]
    route = schema["paths"]["/issues/{issue_id}/complete"]["post"]
    assert "violations" in route["description"]
    tool = next(
        tool
        for tool in asyncio.run(build_server(None).list_tools())
        if tool.name == "complete_claim"
    )
    for field, words in closed.items():
        for published in (
            body[field]["description"],
            tool.inputSchema["properties"][field]["description"],
        ):
            assert all(word in published for word in words), (field, published)
    # Documentation only: the REST fields still accept any JSON so the command,
    # not FastAPI, refuses a bad report (and never echoes it).
    for field in closed:
        assert "type" not in body[field]
        assert body[field]["anyOf"] == [{}, {"type": "null"}]


@pytest.mark.parametrize(
    ("report", "paths"),
    [
        (
            {
                "results": [
                    {
                        "condition": "c",
                        "state": "met",
                        "note": "bell\x07",
                        "subject": ["", "x" * 201],
                    },
                    {"condition": "c", "state": "met", "note": "", "subject": []},
                ]
            },
            [
                "results[0].note",
                "results[0].subject[0]",
                "results[0].subject[1]",
                "results[1].subject",
            ],
        ),
        (
            {
                "evidence": [
                    {
                        "kind": SENTINEL,
                        "source": "\ud800",
                        "ref": "r",
                        "scope": "unrun",
                        "digest": SENTINEL,
                        "bytes": -1,
                        "locator": 7,
                    }
                ]
            },
            [
                "evidence[0].kind",
                "evidence[0].source",
                "evidence[0].digest",
                "evidence[0].bytes",
                "evidence[0].locator",
            ],
        ),
        ({"results": "all of it", "evidence": [SENTINEL]}, ["results", "evidence[0]"]),
        (
            {"usage": {"estimated_cost": 1.5, "reported_at": "2026-11-22"}},
            ["usage", "usage.reported_at"],
        ),
        ({"findings": {}}, ["findings"]),
    ],
)
def test_the_command_lists_each_rule_a_report_breaks(report, paths):
    from athena.aegis.issue_commands import IssueCommandError

    with pytest.raises(IssueCommandError) as caught:
        lease_commands._normalize_completion_report(**report)
    violations = caught.value.extra["violations"]
    assert [item["path"] for item in violations] == paths
    assert SENTINEL not in caught.value.detail + json.dumps(violations)


def test_an_oversized_whole_report_and_its_evidence_refs_are_named():
    from athena.aegis.issue_commands import IssueCommandError

    ref = "r" * lease_commands.MAX_HANDOFF_EVIDENCE_ITEM_CHARS
    item = {"kind": "file", "source": "s", "ref": ref, "scope": "unrun"}
    with pytest.raises(IssueCommandError) as caught:
        lease_commands._normalize_completion_report(evidence=[item] * 10)
    assert caught.value.extra["violations"] == [
        {
            "path": "evidence",
            "message": "refs must encode to at most 8000 characters together",
        }
    ]
    note = "n" * lease_commands.MAX_COMPLETION_TEXT_CHARS
    results = [{"condition": "c", "state": "met", "note": note}] * 20
    with pytest.raises(IssueCommandError) as caught:
        lease_commands._normalize_completion_report(results=results)
    assert caught.value.extra["violations"][0]["path"] == "$"
    assert caught.value.detail.startswith("the encoded reported completion must be")
