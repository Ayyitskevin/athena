"""Accidental credential refusal must never erase history or release possession."""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from athena.aegis import (
    issue_activity,
    issue_commands,
    issue_etags,
    issues,
    lease_commands,
    report_credentials,
)
from athena.core import activity_chain, db
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server
from athena.web import palette

_TEST_PASSWORD = "".join(("p", "w"))


def _samples():
    # Assemble inert fixtures at runtime; never check in a complete token shape.
    cases = []
    for key_type in ("", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED "):
        cases.append(
            (
                "pem-" + (key_type.strip() or "pkcs8"),
                "PEM private key",
                "-----BEGIN " + key_type + "PRIVATE KEY-----",
                "-----BEGIN " + key_type + "PUBLIC KEY-----",
            )
        )
    for prefix in ("ghp", "gho", "ghu", "ghs", "ghr"):
        token = prefix + "_" + "aB3" * 12
        cases.append((prefix, "GitHub token", token, token[:-1]))
    token = "github" + "_pat_" + "a" * 22 + "_" + "B3" * 29 + "c"
    cases.append(("github-fine", "GitHub token", token, token[:-1]))
    for prefix in ("AK" + "IA", "AS" + "IA"):
        token = prefix + "AB23" * 4
        cases.append(("aws-" + prefix.lower(), "AWS access key ID", token, token[:-1]))
    cases.append(
        (
            "slack-bot",
            "Slack token",
            "xox" + "b-" + "1" * 12 + "-" + "2" * 13 + "-" + "aB3" * 8,
            "xox" + "b-" + "1" * 12 + "-" + "2" * 13 + "-" + "aB3" * 7,
        )
    )
    cases.append(
        (
            "slack-user",
            "Slack token",
            "xox" + "p-" + "-".join(["1" * 12] * 3) + "-" + "ab12" * 8,
            "xox" + "p-" + "-".join(["1" * 12] * 3) + "-" + "ab12" * 7,
        )
    )
    cases.append(
        (
            "slack-app",
            "Slack token",
            "xapp" + "-1-A" + "B" * 10 + "-" + "1" * 13 + "-" + "a1" * 32,
            "xapp" + "-1-A" + "B" * 10 + "-" + "1" * 13 + "-" + "a1" * 31,
        )
    )
    for flavor in ("b", "p"):
        token = "xoxe" + ".xox" + flavor + "-1-" + "aB3" * 54 + "a"
        cases.append(("slack-rotated-" + flavor, "Slack token", token, token[:-1]))
    token = "sk" + "-" + "aB3d" * 5 + "T3Bl" + "bkFJ" + "aB3d" * 5
    cases.append(("openai-legacy", "OpenAI API key", token, token[:-1]))
    for prefix in ("proj", "svcacct", "admin"):
        for size in (58, 74):
            token = (
                "sk" + "-" + prefix + "-" + "a" * size + "T3Bl" + "bkFJ" + "B" * size
            )
            cases.append(
                (f"openai-{prefix}-{size}", "OpenAI API key", token, token[:-1])
            )
    for prefix in ("api03", "admin01"):
        token = "sk" + "-ant-" + prefix + "-" + "aB_-" * 23 + "a" + "AA"
        cases.append(("anthropic-" + prefix, "Anthropic API key", token, token[:-1]))
    token = "ath" + "_" + "aB_-" * 10 + "aB4"
    cases.append(("athena", "Athena token", token, token[:-1]))
    host = "example" + ".invalid"
    userinfo = ":".join(("reader", "private"))
    cases.append(
        (
            "url-password",
            "URL password",
            "https://" + userinfo + "@" + host + "/evidence",
            "https://reader@" + host + "/evidence",
        )
    )
    cases.append(
        (
            "authorization",
            "Authorization token",
            "Authorization: Bearer " + "aB3_" * 8,
            "Authorization: Bearer " + "aB3_" * 7,
        )
    )
    return cases


@pytest.fixture
def possession(tmp_path):
    conn = db.connect(tmp_path / "fixture.sqlite")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('reader', 'Reporter', 'admin')"
    )
    conn.commit()
    actor = dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone())
    issue = issues.create_issue(conn, title="Evidence", body="", created_by=actor["id"])
    claim = lease_commands.claim_issue(
        conn,
        actor=actor,
        issue_id=issue["id"],
        if_match=[issue_etags.current_etag(conn, issue)],
    )
    try:
        yield conn, actor, issue, claim["generation"]
    finally:
        conn.close()


