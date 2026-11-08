"""Evidence items may bind to bytes by digest, and a result may name what it judged.

Validation is shape only. The one thing Athena checks is an attachment it holds on the SAME
issue: its stored digest is compared and the history says whether it matches. A mismatch is shown
as unverified and is never a refusal, because Athena verifies only what it holds.
"""

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from athena.aegis import lease_commands
from athena.core import db
from athena.main import create_app

HEADERS = {"X-Athena-Actor": "1", "X-Athena-Run": "completion-evidence-test"}
DATA = b"the bytes a reviewer actually read\n"
DIGEST = hashlib.sha256(DATA).hexdigest()
OTHER = hashlib.sha256(b"different bytes").hexdigest()


def _client(tmp_path):
    client = TestClient(create_app(tmp_path / "evidence.sqlite"))
    client.__enter__()
    assert (
        client.post(
            "/users",
            json={"email": "e@example.com", "name": "Evidence", "password": "pw"},
        ).status_code
        == 201
    )
    return client


def _issue(client, title="Reviewed work"):
    return client.post("/issues", json={"title": title}, headers=HEADERS).json()


def _claim(client, issue):
    read = client.get(f"/issues/{issue['id']}", headers=HEADERS)
    response = client.post(
        f"/issues/{issue['id']}/claim",
        json={},
        headers={**HEADERS, "If-Match": read.headers["etag"]},
    )
    assert response.status_code == 201, response.text
    return response.json()["generation"]


