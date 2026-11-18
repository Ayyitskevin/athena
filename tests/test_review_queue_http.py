"""Transport retains exact receipts and full context without leaking invalid input."""

import json

from fastapi.testclient import TestClient
import pytest

from athena.main import create_app
from test_review_queue import board as _board, claim, issue, state, submission

board = _board

MEMBER = {"X-Athena-Actor": "2"}


def test_http_receipt_queue_full_context_and_ordinary_omission(board):
    conn, path, project = board
    build = issue(conn, project)
    ordinary = issue(conn, project)
    held = claim(conn, build)
    with TestClient(create_app(path)) as client:
        before = client.get(f"/issues/{build['id']}/work-context", headers=MEMBER)
        assert "review_tracking" not in before.json()
        response = client.post(
            f"/issues/{build['id']}/complete",
            headers=MEMBER,
            json={
                "generation": held["generation"],
                "candidate_submission": submission(),
            },
        )
        assert response.status_code == 200, response.text
        receipt = response.json()["candidate_submission"]
        assert "candidate_review" not in response.json()
        full = client.get(f"/issues/{build['id']}/work-context", headers=MEMBER)
        tracking = full.json()["review_tracking"]
        assert (
            tracking["current_candidate"]["candidate"]["candidate_id"]
            == receipt["candidate_id"]
        )
        assert full.headers["etag"] != before.headers["etag"]
        assert tracking["recent_candidates"]["items"][0]["is_current"] is True
        brief = client.get(
            f"/issues/{build['id']}/work-context?brief=true", headers=MEMBER
        )
        assert "review_tracking" not in brief.json()
        packet = client.get(f"/projects/{project['id']}/review-queue", headers=MEMBER)
        assert packet.status_code == 200, packet.text
        assert packet.json()["items"][0] == tracking["current_candidate"]
        assert packet.headers["cache-control"] == "private, no-store"
        assert packet.headers["vary"] == "Authorization, X-Athena-Actor"
        assert "etag" not in packet.headers
        held = claim(conn, ordinary)
        result = client.post(
            f"/issues/{ordinary['id']}/complete",
            headers=MEMBER,
            json={"generation": held["generation"]},
        )
        assert result.status_code == 200
        assert (
            "candidate_submission" not in result.json()
            and "candidate_review" not in result.json()
        )
        assert (
            "review_tracking"
            not in client.get(
                f"/issues/{ordinary['id']}/work-context", headers=MEMBER
            ).json()
        )


@pytest.mark.parametrize(
    "source",
    ["bad\ud800source", "Authorization: Bearer " + "aB3_" * 8, "bad\x00source"],
)
def test_http_rejects_unsafe_source_without_echo_release_or_audit(board, source):
    conn, path, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    value = submission()
    value["identity"]["source"] = source
    before = state(conn)
    with TestClient(create_app(path)) as client:
        response = client.post(
            f"/issues/{build['id']}/complete",
            headers={**MEMBER, "Content-Type": "application/json"},
            content=json.dumps(
                {"generation": held["generation"], "candidate_submission": value}
            ),
        )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "invalid candidate completion extension"
    assert state(conn) == before


@pytest.mark.parametrize(
    "suffix",
    [
        "?limit=0",
        "?limit=21",
        "?limit=true",
        "?limit=1.5",
        "?cursor=0",
        "?cursor=-1",
        "?cursor=true",
        "?cursor=1.5",
        "?cursor=9223372036854775808",
    ],
)
def test_http_queue_bounds(board, suffix):
    _, path, project = board
    with TestClient(create_app(path)) as client:
        assert (
            client.get(
                f"/projects/{project['id']}/review-queue{suffix}", headers=MEMBER
            ).status_code
            == 422
        )


def test_shared_submission_schema_remains_in_openapi(board):
    _, path, _ = board
    schema = create_app(path).openapi()
    body = schema["components"]["schemas"]["CompleteClaimIn"]
    assert body["properties"]["candidate_submission"]["anyOf"][0]["$ref"].endswith(
        "/CandidateSubmission"
    )
    candidate = schema["components"]["schemas"]["CandidateSubmission"]
    assert "expected_previous_candidate_id" in candidate["required"]
    assert candidate["additionalProperties"] is False