def _snapshot(conn):
    return {
        name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
        for name in (
            "activity",
            "activity_chain",
            "issue_leases",
            "issue_claim_handoffs",
            "issues",
        )
    }


@pytest.mark.parametrize(
    ("label", "kind", "credential", "near_miss"),
    _samples(),
    ids=[case[0] for case in _samples()],
)
def test_each_known_shape_refuses_atomically_and_near_miss_is_preserved(
    possession, label, kind, credential, near_miss
):
    conn, actor, issue, generation = possession
    before = _snapshot(conn)
    with pytest.raises(issue_commands.IssueCommandError) as caught:
        lease_commands.complete_claim(
            conn,
            actor=actor,
            issue_id=issue["id"],
            generation=generation,
            results=[
                {"condition": "check", "state": "not_assessed", "note": credential}
            ],
        )
    assert caught.value.kind == "invalid"
    assert (
        caught.value.detail
        == f"results[0].note contains {kind}; remove the credential and retry"
    )
    assert credential not in caught.value.detail
    assert _snapshot(conn) == before
    result = lease_commands.complete_claim(
        conn,
        actor=actor,
        issue_id=issue["id"],
        generation=generation,
        results=[{"condition": "check", "state": "not_assessed", "note": near_miss}],
    )
    assert result["released"] is True and result["issue_status"] == issue["status"]
    event = dict(
        conn.execute("SELECT * FROM activity WHERE verb='claim_completed'").fetchone()
    )
    assert lease_commands.reported_completion(event)["results"][0]["note"] == near_miss
    assert activity_chain.verify_all(conn)["ok"] is True


@pytest.mark.parametrize(
    "field", ["condition", "note", "unfinished", "uncertainty", "source", "ref"]
)
def test_every_completion_text_field_is_checked(possession, field):
    conn, actor, issue, generation = possession
    credential = "ath" + "_" + "a" * 43
    report = {
        "results": [{"condition": "check", "state": "not_met", "note": "residual"}],
        "unfinished": ["owed"],
        "uncertainty": ["unknown"],
        "evidence": [
            {"kind": "file", "source": "fixture", "ref": "file:1", "scope": "unrun"}
        ],
    }
    if field in ("condition", "note"):
        report["results"][0][field] = credential
        path = f"results[0].{field}"
    elif field in ("source", "ref"):
        report["evidence"][0][field] = credential
        path = f"evidence[0].{field}"
    else:
        report[field][0] = credential
        path = f"{field}[0]"
    before = _snapshot(conn)
    with pytest.raises(issue_commands.IssueCommandError) as caught:
        lease_commands.complete_claim(
            conn, actor=actor, issue_id=issue["id"], generation=generation, **report
        )
    assert (
        caught.value.detail
        == f"{path} contains Athena token; remove the credential and retry"
    )
    assert _snapshot(conn) == before
    # A corrected payload must still release despite its negative/unrun report.
    report = {
        "results": [{"condition": "check", "state": "not_met", "note": "residual"}],
        "evidence": [
            {"kind": "file", "source": "fixture", "ref": "file:1", "scope": "unrun"}
        ],
    }
    assert (
        lease_commands.complete_claim(
            conn, actor=actor, issue_id=issue["id"], generation=generation, **report
        )["released"]
        is True
    )


