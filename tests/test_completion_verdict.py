"""A completion report may carry the reporter's own one-word result and severity counts.

Both fields are optional, a closed vocabulary, and a fact the reporter asserts: Athena does not
verify them, compute a score from them, or let them change status, approval or release.
"""

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from athena.aegis import lease_commands
from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server

HEADERS = {"X-Athena-Actor": "1", "X-Athena-Run": "completion-verdict-test"}
RESULTS = [{"condition": "read the diff", "state": "met", "note": "all of it"}]
FULL = {
    "verdict": "PASS-WITH-NOTES",
    "findings": {"P1": 0, "P2": 2, "P3": 3},
    "results": RESULTS,
    "unfinished": ["one follow-up"],
}


def _client(tmp_path):
    client = TestClient(create_app(tmp_path / "verdict.sqlite"))
    client.__enter__()
    assert (
        client.post(
            "/users",
            json={"email": "v@example.com", "name": "Verdict", "password": "pw"},
        ).status_code
        == 201
    )
    return client


def _claimed(client):
    issue = client.post(
        "/issues", json={"title": "Reviewed work"}, headers=HEADERS
    ).json()
    read = client.get(f"/issues/{issue['id']}", headers=HEADERS)
    response = client.post(
        f"/issues/{issue['id']}/claim",
        json={},
        headers={**HEADERS, "If-Match": read.headers["etag"]},
    )
    assert response.status_code == 201, response.text
    return issue, response.json()["generation"]


def _events(client, issue_id):
    return client.get(
        f"/activity?target_kind=issue&target_id={issue_id}&verb=claim_completed",
        headers=HEADERS,
    ).json()


def _complete(client, issue, generation, **fields):
    return client.post(
        f"/issues/{issue['id']}/complete",
        content=json.dumps({"generation": generation, **fields}).encode("utf-8"),
        headers={**HEADERS, "Content-Type": "application/json"},
    )


def test_verdict_and_counts_round_trip_exactly_and_render_labelled_as_reported(
    tmp_path,
):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        response = _complete(client, issue, generation, **FULL)
        assert response.status_code == 200, response.text
        # The release itself is unchanged: the lease goes, the status does not.
        assert response.json()["issue_still_open"] is True

        detail = _events(client, issue["id"])[0]["detail"]
        assert json.loads(detail.split("; reported_completion_v1 ", 1)[1]) == FULL

        context = client.get(
            f"/issues/{issue['id']}/work-context", headers=HEADERS
        ).json()
        completed = next(
            r
            for r in context["recent_activity"]["items"]
            if r["verb"] == "claim_completed"
        )
        assert completed["reported_completion"] == FULL

        history = client.get(f"/issues/{issue['id']}/history", headers=HEADERS).json()
        item = next(i for i in history["items"] if i["state"] == "claim_completed")
        assert item["reported_completion"] == FULL
        assert "reported verdict PASS-WITH-NOTES" in item["summary"]
        assert "reported findings P1 0, P2 2, P3 3" in item["summary"]
        assert "advisory" in item["summary"]

        for view in ("history", "work-context"):
            page = client.get(f"/aegis/issues/{issue['id']}/{view}", headers=HEADERS)
            assert page.status_code == 200, page.text
            summary = re.search(
                r"<summary>(Reported completion \(advisory\).*?)</summary>",
                page.text,
                re.DOTALL,
            )
            assert summary is not None
            text = " ".join(summary[1].split())
            assert "reported verdict PASS-WITH-NOTES" in text
            assert "reported findings P1 0, P2 2, P3 3" in text
            assert "Reported by the actor" in page.text
        # A verdict never changes the issue or the lease.
        assert (
            client.get(f"/issues/{issue['id']}", headers=HEADERS).json()["status"]
            == "open"
        )
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "fields",
    [
        {"verdict": "HOLD"},
        {"findings": {"P2": 1}},
        {"verdict": "BLOCKED", "findings": {"P1": 1}},
    ],
    ids=["verdict-only", "partial-findings-only", "both"],
)
def test_either_field_alone_is_a_valid_report(tmp_path, fields):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        assert _complete(client, issue, generation, **fields).status_code == 200
        detail = _events(client, issue["id"])[0]["detail"]
        assert json.loads(detail.split("; reported_completion_v1 ", 1)[1]) == fields
    finally:
        client.__exit__(None, None, None)