def _attach(client, issue, data=DATA):
    response = client.post(
        f"/issues/{issue['id']}/attachments",
        files={"file": ("review.md", data, "text/markdown")},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _claimed(client):
    issue = _issue(client)
    return issue, _claim(client, issue)


def _complete(client, issue, generation, **fields):
    return client.post(
        f"/issues/{issue['id']}/complete",
        content=json.dumps({"generation": generation, **fields}).encode("utf-8"),
        headers={**HEADERS, "Content-Type": "application/json"},
    )


def _events(client, issue_id):
    return client.get(
        f"/activity?target_kind=issue&target_id={issue_id}&verb=claim_completed",
        headers=HEADERS,
    ).json()


def _stored_report(client, issue_id):
    detail = _events(client, issue_id)[0]["detail"]
    return json.loads(detail.split("; reported_completion_v1 ", 1)[1])


def _item(client, issue_id):
    history = client.get(f"/issues/{issue_id}/history", headers=HEADERS).json()
    return next(i for i in history["items"] if i["state"] == "claim_completed")


def _evidence(**extra):
    return {
        "kind": "file",
        "source": "checkout@abc123",
        "ref": "docs/review.md",
        "scope": "source-only",
        **extra,
    }


def test_digest_bytes_locator_and_subject_round_trip_exactly(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        report = {
            "verdict": "PASS",
            "results": [
                {
                    "condition": "reviewed",
                    "state": "met",
                    "note": "",
                    "subject": ["src/a.py", "src/b.py"],
                }
            ],
            "evidence": [
                _evidence(digest=DIGEST, bytes=len(DATA), locator="L10-L40"),
                _evidence(kind="forge_event", ref="42", source="imported forge row"),
            ],
        }
        response = _complete(client, issue, generation, **report)
        assert response.status_code == 200, response.text
        assert _stored_report(client, issue["id"]) == report
        context = client.get(
            f"/issues/{issue['id']}/work-context", headers=HEADERS
        ).json()
        completed = next(
            r
            for r in context["recent_activity"]["items"]
            if r["verb"] == "claim_completed"
        )
        assert completed["reported_completion"] == report
        assert _item(client, issue["id"])["reported_completion"] == report
        # A forge reference is shape only: Athena does not claim to have checked it.
        assert _item(client, issue["id"])["evidence_checks"] == [
            "unverified reference",
            "unverified reference",
        ]
    finally:
        client.__exit__(None, None, None)


def test_old_reports_are_stored_and_shown_exactly_as_before(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        old = {"evidence": [_evidence()], "unfinished": ["x"]}
        assert _complete(client, issue, generation, **old).status_code == 200
        assert _stored_report(client, issue["id"]) == old
        item = _item(client, issue["id"])
        assert item["reported_completion"] == old
        assert (
            "evidence_checks" not in item
        )  # nothing new was asked, nothing new is shown
        page = client.get(f"/aegis/issues/{issue['id']}/history", headers=HEADERS)
        assert "matches stored attachment" not in page.text
    finally:
        client.__exit__(None, None, None)


BAD_EVIDENCE = [
    ({"digest": DIGEST.upper()}, "digest"),
    ({"digest": DIGEST[:-1]}, "digest"),
    ({"digest": DIGEST + "0"}, "digest"),
    ({"digest": "z" * 64}, "digest"),
    ({"digest": 123}, "digest"),
    ({"digest": None}, "digest"),
    ({"bytes": -1}, "bytes"),
    ({"bytes": 1.5}, "bytes"),
    ({"bytes": "10"}, "bytes"),
    ({"bytes": True}, "bytes"),
    ({"locator": ""}, "locator"),
    ({"locator": 7}, "locator"),
    ({"locator": "x" * 501}, "locator"),
    ({"extra": "nope"}, "evidence[0]"),
    ({"kind": "attachment", "ref": "not-a-number"}, "ref"),
    ({"kind": "attachment", "ref": "0"}, "ref"),
    ({"kind": "forge_event", "ref": "abc"}, "ref"),
    ({"kind": "forge_event", "ref": "-3"}, "ref"),
]


@pytest.mark.parametrize("extra,named", BAD_EVIDENCE)
def test_malformed_evidence_is_refused_atomically_then_a_corrected_retry_works(
    tmp_path, extra, named
):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        refused = _complete(client, issue, generation, evidence=[_evidence(**extra)])
        assert refused.status_code == 422, refused.text
        assert named in refused.text
        assert _events(client, issue["id"]) == []
        # Positive control: a well-formed digest on the same lease is accepted.
        good = _complete(
            client,
            issue,
            generation,
            evidence=[_evidence(digest=DIGEST, bytes=3, locator="L1")],
        )
        assert good.status_code == 200, good.text
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "subject",
    [[], "src/a.py", [""], [5], ["x" * 201], ["s"] * 11, {"a": 1}],
    ids=["empty", "string", "blank", "int", "long", "too-many", "object"],
)
def test_a_malformed_subject_is_refused_and_a_good_one_accepted(tmp_path, subject):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        result = {"condition": "c", "state": "met", "note": "", "subject": subject}
        refused = _complete(client, issue, generation, results=[result])
        assert refused.status_code == 422, refused.text
        assert "subject" in refused.text
        assert _events(client, issue["id"]) == []
        ok = dict(result, subject=["src/a.py"])
        assert _complete(client, issue, generation, results=[ok]).status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_a_matching_attachment_on_this_issue_shows_matches_stored_attachment(tmp_path):
    client = _client(tmp_path)
    try:
        issue = _issue(client)
        attachment = _attach(client, issue)
        generation = _claim(client, issue)
        evidence = [
            _evidence(
                kind="attachment",
                ref=str(attachment["id"]),
                digest=DIGEST,
                bytes=len(DATA),
                source="this issue",
            )
        ]
        assert (
            _complete(client, issue, generation, evidence=evidence).status_code == 200
        )
        assert _item(client, issue["id"])["evidence_checks"] == [
            "matches stored attachment"
        ]
        for view in ("history", "work-context"):
            page = client.get(f"/aegis/issues/{issue['id']}/{view}", headers=HEADERS)
            assert page.status_code == 200, page.text
            assert "matches stored attachment" in page.text
            assert "unverified reference" not in page.text
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "mutate",
    [
        {"digest": OTHER},
        {"bytes": len(DATA) + 1},
        {"ref": "9999"},  # no such attachment
    ],
    ids=["wrong-digest", "wrong-size", "no-such-attachment"],
)
def test_a_mismatch_is_shown_as_unverified_and_is_not_refused(tmp_path, mutate):
    client = _client(tmp_path)
    try:
        issue = _issue(client)
        attachment = _attach(client, issue)
        generation = _claim(client, issue)
        item = {
            "kind": "attachment",
            "ref": str(attachment["id"]),
            "digest": DIGEST,
            "bytes": len(DATA),
            "source": "this issue",
            "scope": "actual-run",
            **mutate,
        }
        response = _complete(client, issue, generation, evidence=[item])
        assert response.status_code == 200, response.text  # recorded, not refused
        assert _stored_report(client, issue["id"])["evidence"] == [item]
        assert _item(client, issue["id"])["evidence_checks"] == ["unverified reference"]
        page = client.get(f"/aegis/issues/{issue['id']}/history", headers=HEADERS)
        assert "unverified reference" in page.text
        assert "matches stored attachment" not in page.text
    finally:
        client.__exit__(None, None, None)


def test_an_attachment_on_another_issue_is_never_confirmed_here(tmp_path):
    """Comparing a digest would confirm a hidden file exists, so only this issue's own
    attachments are checked; anything else is simply an unverified reference."""
    client = _client(tmp_path)
    try:
        other = _issue(client, "Elsewhere")
        foreign = _attach(client, other)
        issue = _issue(client)
        generation = _claim(client, issue)
        evidence = [
            _evidence(
                kind="attachment",
                ref=str(foreign["id"]),
                digest=DIGEST,
                source="other issue",
            )
        ]
        assert (
            _complete(client, issue, generation, evidence=evidence).status_code == 200
        )
        assert _item(client, issue["id"])["evidence_checks"] == ["unverified reference"]
    finally:
        client.__exit__(None, None, None)


def test_a_digest_on_a_non_attachment_is_a_declaration_athena_cannot_check(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        assert (
            _complete(
                client,
                issue,
                generation,
                evidence=[_evidence(digest=DIGEST, bytes=len(DATA))],
            ).status_code
            == 200
        )
        assert _item(client, issue["id"])["evidence_checks"] == ["unverified reference"]
    finally:
        client.__exit__(None, None, None)


def test_the_credential_refusal_still_applies_to_the_new_text_fields(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        token = (
            "gh" + "p_" + "aB3" * 12
        )  # assembled at runtime, never a checked-in token
        for fields, path in (
            ({"evidence": [_evidence(locator=f"see {token}")]}, "evidence[0].locator"),
            (
                {
                    "results": [
                        {
                            "condition": "c",
                            "state": "met",
                            "note": "",
                            "subject": [token],
                        }
                    ]
                },
                "results[0].subject[0]",
            ),
        ):
            refused = _complete(client, issue, generation, **fields)
            assert refused.status_code == 422, refused.text
            assert "GitHub token" in refused.text and path in refused.text
            assert _events(client, issue["id"]) == []
        clean = _complete(
            client, issue, generation, evidence=[_evidence(digest=DIGEST, locator="L1")]
        )
        assert clean.status_code == 200, clean.text
    finally:
        client.__exit__(None, None, None)


def test_the_read_grammar_is_as_strict_and_a_forged_digest_shows_nothing(tmp_path):
    client = _client(tmp_path)
    try:
        issue, generation = _claimed(client)
        report = {"evidence": [_evidence(digest=DIGEST, bytes=1, locator="L1")]}
        assert _complete(client, issue, generation, **report).status_code == 200
    finally:
        client.__exit__(None, None, None)
    conn = db.connect(tmp_path / "evidence.sqlite")
    try:
        event = dict(
            conn.execute(
                "SELECT * FROM activity WHERE verb='claim_completed'"
            ).fetchone()
        )
        assert lease_commands.reported_completion(event) == report
        head = event["detail"].split("; reported_completion_v1 ", 1)[0]
        for bad in (
            {"evidence": [_evidence(digest="ABC")]},
            {"evidence": [_evidence(bytes=-1)]},
            {
                "results": [
                    {"condition": "c", "state": "met", "note": "", "subject": "x"}
                ]
            },
        ):
            tampered = dict(
                event, detail=f"{head}; reported_completion_v1 {json.dumps(bad)}"
            )
            assert lease_commands.reported_completion(tampered) is None
    finally:
        conn.close()


def test_documented_where_the_report_is_documented():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (
        "docs/CLOSEOUT_RECIPE.md",
        "docs/AGENT_API.md",
        "src/athena/field_guide/02_claiming_work.md",
    ):
        text = " ".join((root / path).read_text().replace("`", "").split())
        assert "digest" in text and "forge_event" in text, path
        assert re.search(r"matches stored attachment", text), path