def test_yield_refusal_keeps_all_possession_and_history_then_corrected_retry_succeeds(
    possession,
):
    conn, actor, issue, generation = possession
    token = "ghp" + "_" + "a" * 36
    payload = dict(
        actor=actor,
        issue_id=issue["id"],
        generation=generation,
        reason="blocked",
        attempted_work="source read",
        evidence=[token],
        blocking_question="Choose next step",
        resume_instructions="Read the source",
    )
    before = _snapshot(conn)
    with pytest.raises(issue_commands.IssueCommandError) as caught:
        lease_commands.yield_claim(conn, **payload)
    assert (
        caught.value.detail
        == "evidence[0] contains GitHub token; remove the credential and retry"
    )
    assert _snapshot(conn) == before
    payload["evidence"] = ["sha256:" + "b" * 64]
    handoff = lease_commands.yield_claim(conn, **payload)
    assert handoff["evidence"] == payload["evidence"]
    assert conn.execute("SELECT COUNT(*) FROM issue_leases").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM issue_claim_handoffs").fetchone()[0] == 1
    assert activity_chain.verify_all(conn)["ok"] is True


def test_legacy_credential_shaped_history_still_reads_and_renders(tmp_path):
    path = tmp_path / "legacy.sqlite"
    token = "ath" + "_" + "a" * 43
    assert lease_commands._normalize_handoff_evidence([token]) == [token]
    report = {
        "results": [{"condition": "source", "state": "inconclusive", "note": token}]
    }
    assert lease_commands._normalize_completion_report(**report) == report
    with TestClient(create_app(path)) as client:
        client.post(
            "/users",
            json={
                "email": "reader@" + "example" + ".invalid",
                "name": "Reporter",
                "password": _TEST_PASSWORD,
            },
        )
        headers = {"X-Athena-Actor": "1"}
        issue = client.post(
            "/issues", json={"title": "Legacy report"}, headers=headers
        ).json()
        conn = db.connect(path)
        try:
            issue_activity.record_claim_completed(
                conn,
                actor_id=1,
                issue_id=issue["id"],
                generation="a" * 32,
                reported=report,
            )
            event = dict(
                conn.execute(
                    "SELECT * FROM activity WHERE verb='claim_completed'"
                ).fetchone()
            )
            assert lease_commands.reported_completion(event) == report
            assert activity_chain.verify_all(conn)["ok"] is True
        finally:
            conn.close()
        context = client.get(f"/issues/{issue['id']}/work-context", headers=headers)
        assert context.status_code == 200
        assert (
            next(
                item
                for item in context.json()["recent_activity"]["items"]
                if item["verb"] == "claim_completed"
            )["reported_completion"]
            == report
        )
        for view in ("history", "work-context"):
            page = client.get(f"/aegis/issues/{issue['id']}/{view}", headers=headers)
            assert page.status_code == 200 and token in page.text


@pytest.mark.parametrize("operation", ["complete", "yield"])
def test_api_mcp_and_web_error_translation_never_echo_or_log_refused_text(
    tmp_path, caplog, operation
):
    token = "ath" + "_" + "aB_" * 14 + "A"
    with TestClient(create_app(tmp_path / "transport.sqlite")) as client:
        client.post(
            "/users",
            json={
                "email": "reader@" + "example" + ".invalid",
                "name": "Reporter",
                "password": _TEST_PASSWORD,
            },
        )
        client.headers.update({"X-Athena-Actor": "1"})
        issue = client.post("/issues", json={"title": "Transport refusal"}).json()
        read = client.get(f"/issues/{issue['id']}")
        claim = client.post(
            f"/issues/{issue['id']}/claim",
            json={},
            headers={"If-Match": read.headers["etag"]},
        ).json()
        payload = {"generation": claim["generation"], "uncertainty": [token]}
        field = "uncertainty"
        if operation == "yield":
            field = "evidence"
            payload = {
                "generation": claim["generation"],
                "reason": "blocked",
                "attempted_work": "source read",
                "evidence": [token],
                "blocking_question": "Choose next step",
                "resume_instructions": "Read source",
            }
        caplog.clear()
        caplog.set_level(logging.DEBUG)
        response = client.post(f"/issues/{issue['id']}/{operation}", json=payload)
        assert response.status_code == 422
        detail = f"{field}[0] contains Athena token; remove the credential and retry"
        assert response.json() == {"detail": detail}
        server = build_server(AthenaClient(client=client))
        with pytest.raises(ToolError) as caught:
            asyncio.run(
                server.call_tool(
                    f"{operation}_claim", {"issue_id": issue["id"], **payload}
                )
            )
        assert detail in str(caught.value)
        web = palette._command_error(
            issue_commands.IssueCommandError("invalid", detail)
        )
        assert web.status_code == 422
        assert json.loads(web.body) == {"detail": detail, "code": "invalid"}
        assert (
            token
            not in response.text + str(caught.value) + web.body.decode() + caplog.text
        )
        assert token[4:] not in caplog.text
        payload[field] = ["Still unknown"]
        accepted = client.post(f"/issues/{issue['id']}/{operation}", json=payload)
        assert accepted.status_code == (200 if operation == "complete" else 201)


