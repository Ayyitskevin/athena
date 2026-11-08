"""brief=true: the issue packet without the protocol the agent already read.

WHY: the work-context packet carries the same protocol, how-to-claim and runbook
blocks on every call, about 1.4k tokens even for an empty issue, and a seat reads
it at least twice per issue. `brief=true` keeps what a decision needs (the issue,
its ETag, its dependencies, its warnings) and drops the rest. It is a projection
of the full packet, built from the same snapshot, so a retained value can never
differ from the full packet's and a hidden issue stays hidden. The default is
byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from athena.aegis import work_context
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

H_ADMIN = {"X-Athena-Actor": "1"}
H_OUTSIDER = {"X-Athena-Actor": "2"}
FULL_KEYS = frozenset(
    {
        "schema",
        "scope",
        "semantics",
        "issue",
        "issue_etag",
        "seat_slug",
        "protocol",
        "how_to_claim",
        "runbook",
        "complete_does_not_close_issue",
        "warnings",
        "hierarchy",
        "dependencies",
        "contributors",
        "attachments",
        "comments",
        "references",
        "related",
        "recent_activity",
        "claim_handoffs",
    }
)
BRIEF_KEYS = frozenset(
    {"schema", "scope", "brief", "issue", "issue_etag", "dependencies", "warnings"}
)
# The blocks that repeat on every call and say nothing about this issue.
PROTOCOL_BLOCKS = frozenset(
    {
        "semantics",
        "protocol",
        "how_to_claim",
        "runbook",
        "complete_does_not_close_issue",
    }
)


@pytest.fixture
def board(tmp_path):
    app = create_app(tmp_path / "brief.db")
    with TestClient(app) as tc:
        tc.post("/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"})
        tc.post(
            "/users",
            json={
                "email": "out@e.com",
                "name": "Out",
                "password": "pw",
                "role": "member",
            },
            headers=H_ADMIN,
        )
        tc.headers.update(H_ADMIN)
        api = AthenaClient(client=tc)
        project = tc.post("/projects", json={"name": "Brief", "key": "BRF"})
        assert project.status_code == 201, project.text
        project = project.json()
        issue = tc.post(
            "/issues",
            json={
                "title": "compact the packet",
                "body": "The full packet repeats itself.",
                "project_id": project["id"],
            },
        ).json()
        blocker = tc.post(
            "/issues", json={"title": "the blocker", "project_id": project["id"]}
        ).json()
        api.link_issues(issue["id"], str(blocker["id"]), "blocked_by")
        api.comment_on_issue(issue["id"], "a comment the brief packet does not carry")
        private = tc.post("/projects", json={"name": "Private", "key": "PRV"})
        assert private.status_code == 201, private.text
        private = private.json()
        made_private = tc.put(
            f"/projects/{private['id']}/visibility", json={"visibility": "private"}
        )
        assert made_private.status_code == 200, made_private.text
        hidden = tc.post(
            "/issues", json={"title": "not for outsiders", "project_id": private["id"]}
        ).json()
        yield {
            "tc": tc,
            "app": app,
            "issue": issue,
            "blocker": blocker,
            "hidden": hidden,
        }


def _get(tc, ref, *, brief=None, headers=None):
    params = {} if brief is None else {"brief": "true" if brief else "false"}
    return tc.get(f"/issues/{ref}/work-context", params=params, headers=headers)


def test_brief_keeps_the_issue_its_etag_dependencies_and_warnings_and_drops_the_rest(
    board,
):
    full = _get(board["tc"], board["issue"]["id"]).json()
    brief = _get(board["tc"], board["issue"]["id"], brief=True).json()
    assert set(brief) == BRIEF_KEYS
    assert brief["brief"] is True
    for key in ("schema", "scope", "issue", "issue_etag", "dependencies", "warnings"):
        assert brief[key] == full[key], key
    # The retained values are real, not empty shells.
    assert full["dependencies"]["blocked_by"]["visible_total"] == 1
    assert "visible_open_blockers" in full["warnings"]
    assert full["comments"]["visible_total"] == 1  # present in full, absent in brief
    dropped = set(full) - set(brief)
    assert PROTOCOL_BLOCKS <= dropped
    assert dropped == FULL_KEYS - BRIEF_KEYS


def test_the_default_packet_is_unchanged(board):
    default = _get(board["tc"], board["issue"]["id"])
    explicit = _get(board["tc"], board["issue"]["id"], brief=False)
    assert set(default.json()) == FULL_KEYS
    assert "brief" not in default.json()
    assert default.json() == explicit.json()
    assert default.headers["etag"] == work_context.context_etag(default.json())


def test_brief_is_its_own_representation_with_its_own_validator(board):
    full = _get(board["tc"], board["issue"]["id"])
    brief = _get(board["tc"], board["issue"]["id"], brief=True)
    assert brief.headers["etag"] == work_context.context_etag(brief.json())
    assert brief.headers["etag"] != full.headers["etag"]
    assert brief.json()["issue_etag"] == full.json()["issue_etag"]
    assert brief.headers["cache-control"] == full.headers["cache-control"]


def test_brief_hides_exactly_what_the_full_packet_hides(board):
    # A hidden issue is indistinguishable from a missing one on both paths.
    hidden = board["hidden"]["id"]
    for brief in (None, True):
        refused = _get(board["tc"], hidden, brief=brief, headers=H_OUTSIDER)
        assert refused.status_code == 404, refused.text
        assert refused.json()["detail"] == "no such issue"
    # Positive control: the same two reads answer the admin.
    for brief in (None, True):
        assert _get(board["tc"], hidden, brief=brief).status_code == 200


def test_the_brief_packet_is_well_under_half_the_full_one(board):
    full = _get(board["tc"], board["issue"]["id"]).json()
    brief = _get(board["tc"], board["issue"]["id"], brief=True).json()
    assert len(json.dumps(brief)) * 2 < len(json.dumps(full))


class _Recording:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {}

        return record


def test_the_mcp_tool_passes_brief_only_when_asked():
    client = _Recording()
    server = mcp_server.build_server(client)
    asyncio.run(server.call_tool("get_issue_work_context", {"ref": "ATH-7"}))
    asyncio.run(
        server.call_tool("get_issue_work_context", {"issue_id": 7, "brief": True})
    )
    assert client.calls == [
        ("get_issue_work_context", ("ATH-7",), {}),
        ("get_issue_work_context", ("7",), {"brief": True}),
    ]


def test_the_mcp_tool_returns_the_brief_packet_over_the_real_app(board):
    server = mcp_server.build_server(AthenaClient(client=board["tc"]))
    result = asyncio.run(
        server.call_tool(
            "get_issue_work_context", {"issue_id": board["issue"]["id"], "brief": True}
        )
    )
    packet = json.loads(result[0].text)
    assert set(packet) == BRIEF_KEYS | {"_etag"}
    assert packet["issue"]["id"] == board["issue"]["id"]
    description = next(
        tool.description
        for tool in asyncio.run(server.list_tools())
        if tool.name == "get_issue_work_context"
    )
    assert "brief" in description
