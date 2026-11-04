"""Claim release can carry an honest report without certifying the work."""

import asyncio
import copy
import csv
from io import StringIO
import json
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from athena.aegis import lease_commands, work_context
from athena.core import db, activity_chain, portability
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server


HEADERS = {"X-Athena-Actor": "1", "X-Athena-Run": "completion-report-test"}
REPORT = {
    "results": [
        {"condition": "source reviewed", "state": "met", "note": "read original bytes"},
        {"condition": "integration", "state": "not_met", "note": "still fails"},
        {"condition": "production", "state": "not_assessed", "note": "not run"},
        {"condition": "timing", "state": "inconclusive", "note": "needs more evidence"},
    ],
    "unfinished": ["  retain this exact whitespace  "],
    "uncertainty": ["The source-only read does not prove execution."],
    "evidence": [
        {
            "kind": "file",
            "source": "checkout@abc123",
            "ref": "src/example.py:4",
            "scope": "source-only",
        },
        {
            "kind": "manifest",
            "source": "synthetic fixture",
            "ref": "checks.json",
            "scope": "unrun",
        },
    ],
}


def _claimed(client, *, project_id=None):
    issue = client.post(
        "/issues",
        json={"title": "Reported work", "project_id": project_id},
        headers=HEADERS,
    ).json()
    read = client.get(f"/issues/{issue['id']}", headers=HEADERS)
    response = client.post(
        f"/issues/{issue['id']}/claim",
        json={},
        headers={**HEADERS, "If-Match": read.headers["etag"]},
    )
    assert response.status_code == 201, response.text
    return issue, response.json()["generation"]


def _client(tmp_path):
    client = TestClient(create_app(tmp_path / "completion.sqlite"))
    client.__enter__()
    assert (
        client.post(
            "/users",
            json={"email": "report@example.com", "name": "Reporter", "password": "pw"},
        ).status_code
        == 201
    )
    return client


def _completion_events(client, issue_id):
    return client.get(
        f"/activity?target_kind=issue&target_id={issue_id}&verb=claim_completed",
        headers=HEADERS,
    ).json()


def test_reported_failure_and_unrun_evidence_release_the_claim_and_survive_context(
    tmp_path,
):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        response = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": generation, **REPORT},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "released": True,
            "issue_id": issue["id"],
            "issue_status": "open",
            "issue_still_open": True,
            "lapsed": False,
            "next": "complete_claim released the lease only; issue status is unchanged",
        }
        events = client.get(
            f"/activity?target_kind=issue&target_id={issue['id']}&verb=claim_completed",
            headers=HEADERS,
        ).json()
        assert len(events) == 1
        detail = events[0]["detail"]
        assert detail.startswith(f"generation {generation}; reported_completion_v1 ")
        assert json.loads(detail.split("; reported_completion_v1 ", 1)[1]) == REPORT
        context = client.get(
            f"/issues/{issue['id']}/work-context", headers=HEADERS
        ).json()
        completed = next(
            row
            for row in context["recent_activity"]["items"]
            if row["verb"] == "claim_completed"
        )
        assert completed["reported_completion"] == REPORT
        assert (
            client.get(f"/issues/{issue['id']}", headers=HEADERS).json()["status"]
            == "open"
        )
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "fields",
    [{}, {"results": None, "unfinished": None, "uncertainty": None, "evidence": None}],
)
def test_absent_report_preserves_legacy_detail_and_response(tmp_path, fields):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        result = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": generation, **fields},
            headers=HEADERS,
        )
        assert result.status_code == 200
        expected = {
            "released": True,
            "issue_id": issue["id"],
            "issue_status": "open",
            "issue_still_open": True,
            "lapsed": False,
            "next": "complete_claim released the lease only; issue status is unchanged",
        }
        assert result.json() == expected
        assert result.content == json.dumps(expected, separators=(",", ":")).encode(
            "utf-8"
        )
        assert (
            _completion_events(client, issue["id"])[0]["detail"]
            == f"generation {generation}"
        )
    finally:
        client.__exit__(None, None, None)