def test_ordinary_evidence_and_token_near_misses_are_not_entropy_scanned():
    host = "example" + ".invalid"
    safe = [
        "Review is unfinished; a separate run is still owed.",
        "a" * 40,
        "sha256:" + "b" * 64,
        "12345678-1234-1234-1234-123456789abc",
        "https://" + host + "/evidence",
        "https://reader:@" + host,
        "ssh://reader@" + host + "/source",
    ]
    for text in safe:
        assert report_credentials.credential_kind(text) is None
    for label, kind, credential, near_miss in _samples():
        assert report_credentials.credential_kind(credential) == kind, label
        assert report_credentials.credential_kind(near_miss) is None, label


@pytest.mark.parametrize("operation", ["complete", "yield"])
@pytest.mark.parametrize("style", ["uppercase-underscore", "lowercase-hyphen"])
def test_authorization_word_placeholders_pass_and_digit_tokens_still_refuse(
    possession, operation, style
):
    words = ("YOUR", "LONG", "LIVED", "ACCESS", "TOKEN", "HERE")
    placeholder = (
        "_".join(words)
        if style == "uppercase-underscore"
        else "-".join(word.lower() for word in words)
    )
    header = "Authorization: " + "Bearer " + placeholder
    assert len(placeholder) == 33
    assert report_credentials.credential_kind(header) is None
    mixed = "".join(("aB7_", "qR3-", "mN8.", "tU2~")) * 2
    credential = "Authorization: " + "Bearer " + mixed
    assert report_credentials.credential_kind(credential) == "Authorization token"
    conn, actor, issue, generation = possession
    payload = {"actor": actor, "issue_id": issue["id"], "generation": generation}
    if operation == "complete":
        payload["results"] = [
            {"condition": "check", "state": "not_met", "note": credential}
        ]
        field = "results[0].note"
        command = lease_commands.complete_claim
    else:
        payload.update(
            reason="blocked",
            attempted_work="source read",
            evidence=[credential],
            blocking_question="Choose next step",
            resume_instructions="Read source",
        )
        field = "evidence[0]"
        command = lease_commands.yield_claim
    before = _snapshot(conn)
    with pytest.raises(issue_commands.IssueCommandError) as caught:
        command(conn, **payload)
    assert (
        caught.value.detail
        == f"{field} contains Authorization token; remove the credential and retry"
    )
    assert credential not in caught.value.detail
    assert _snapshot(conn) == before
    if operation == "complete":
        payload["results"][0]["note"] = header
        assert command(conn, **payload)["released"] is True
        event = dict(
            conn.execute(
                "SELECT * FROM activity WHERE verb='claim_completed'"
            ).fetchone()
        )
        assert lease_commands.reported_completion(event)["results"][0]["note"] == header
    else:
        payload["evidence"] = [header]
        assert command(conn, **payload)["evidence"] == [header]
    assert activity_chain.verify_all(conn)["ok"] is True


def test_authorization_digit_in_scheme_is_not_a_digit_in_the_value():
    words = ("your", "long", "lived", "access", "token", "here")
    header = "Authorization: " + "Scheme9 " + "-".join(words)
    assert report_credentials.credential_kind(header) is None