def test_a_report_without_them_is_stored_and_shown_exactly_as_before(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        old = {"results": RESULTS, "unfinished": ["x"]}
        assert _complete(client, issue, generation, **old).status_code == 200
        detail = _events(client, issue["id"])[0]["detail"]
        stored = json.loads(detail.split("; reported_completion_v1 ", 1)[1])
        assert stored == old and "verdict" not in stored and "findings" not in stored
        history = client.get(f"/issues/{issue['id']}/history", headers=HEADERS).json()
        item = next(i for i in history["items"] if i["state"] == "claim_completed")
        assert "verdict" not in item["summary"] and "findings" not in item["summary"]
        assert "1 reported met" in item["summary"]
    finally:
        client.__exit__(None, None, None)


REFUSED = [
    ({"verdict": "pass"}, "verdict"),
    ({"verdict": "APPROVED"}, "verdict"),
    ({"verdict": "PASS "}, "verdict"),
    ({"verdict": ""}, "verdict"),
    ({"verdict": 1}, "verdict"),
    ({"verdict": ["PASS"]}, "verdict"),
    ({"findings": {"P4": 1}}, "findings"),
    ({"findings": {"p1": 1}}, "findings"),
    ({"findings": {"critical": 1}}, "findings"),
    ({"findings": {"P1": -1}}, "findings.P1"),
    ({"findings": {"P2": 1.5}}, "findings.P2"),
    ({"findings": {"P3": "2"}}, "findings.P3"),
    ({"findings": {"P1": True}}, "findings.P1"),
    ({"findings": {"P1": None}}, "findings.P1"),
    ({"findings": {}}, "findings"),
    ({"findings": [1, 2]}, "findings"),
    ({"findings": "P1"}, "findings"),
]


@pytest.mark.parametrize("fields,named", REFUSED)
def test_unknown_words_keys_and_bad_counts_are_refused_atomically_then_retry_works(
    tmp_path, fields, named
):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        refused = _complete(client, issue, generation, **fields)
        assert refused.status_code == 422, refused.text
        assert named in refused.text
        # Nothing was written and the lease is untouched.
        assert _events(client, issue["id"]) == []
        assert (
            client.get(f"/issues/{issue['id']}/lease", headers=HEADERS).json()[
                "generation"
            ]
            == generation
        )
        # Positive control: the corrected report is accepted on the same lease.
        assert _complete(client, issue, generation, **FULL).status_code == 200
        assert len(_events(client, issue["id"])) == 1
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("word", ["PASS", "PASS-WITH-NOTES", "HOLD", "BLOCKED"])
def test_every_documented_verdict_word_is_accepted(tmp_path, word):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        assert _complete(client, issue, generation, verdict=word).status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_the_credential_refusal_still_applies_when_a_verdict_is_present(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        token = (
            "gh" + "p_" + "aB3" * 12
        )  # assembled at runtime, never a checked-in token
        leaky = [{"condition": "c", "state": "met", "note": f"used {token}"}]
        refused = _complete(client, issue, generation, verdict="PASS", results=leaky)
        assert refused.status_code == 422, refused.text
        assert "GitHub token" in refused.text and "results[0].note" in refused.text
        assert _events(client, issue["id"]) == []
        clean = _complete(client, issue, generation, verdict="PASS", results=RESULTS)
        assert clean.status_code == 200, clean.text
    finally:
        client.__exit__(None, None, None)


def test_mcp_accepts_and_refuses_the_same_way_as_rest(tmp_path):
    client = _client(tmp_path)
    try:
        client.headers.update(HEADERS)
        issue, generation = _claimed(client)
        server = build_server(AthenaClient(client=client))
        description = next(
            t.description
            for t in asyncio.run(server.list_tools())
            if t.name == "complete_claim"
        )
        assert "verdict" in description and "findings" in description
        with pytest.raises(Exception) as bad:
            asyncio.run(
                server.call_tool(
                    "complete_claim",
                    {
                        "issue_id": issue["id"],
                        "generation": generation,
                        "verdict": "pass",
                    },
                )
            )
        assert "verdict" in str(bad.value)
        assert _events(client, issue["id"]) == []
        result = asyncio.run(
            server.call_tool(
                "complete_claim",
                {
                    "issue_id": issue["id"],
                    "generation": generation,
                    "verdict": "HOLD",
                    "findings": {"P1": 1, "P2": 0, "P3": 4},
                },
            )
        )
        assert json.loads(result[0].text)["released"] is True
        detail = _events(client, issue["id"])[0]["detail"]
        assert json.loads(detail.split("; reported_completion_v1 ", 1)[1]) == {
            "verdict": "HOLD",
            "findings": {"P1": 1, "P2": 0, "P3": 4},
        }
    finally:
        client.__exit__(None, None, None)


def test_stored_history_with_an_unknown_verdict_is_not_read_as_a_report(tmp_path):
    """The read grammar is as strict as the write grammar, so a forged detail shows nothing."""
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        assert _complete(client, issue, generation, **FULL).status_code == 200
    finally:
        client.__exit__(None, None, None)
    conn = db.connect(tmp_path / "verdict.sqlite")
    try:
        event = dict(
            conn.execute(
                "SELECT * FROM activity WHERE verb='claim_completed'"
            ).fetchone()
        )
        assert lease_commands.reported_completion(event) == FULL
        for forged in (
            dict(FULL, verdict="pass"),
            dict(FULL, findings={"P9": 1}),
            dict(FULL, findings={"P1": -1}),
        ):
            head = event["detail"].split("; reported_completion_v1 ", 1)[0]
            tampered = dict(
                event, detail=f"{head}; reported_completion_v1 {json.dumps(forged)}"
            )
            assert lease_commands.reported_completion(tampered) is None
    finally:
        conn.close()


def test_every_place_the_report_is_documented_names_the_closed_words():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (
        "docs/CLOSEOUT_RECIPE.md",
        "docs/AGENT_API.md",
        "src/athena/field_guide/02_claiming_work.md",
    ):
        text = " ".join((root / path).read_text().replace("`", "").split())
        assert "PASS-WITH-NOTES" in text and "BLOCKED" in text, path
        assert "P1" in text and "P3" in text, path
    recipe = (root / "docs/CLOSEOUT_RECIPE.md").read_text()
    assert (
        "not an overall\nverdict" not in recipe
    )  # the old sentence was replaced on purpose