INVALID_REPORTS = [
    {"results": "not a list"},
    {"results": [{}]},
    {"results": [{"condition": "x", "state": "passed", "note": ""}]},
    {"results": [{"condition": "x", "state": ["met"], "note": ""}]},
    {"results": [{"condition": " ", "state": "met", "note": ""}]},
    {"results": [{"condition": "x" * 501, "state": "met", "note": ""}]},
    {"results": [{"condition": "x", "state": "met", "note": "x" * 501}]},
    {"results": [{"condition": "x", "state": "met", "note": "", "success": True}]},
    {"results": [{"condition": "x", "state": "met", "note": ""}] * 21},
    {"unfinished": ["x"] * 11},
    {"unfinished": ["x" * 501]},
    {"unfinished": ["bad\x00control"]},
    {"unfinished": [False]},
    {"uncertainty": "unknown"},
    {"uncertainty": [" "]},
    {"uncertainty": ["\ud800"]},
    {"evidence": ["untyped"]},
    {"evidence": [{"kind": "file", "ref": "x", "scope": "unrun"}]},
    {"evidence": [{"kind": "url", "source": "x", "ref": "x", "scope": "unrun"}]},
    {"evidence": [{"kind": "file", "source": "x", "ref": "x", "scope": "verified"}]},
    {"evidence": [{"kind": "file", "source": "x" * 201, "ref": "x", "scope": "unrun"}]},
    {
        "evidence": [
            {"kind": "file", "source": "x", "ref": "x" * 1001, "scope": "unrun"}
        ]
    },
    {"evidence": [REPORT["evidence"][0]] * 11},
    {
        "evidence": [
            {"kind": "file", "source": "x", "ref": "x" * 1000, "scope": "unrun"}
        ]
        * 9
    },
    {
        "results": [
            {"condition": "c" * 300, "state": "inconclusive", "note": "n" * 300}
        ]
        * 20
    },
]


@pytest.mark.parametrize("fields", INVALID_REPORTS)
def test_invalid_or_oversized_report_is_atomic_and_can_be_retried_honestly(
    tmp_path, fields
):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        refused = client.post(
            f"/issues/{issue['id']}/complete",
            content=json.dumps({"generation": generation, **fields}).encode("utf-8"),
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        assert refused.status_code == 422, refused.text
        assert _completion_events(client, issue["id"]) == []
        assert (
            client.get(f"/issues/{issue['id']}/lease", headers=HEADERS).json()[
                "generation"
            ]
            == generation
        )
        accepted = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": generation, **REPORT},
            headers=HEADERS,
        )
        assert accepted.status_code == 200, accepted.text
        assert len(_completion_events(client, issue["id"])) == 1
    finally:
        client.__exit__(None, None, None)


def test_report_never_bypasses_stale_generation_or_changes_an_approval(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        stale = "0" * 32 if generation != "0" * 32 else "1" * 32
        bad = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": stale, **REPORT},
            headers=HEADERS,
        )
        assert bad.status_code == 409
        assert _completion_events(client, issue["id"]) == []
        assert (
            client.post(
                f"/issues/{issue['id']}/complete",
                json={"generation": generation, **REPORT},
                headers=HEADERS,
            ).status_code
            == 200
        )

        agent = client.post(
            "/users",
            json={
                "email": "agent@example.com",
                "name": "Agent",
                "password": "pw",
                "is_agent": True,
            },
            headers=HEADERS,
        ).json()
        actor_headers = {"X-Athena-Actor": str(agent["id"])}
        gated = client.post(
            "/issues", json={"title": "Gated work"}, headers=actor_headers
        ).json()
        assigned = client.put(
            f"/issues/{gated['id']}/assignee",
            json={"assignee_id": agent["id"]},
            headers=actor_headers,
        )
        assert assigned.status_code == 200, assigned.text
        read = client.get(f"/issues/{gated['id']}", headers=actor_headers)
        claim = client.post(
            f"/issues/{gated['id']}/claim",
            json={},
            headers={**actor_headers, "If-Match": read.headers["etag"]},
        )
        assert claim.status_code == 201
        assert (
            client.put(
                f"/approvals/policies/{agent['id']}",
                json={"action_kind": "issue.close"},
                headers=HEADERS,
            ).status_code
            == 200
        )
        closing = client.patch(
            f"/issues/{gated['id']}", json={"status": "done"}, headers=actor_headers
        )
        assert closing.status_code == 202, closing.text
        assert closing.json()["code"] == "approval_required"
        gates = client.get("/approvals", headers=HEADERS).json()
        policies = client.get(
            f"/approvals/policies/{agent['id']}", headers=HEADERS
        ).json()
        result = client.post(
            f"/issues/{gated['id']}/complete",
            json={"generation": claim.json()["generation"], **REPORT},
            headers=actor_headers,
        )
        assert result.status_code == 200, result.text
        assert client.get("/approvals", headers=HEADERS).json() == gates
        assert (
            client.get(f"/approvals/policies/{agent['id']}", headers=HEADERS).json()
            == policies
        )
        assert (
            client.get(f"/issues/{gated['id']}", headers=actor_headers).json()["status"]
            == "open"
        )
    finally:
        client.__exit__(None, None, None)


