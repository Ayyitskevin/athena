"""A done transition reports a holder's row without performing closeout for them."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from athena.aegis import leases
from athena.core import db
from athena.core.etag import strong_etag
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server

ADMIN = {"X-Athena-Actor": "1"}


def _bearer(agent):
    return {"Authorization": "Bearer " + agent["token"]["token"]}


@pytest.fixture
def subject(tmp_path):
    database = tmp_path / "held-note.db"
    with TestClient(create_app(database)) as client:
        created = client.post(
            "/users", json={"email": "admin@example.test", "name": "Admin"}
        )
        assert created.status_code == 201, created.text
        agents = []
        for name in ("holder", "other", "outsider"):
            onboarded = client.post(
                "/users/onboard_agent",
                json={
                    "email": name + "@example.test",
                    "name": name,
                    "scopes": ["read", "issue:write"],
                },
                headers=ADMIN,
            )
            assert onboarded.status_code == 201, onboarded.text
            agents.append(onboarded.json())
        project = client.post(
            "/projects", json={"name": "Work", "key": "WORK"}, headers=ADMIN
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        for name in ("shipped", "released"):
            status = client.post(
                f"/projects/{project_id}/statuses",
                json={"name": name, "category": "done"},
                headers=ADMIN,
            )
            assert status.status_code == 201, status.text
        issue = client.post(
            "/issues",
            json={"title": "Feature", "project_id": project_id},
            headers=ADMIN,
        )
        assert issue.status_code == 201, issue.text
        issue_id = issue.json()["id"]
        for agent in agents[:2]:
            delegated = client.post(
                f"/issues/{issue_id}/delegate",
                json={"user_id": agent["user"]["id"]},
                headers=ADMIN,
            )
            assert delegated.status_code == 201, delegated.text
        tag = client.get(f"/issues/{issue_id}", headers=_bearer(agents[0])).headers[
            "etag"
        ]
        claim = client.post(
            f"/issues/{issue_id}/claim", headers={**_bearer(agents[0]), "If-Match": tag}
        )
        assert claim.status_code == 201, claim.text
        conn = db.connect(database)
        try:
            yield client, conn, issue_id, agents, claim.json()
        finally:
            conn.close()


def _row(conn, issue_id):
    row = conn.execute(
        "SELECT * FROM issue_leases WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    return tuple(row) if row else None


def _expire(conn, issue_id):
    # Fixture-only clock setup, never a production mutation or renewal.
    conn.execute(
        "UPDATE issue_leases SET expires_at = '2000-01-01 00:00:00' WHERE issue_id = ?",
        (issue_id,),
    )
    conn.commit()


def _side_state(conn):
    return {
        "cursors": [tuple(row) for row in conn.execute("SELECT * FROM agent_cursors")],
        "notification_reads": dict(
            conn.execute("SELECT id, read_at FROM notifications").fetchall()
        ),
    }


def _unchanged_closeout(before, conn):
    after = _side_state(conn)
    assert before["cursors"] == after["cursors"]
    assert all(
        after["notification_reads"][key] == value
        for key, value in before["notification_reads"].items()
    )


@pytest.mark.parametrize("surface", ["rest", "client", "mcp"])
@pytest.mark.parametrize("expired", [False, True])
def test_own_row_note_crosses_real_chain_without_releasing(subject, surface, expired):
    client, conn, issue_id, agents, claim = subject
    if expired:
        _expire(conn, issue_id)
    row = _row(conn, issue_id)
    before = _side_state(conn)
    baseline = conn.execute("SELECT max(id) FROM activity").fetchone()[0]
    headers = _bearer(agents[0])
    tag = client.get(f"/issues/{issue_id}", headers=headers).headers["etag"]
    if surface == "rest":
        response = client.patch(
            f"/issues/{issue_id}",
            json={"status": "shipped"},
            headers={**headers, "If-Match": tag},
        )
        assert response.status_code == 200, response.text
        body = response.json()
    else:
        client.headers.update(headers)
        api = AthenaClient(client=client)
        if surface == "client":
            body = api.update_issue(issue_id, status="shipped", if_match=tag)
        else:
            server = build_server(api, scopes={"read", "issue:write"})
            result = asyncio.run(
                server.call_tool(
                    "update_issue",
                    {
                        "issue_id": issue_id,
                        "status": "shipped",
                        "if_match": tag,
                    },
                )
            )
            body = json.loads(result[0].text)
    note = body["held_lease"]
    assert set(note) == {"generation", "expires_at", "active", "message"}
    assert note["generation"] == claim["generation"]
    assert note["expires_at"] == (
        "2000-01-01 00:00:00" if expired else claim["expires_at"]
    )
    assert note["active"] is (not expired)
    assert "separate deliberate action" in note["message"]
    assert (
        "complete_claim" in note["message"]
        and "do not auto-complete" in note["message"]
    )
    assert "transition" in note["message"]
    if expired:
        assert "not active possession" in note["message"]
    assert _row(conn, issue_id) == row
    _unchanged_closeout(before, conn)
    assert [
        r[0]
        for r in conn.execute(
            "SELECT verb FROM activity WHERE id > ? ORDER BY id", (baseline,)
        )
    ] == ["changed_status"]
    # The human/agent must choose the second operation explicitly with the exact
    # generation. Receiving the note did not make that decision for them.
    completed = client.post(
        f"/issues/{issue_id}/complete",
        json={"generation": note["generation"]},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["released"] is True
    assert _row(conn, issue_id) is None


@pytest.mark.parametrize("actor", ["other", "admin"])
@pytest.mark.parametrize("expired", [False, True])
def test_other_actor_never_receives_holder_row(subject, actor, expired):
    client, conn, issue_id, agents, _ = subject
    if expired:
        _expire(conn, issue_id)
    before = _row(conn, issue_id)
    response = client.patch(
        f"/issues/{issue_id}",
        json={"status": "shipped"},
        headers=ADMIN if actor == "admin" else _bearer(agents[1]),
    )
    assert response.status_code == 200, response.text
    assert "held_lease" not in response.json()
    assert _row(conn, issue_id) == before


@pytest.mark.parametrize(
    "case", ["no_lease", "non_done", "already_done", "same_done_category"]
)
def test_non_transition_or_absent_row_has_no_note(subject, case):
    client, conn, issue_id, agents, claim = subject
    headers = _bearer(agents[0])
    status = "shipped"
    if case == "no_lease":
        assert (
            client.post(
                f"/issues/{issue_id}/complete",
                json={"generation": claim["generation"]},
                headers=headers,
            ).status_code
            == 200
        )
    elif case == "non_done":
        status = "in_progress"
    else:
        assert (
            client.patch(
                f"/issues/{issue_id}", json={"status": "shipped"}, headers=ADMIN
            ).status_code
            == 200
        )
        if case == "same_done_category":
            status = "released"
    before = _row(conn, issue_id)
    response = client.patch(
        f"/issues/{issue_id}", json={"status": status}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert "held_lease" not in response.json()
    assert _row(conn, issue_id) == before


@pytest.mark.parametrize(
    "case,expected",
    [("stale", 412), ("outsider", 403), ("invalid", 422), ("empty", 422)],
)
def test_refused_write_has_no_note_or_closeout_effect(subject, case, expected):
    client, conn, issue_id, agents, _ = subject
    headers = _bearer(agents[2] if case == "outsider" else agents[0])
    if case == "stale":
        headers["If-Match"] = '"not-current"'
    payload = (
        {}
        if case == "empty"
        else {"status": "missing" if case == "invalid" else "shipped"}
    )
    before = _side_state(conn)
    row = _row(conn, issue_id)
    activity_before = [tuple(r) for r in conn.execute("SELECT * FROM activity")]
    response = client.patch(f"/issues/{issue_id}", json=payload, headers=headers)
    assert response.status_code == expected, response.text
    assert "held_lease" not in response.json()
    assert _row(conn, issue_id) == row
    assert activity_before == [tuple(r) for r in conn.execute("SELECT * FROM activity")]
    _unchanged_closeout(before, conn)


def test_note_is_transient_and_etag_covers_only_canonical_issue(subject):
    client, conn, issue_id, agents, _ = subject
    headers = _bearer(agents[0])
    initial = client.get(f"/issues/{issue_id}", headers=headers)
    closed = client.patch(
        f"/issues/{issue_id}",
        json={"status": "shipped"},
        headers={**headers, "If-Match": initial.headers["etag"]},
    )
    assert closed.status_code == 200, closed.text
    patch_body = closed.json()
    assert patch_body.pop("held_lease")["active"] is True
    fetched = client.get(f"/issues/{issue_id}", headers=headers)
    assert patch_body == fetched.json()
    assert (
        closed.headers["etag"]
        == fetched.headers["etag"]
        == strong_etag("issue-v1", patch_body)
    )
    assert "held_lease" not in fetched.json()
    assert all(
        "held_lease" not in issue
        for issue in client.get("/issues", headers=headers).json()
    )
    assert patch_body["assignee_id"] is None and patch_body["sprint_id"] is None
    assert patch_body["archived_at"] is None
    still_current = client.patch(
        f"/issues/{issue_id}",
        json={"title": "Documented"},
        headers={**headers, "If-Match": closed.headers["etag"]},
    )
    assert still_current.status_code == 200, still_current.text
    assert "held_lease" not in still_current.json()
    stale = client.patch(
        f"/issues/{issue_id}",
        json={"title": "Lost"},
        headers={**headers, "If-Match": closed.headers["etag"]},
    )
    assert stale.status_code == 412


def test_idempotent_replay_preserves_original_note_without_reobserving(
    subject, monkeypatch
):
    client, conn, issue_id, agents, claim = subject
    headers = {**_bearer(agents[0]), "Idempotency-Key": "held-note-replay"}
    original = client.patch(
        f"/issues/{issue_id}", json={"status": "shipped"}, headers=headers
    )
    assert original.status_code == 200, original.text
    assert original.json()["held_lease"]["generation"] == claim["generation"]
    completed = client.post(
        f"/issues/{issue_id}/complete",
        json={"generation": claim["generation"]},
        headers=_bearer(agents[0]),
    )
    assert completed.status_code == 200 and _row(conn, issue_id) is None
    before = [tuple(row) for row in conn.execute("SELECT * FROM activity")]

    def no_reobservation(*args, **kwargs):
        pytest.fail("idempotent replay must not re-read the lease")

    monkeypatch.setattr(leases, "get_lease", no_reobservation)
    replay = client.patch(
        f"/issues/{issue_id}", json={"status": "shipped"}, headers=headers
    )
    assert replay.status_code == 200 and replay.json() == original.json()
    assert replay.headers["etag"] == original.headers["etag"]
    assert before == [tuple(row) for row in conn.execute("SELECT * FROM activity")]
    assert _row(conn, issue_id) is None
