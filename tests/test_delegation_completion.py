"""A delegated agent can FINISH the work it was handed.

The flagship Assign → Work loop used to dead-end one step before completion:
delegate_issue added the agent to issue_contributors and the inbox described the
work beautifully — but issues.can_modify was creator-or-assignee only, so the
delegated agent could never mark the issue in_progress or done. These tests pin
the authoritative gate (issues.can_act_on): creator, assignee, delegated
contributor, or admin may act; everyone else stays 403; and the fix holds across
REST, the command layer, and the MCP client path.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}  # bootstrap admin (issue creator in these tests)


def _app(tmp_path, name="delegation.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _user(client, email, *, role="member", is_agent=False):
    return client.post(
        "/users",
        json={
            "email": email,
            "name": email.split("@")[0],
            "role": role,
            "is_agent": is_agent,
        },
        headers=H1,
    ).json()


def _issue(client, title="delegated work"):
    return client.post("/issues", json={"title": title}, headers=H1).json()


def test_delegated_agent_can_transition_the_issue(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _user(c, "sol@e.com", is_agent=True)
        issue = _issue(c)
        # Creator delegates; the agent is now a contributor.
        assert (
            c.post(
                f"/issues/{issue['id']}/delegate",
                json={"user_id": agent["id"]},
                headers=H1,
            ).status_code
            == 201
        )
        # THE FIX: the delegated agent may now work the issue to completion.
        as_agent = {"X-Athena-Actor": str(agent["id"])}
        started = c.patch(
            f"/issues/{issue['id']}", json={"status": "in_progress"}, headers=as_agent
        )
        assert started.status_code == 200, started.text
        done = c.patch(
            f"/issues/{issue['id']}", json={"status": "done"}, headers=as_agent
        )
        assert done.status_code == 200
        assert done.json()["status"] == "done"


def test_unrelated_actor_is_still_forbidden(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bystander = _user(c, "random@e.com")
        issue = _issue(c)
        denied = c.patch(
            f"/issues/{issue['id']}",
            json={"status": "done"},
            headers={"X-Athena-Actor": str(bystander["id"])},
        )
        assert denied.status_code == 403
        assert "delegated contributor" in denied.json()["detail"]


def test_admin_override_can_transition_without_delegation(tmp_path):
    # The operator lever: an admin who is neither creator, assignee, nor
    # contributor may still act — and it also unbricks issues whose creator left.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = _user(c, "m@e.com")
        second_admin = _user(c, "boss@e.com", role="admin")
        as_member = {"X-Athena-Actor": str(member["id"])}
        issue = c.post("/issues", json={"title": "member's"}, headers=as_member).json()
        moved = c.patch(
            f"/issues/{issue['id']}",
            json={"status": "in_progress"},
            headers={"X-Athena-Actor": str(second_admin["id"])},
        )
        assert moved.status_code == 200


def test_removing_the_contributor_revokes_the_ability(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _user(c, "sol@e.com", is_agent=True)
        issue = _issue(c)
        c.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": agent["id"]},
            headers=H1,
        )
        as_agent = {"X-Athena-Actor": str(agent["id"])}
        assert (
            c.patch(
                f"/issues/{issue['id']}", json={"status": "in_progress"}, headers=as_agent
            ).status_code
            == 200
        )
        # Un-delegate: the agent loses write access again.
        assert (
            c.delete(
                f"/issues/{issue['id']}/contributors/{agent['id']}", headers=H1
            ).status_code
            in (200, 204)
        )
        assert (
            c.patch(
                f"/issues/{issue['id']}", json={"status": "done"}, headers=as_agent
            ).status_code
            == 403
        )


def test_delegated_agent_completes_over_mcp(tmp_path):
    # End-to-end through the transport agents actually use: delegate, then the
    # agent (scoped token, tagged run) moves the issue to done via AthenaClient.
    app, db_file = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        agent = _user(tc, "sol@e.com", is_agent=True)
        issue = _issue(tc)
        tc.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": agent["id"]},
            headers=H1,
        )
        raw = tc.post(
            "/tokens",
            json={"name": "sol", "scopes": ["read", "issue:write"]},
            headers={"X-Athena-Actor": str(agent["id"])},
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        tc.headers.pop("X-Athena-Actor", None)
        ath = AthenaClient(client=tc, run_id="sol-finishes-work")

        updated = ath.update_issue(issue["id"], status="done")
        assert updated["status"] == "done"
    finally:
        tc.__exit__(None, None, None)

    conn = db.connect(db_file)
    ev = conn.execute(
        "SELECT actor_id, run_id FROM activity WHERE verb = 'changed_status'"
    ).fetchone()
    conn.close()
    assert ev["actor_id"] == agent["id"]  # attributed to the agent
    assert ev["run_id"] == "sol-finishes-work"  # and to its run