def test_render_replay_export_and_mcp_preserve_report_without_executing_refs(tmp_path):
    client = _client(tmp_path)
    try:
        client.headers.update(HEADERS)
        project = client.post(
            "/projects", json={"name": "Reports", "key": "RPT"}, headers=HEADERS
        ).json()
        issue, generation = _claimed(client, project_id=project["id"])
        hostile = copy.deepcopy(REPORT)
        hostile["results"][0]["note"] = '<img src=x onerror="alert(1)">'
        hostile["evidence"][0]["ref"] = "javascript:alert(1)<script>bad</script>"
        mcp = build_server(AthenaClient(client=client))
        result = asyncio.run(
            mcp.call_tool(
                "complete_claim",
                {"issue_id": issue["id"], "generation": generation, **hostile},
            )
        )
        body = json.loads(result[0].text)
        assert body["released"] is True
        detail = _completion_events(client, issue["id"])[0]["detail"]
        for url in (
            f"/aegis/issues/{issue['id']}/history",
            f"/aegis/issues/{issue['id']}/work-context",
        ):
            page = client.get(url, headers=HEADERS)
            assert page.status_code == 200, page.text
            assert "Reported completion (advisory)" in page.text
            assert "&lt;img" in page.text and "&lt;script&gt;" in page.text
            assert (
                "<img src=x" not in page.text and 'href="javascript:' not in page.text
            )
        replay = client.get(
            "/activity/runs/completion-report-test/replay", headers=HEADERS
        )
        assert replay.status_code == 200, replay.text
        assert any(row["detail"] == detail for row in replay.json()["events"])
        exported = client.get(
            f"/activity/export.csv?target_kind=issue&target_id={issue['id']}",
            headers=HEADERS,
        )
        assert any(
            row["detail"] == detail for row in csv.DictReader(StringIO(exported.text))
        )
        conn = db.connect(tmp_path / "completion.sqlite")
        try:
            assert activity_chain.verify_all(conn)["ok"] is True
            event = dict(
                conn.execute(
                    "SELECT * FROM activity WHERE verb='claim_completed'"
                ).fetchone()
            )
            assert lease_commands.reported_completion(event) == hostile
            bundle = portability.export_bundle(conn, "project", project["id"])
            assert any(row["detail"] == detail for row in bundle["activity"])
            event["imported_at"] = "2030-01-01 00:00:00"
            assert lease_commands.reported_completion(event) is None
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def test_bounded_context_keeps_full_report_even_when_the_raw_detail_excerpt_clips(
    tmp_path,
):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        report = copy.deepcopy(REPORT)
        report["results"][0]["note"] = "n" * lease_commands.MAX_COMPLETION_TEXT_CHARS
        report["unfinished"] = ["u" * 200] * lease_commands.MAX_COMPLETION_TEXT_ITEMS
        for index in range(16):
            if index:
                read = client.get(f"/issues/{issue['id']}", headers=HEADERS)
                claim = client.post(
                    f"/issues/{issue['id']}/claim",
                    json={},
                    headers={**HEADERS, "If-Match": read.headers["etag"]},
                )
                assert claim.status_code == 201
                generation = claim.json()["generation"]
            completed = client.post(
                f"/issues/{issue['id']}/complete",
                json={"generation": generation, **report},
                headers=HEADERS,
            )
            assert completed.status_code == 200, completed.text
        context = client.get(
            f"/issues/{issue['id']}/work-context", headers=HEADERS
        ).json()
        recent = context["recent_activity"]
        assert recent["clipped"] is True
        assert len(recent["items"]) == work_context.ACTIVITY_LIMIT
        for row in recent["items"]:
            if row["verb"] == "claim_completed":
                assert row["detail"]["truncated"] is True
                assert row["reported_completion"] == report
                assert (
                    len(
                        json.dumps(
                            report,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    <= lease_commands.MAX_COMPLETION_REPORT_JSON_CHARS
                )
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("scope", ["source-only", "synthetic", "actual-run", "unrun"])
def test_each_reported_scope_is_data_and_unicode_is_preserved(tmp_path, scope):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        report = {
            "evidence": [
                {
                    "kind": "doc",
                    "source": "fixture@revision",
                    "ref": "例/λ.txt:1",
                    "scope": scope,
                }
            ]
        }
        response = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": generation, **report},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        detail = _completion_events(client, issue["id"])[0]["detail"]
        assert json.loads(detail.split("; reported_completion_v1 ", 1)[1]) == report
    finally:
        client.__exit__(None, None, None)


def test_mcp_and_rest_use_the_same_report_refusal_and_keep_the_claim(tmp_path):
    from mcp.server.fastmcp.exceptions import ToolError

    client = _client(tmp_path)
    try:
        client.headers.update(HEADERS)
        issue, generation = _claimed(client)
        bad_report = {
            "evidence": [
                {
                    "kind": "file",
                    "source": "fixture",
                    "ref": "missing-original.json",
                    "scope": "verified",
                }
            ]
        }
        rest = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": generation, **bad_report},
            headers=HEADERS,
        )
        assert rest.status_code == 422
        mcp = build_server(AthenaClient(client=client))
        with pytest.raises(ToolError) as error:
            asyncio.run(
                mcp.call_tool(
                    "complete_claim",
                    {"issue_id": issue["id"], "generation": generation, **bad_report},
                )
            )
        parsed = json.loads(str(error.value).split("ATHENA_ERROR_JSON=", 1)[1])
        assert parsed["status_code"] == 422
        assert parsed["detail"] == rest.json()["detail"]
        assert parsed["code"] == rest.json().get("code")
        assert _completion_events(client, issue["id"]) == []
        accepted = asyncio.run(
            mcp.call_tool(
                "complete_claim",
                {"issue_id": issue["id"], "generation": generation, **REPORT},
            )
        )
        assert json.loads(accepted[0].text)["released"] is True
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("report", "labels", "expanded"),
    [
        (
            REPORT,
            [
                "1 reported met",
                "1 reported not_met",
                "1 reported not_assessed",
                "1 reported inconclusive",
                "1 reported unfinished",
                "1 reported uncertainty",
            ],
            True,
        ),
        (
            {"results": [REPORT["results"][0]] * 2},
            ["2 reported met"],
            False,
        ),
        *[
            (
                {"results": [{"condition": "check", "state": state, "note": ""}]},
                [f"1 reported {state}"],
                True,
            )
            for state in ("not_met", "not_assessed", "inconclusive")
        ],
        ({"unfinished": ["Still owed"]}, ["1 reported unfinished"], True),
        ({"uncertainty": ["Unknown"]}, ["1 reported uncertainty"], True),
        ({"results": [], "unfinished": [], "uncertainty": []}, [], False),
    ],
    ids=[
        "mixed",
        "met-only",
        "not-met",
        "not-assessed",
        "inconclusive",
        "unfinished",
        "uncertainty",
        "empty",
    ],
)
def test_reported_counts_are_visible_when_collapsed_and_residuals_expand(
    tmp_path, report, labels, expanded
):
    report = copy.deepcopy(report)
    if report.get("results"):
        report["results"][0]["condition"] = (
            '<img src=x onerror="alert(1)"> pass verified approved'
        )
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        completed = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": generation, **report},
            headers=HEADERS,
        )
        assert completed.status_code == 200, completed.text
        narrative = client.get(f"/issues/{issue['id']}/history", headers=HEADERS)
        assert narrative.status_code == 200, narrative.text
        line = next(
            item["summary"]
            for item in narrative.json()["items"]
            if item["state"] == "claim_completed"
        )
        for view in ("history", "work-context"):
            page = client.get(f"/aegis/issues/{issue['id']}/{view}", headers=HEADERS)
            assert page.status_code == 200, page.text
            if report.get("results"):
                assert "&lt;img" in page.text
                assert '<img src=x onerror="alert(1)">' not in page.text
            summary = re.search(
                r"<details(?P<attrs>[^>]*)>\s*<summary>(?P<text>Reported completion \(advisory\).*?)</summary>",
                page.text,
                re.DOTALL,
            )
            assert summary is not None
            text = " ".join(summary["text"].split())
            for label in labels:
                assert label in text
            assert bool(re.search(r"\bopen(?:\s|=|$)", summary["attrs"])) is expanded
            assert all(
                word not in text.lower() for word in ("pass", "verified", "approved")
            )
            assert "<" not in text
        for label in labels:
            assert label in line
        assert "advisory" in line
    finally:
        client.__exit__(None, None, None)


def test_registered_tool_and_agent_docs_define_unperformed_and_inconclusive(tmp_path):
    client = _client(tmp_path)
    try:
        server = build_server(AthenaClient(client=client))
        description = next(
            tool.description
            for tool in asyncio.run(server.list_tools())
            if tool.name == "complete_claim"
        )
        root = Path(__file__).resolve().parents[1]
        sources = [description] + [
            (root / path).read_text()
            for path in (
                "docs/CLOSEOUT_RECIPE.md",
                "src/athena/field_guide/02_claiming_work.md",
                "docs/AGENT_API.md",
            )
        ]
        for source in sources:
            text = " ".join(source.replace("`", "").split())
            assert "not_assessed means the condition was not evaluated" in text
            assert "integration check was never run" in text
            assert (
                "inconclusive means an evaluation was attempted but did not establish met or not_met"
                in text
            )
            assert (
                "integration check ran but timed out before producing a result" in text
            )
    finally:
        client.__exit__(None, None, None)
