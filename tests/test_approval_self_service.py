"""Approvals are human-only, and no account steers its own controls (D13, SEC-1).

`docs/APPROVALS.md` promises a gated agent cannot approve itself or ungate
itself. The review reproduced the opposite: an agent account the operator had
given the admin role and an admin-scope token approved its own request, cleared
its own approval policy and deleted its own budget, and the trail recorded each
as an ordinary operator write. The admin gate alone cannot carry the promise,
because the precondition IS admin.

So the rule lives in the commands, not the routes: `decide` refuses a decider
who is the requester and any agent account whatever its role; the policy and
budget writes refuse when the target is the actor. A refusal is a 403 and
writes nothing, not even an audit event — the trail must not record a control
change that did not happen. A human operator keeps every power it had.
"""

import pytest
from fastapi.testclient import TestClient

from athena.core import approval_commands, budget_commands, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _admin_agent(client, email):
    """The precondition SEC-1 needs: an agent account with the admin role AND an
    admin-scope bearer token — reachable only by an operator going past the
    conductor recipe, but reachable."""
    onboarded = client.post(
        "/users/onboard_agent",
        json={
            "email": email,
            "name": email.split("@")[0],
            "scopes": ["read", "issue:write", "admin"],
        },
        headers=H1,
    )
    assert onboarded.status_code == 201, onboarded.text
    body = onboarded.json()
    promoted = client.put(
        f"/users/{body['user']['id']}/role", json={"role": "admin"}, headers=H1
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["is_agent"] is True
    return body["user"]["id"], {"Authorization": f"Bearer {body['token']['token']}"}


def _activity_count(db_file):
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    finally:
        conn.close()


def _gated_ask(client, agent_id, bearer):
    """Gate the agent on issue.close and have it earn a genuine pending ask."""
    gated = client.put(
        f"/approvals/policies/{agent_id}",
        json={"action_kind": "issue.close"},
        headers=H1,
    )
    assert gated.status_code == 200, gated.text
    issue = client.post("/issues", json={"title": "gated"}, headers=bearer).json()
    refused = client.patch(
        f"/issues/{issue['id']}", json={"status": "done"}, headers=bearer
    )
    assert refused.status_code == 202, refused.text
    return issue["id"], refused.json()["approval"]["id"]


def _set_budget(client, user_id, limit=100, headers=H1):
    return client.put(
        f"/users/{user_id}/budget",
        json={"window": "day", "action_limit": limit},
        headers=headers,
    )


def test_an_admin_agent_cannot_approve_its_own_request(tmp_path):
    db_file = tmp_path / "self.db"
    with TestClient(create_app(db_file)) as c:
        _bootstrap(c)
        sol, sol_bearer = _admin_agent(c, "sol@e.com")
        issue_id, request_id = _gated_ask(c, sol, sol_bearer)

        before = _activity_count(db_file)
        own = c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=sol_bearer,
        )
        assert own.status_code == 403, own.text
        assert "agent" in own.json()["detail"]
        assert _activity_count(db_file) == before

        # The ask is still waiting on a human, and the close did not happen.
        pending = c.get(f"/approvals/{request_id}", headers=H1).json()
        assert pending["state"] == "pending"
        assert pending["decided_by"] is None
        assert c.get(f"/issues/{issue_id}", headers=H1).json()["status"] == "open"
        again = c.patch(
            f"/issues/{issue_id}", json={"status": "done"}, headers=sol_bearer
        )
        assert again.status_code == 202


def test_another_admin_agent_cannot_decide_it_either(tmp_path):
    # Not only self-approval: any agent account deciding is the fleet approving
    # itself. "Human-only" is a property of the decider, whatever its role.
    db_file = tmp_path / "peer.db"
    with TestClient(create_app(db_file)) as c:
        _bootstrap(c)
        sol, sol_bearer = _admin_agent(c, "sol@e.com")
        _, peer_bearer = _admin_agent(c, "peer@e.com")
        _, request_id = _gated_ask(c, sol, sol_bearer)

        before = _activity_count(db_file)
        for decision in ("approve", "reject"):
            peer = c.post(
                f"/approvals/{request_id}/decision",
                json={"decision": decision},
                headers=peer_bearer,
            )
            assert peer.status_code == 403, peer.text
        assert _activity_count(db_file) == before
        assert c.get(f"/approvals/{request_id}", headers=H1).json()["state"] == (
            "pending"
        )


def test_an_admin_agent_cannot_ungate_or_unbudget_itself(tmp_path):
    db_file = tmp_path / "ungate.db"
    with TestClient(create_app(db_file)) as c:
        _bootstrap(c)
        sol, sol_bearer = _admin_agent(c, "sol@e.com")
        _gated_ask(c, sol, sol_bearer)
        assert _set_budget(c, sol, limit=100).status_code == 200

        before = _activity_count(db_file)
        attempts = [
            c.delete(f"/approvals/policies/{sol}/issue.close", headers=sol_bearer),
            c.put(
                f"/approvals/policies/{sol}",
                json={"action_kind": "dispatch.request"},
                headers=sol_bearer,
            ),
            c.delete(f"/users/{sol}/budget", headers=sol_bearer),
            _set_budget(c, sol, limit=100000, headers=sol_bearer),
        ]
        for attempt in attempts:
            assert attempt.status_code == 403, attempt.text
            assert "own" in attempt.json()["detail"]
        assert _activity_count(db_file) == before

        # Both controls are exactly where the operator left them.
        assert c.get(f"/approvals/policies/{sol}", headers=H1).json() == ["issue.close"]
        budget = c.get(f"/users/{sol}/budget", headers=H1).json()
        assert budget["action_limit"] == 100


def test_a_human_admin_still_decides_and_steers(tmp_path):
    db_file = tmp_path / "human.db"
    with TestClient(create_app(db_file)) as c:
        _bootstrap(c)
        sol, sol_bearer = _admin_agent(c, "sol@e.com")
        issue_id, request_id = _gated_ask(c, sol, sol_bearer)

        approved = c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=H1,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["decided_by"] == 1
        assert (
            c.patch(
                f"/issues/{issue_id}", json={"status": "done"}, headers=sol_bearer
            ).status_code
            == 200
        )

        other = c.post("/issues", json={"title": "second"}, headers=sol_bearer).json()
        second = c.patch(
            f"/issues/{other['id']}", json={"status": "done"}, headers=sol_bearer
        ).json()["approval"]["id"]
        rejected = c.post(
            f"/approvals/{second}/decision",
            json={"decision": "reject"},
            headers=H1,
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["state"] == "rejected"

        # Gate, ungate, budget and unbudget the agent: all still the operator's.
        assert (
            c.put(
                f"/approvals/policies/{sol}",
                json={"action_kind": "dispatch.request"},
                headers=H1,
            ).status_code
            == 200
        )
        assert c.delete(
            f"/approvals/policies/{sol}/issue.close", headers=H1
        ).json() == ["dispatch.request"]
        assert _set_budget(c, sol, limit=5).status_code == 200
        assert _set_budget(c, sol, limit=7).json()["action_limit"] == 7
        assert c.delete(f"/users/{sol}/budget", headers=H1).status_code == 204
        assert c.get(f"/users/{sol}/budget", headers=H1).json() is None


def test_the_rule_lives_in_the_command_not_the_route(tmp_path):
    # Any transport that reaches the commands — REST, the browser cockpit, the
    # palette, a future one — inherits the refusal without reimplementing it.
    db_file = tmp_path / "command.db"
    with TestClient(create_app(db_file)) as c:
        _bootstrap(c)
        sol, sol_bearer = _admin_agent(c, "sol@e.com")
        _, request_id = _gated_ask(c, sol, sol_bearer)
        assert _set_budget(c, sol).status_code == 200
    conn = db.connect(db_file)
    try:
        before = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        with pytest.raises(approval_commands.ApprovalDecisionError) as agent_decider:
            approval_commands.decide(
                conn, actor_id=sol, request_id=request_id, decision="approve"
            )
        assert agent_decider.value.kind == "forbidden"
        with pytest.raises(approval_commands.ApprovalDecisionError) as own_gate:
            approval_commands.clear_policy(
                conn, actor_id=sol, target_user_id=sol, action_kind="issue.close"
            )
        assert own_gate.value.kind == "forbidden"
        with pytest.raises(approval_commands.ApprovalDecisionError) as set_own:
            approval_commands.set_policy(
                conn, actor_id=1, target_user_id=1, action_kind="issue.close"
            )
        assert set_own.value.kind == "forbidden"
        with pytest.raises(budget_commands.BudgetCommandError) as own_budget:
            budget_commands.clear_budget(conn, actor_id=sol, target_user_id=sol)
        assert own_budget.value.kind == "forbidden"
        with pytest.raises(budget_commands.BudgetCommandError) as raise_own:
            budget_commands.set_budget(
                conn, actor_id=1, target_user_id=1, window="day", action_limit=9
            )
        assert raise_own.value.kind == "forbidden"
        assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == before
        assert (
            conn.execute(
                "SELECT state FROM approval_requests WHERE id = ?", (request_id,)
            ).fetchone()[0]
            == "pending"
        )
    finally:
        conn.close()


def test_the_cockpit_refuses_self_service_as_an_error_not_a_crash(tmp_path):
    # The browser shares the commands. A human admin acting on their OWN account
    # from the cockpit gets the refusal as a page error; the controls are unchanged.
    db_file = tmp_path / "cockpit.db"
    with TestClient(create_app(db_file)) as c:
        _bootstrap(c)
        assert (
            c.post(
                "/login",
                data={"email": "a@e.com", "password": "pw"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        before = _activity_count(db_file)
        for path, data in (
            ("/admin/agents/1/approval-policy", {"action_kind": "issue.close"}),
            ("/admin/agents/1/budget", {"action_limit": "5", "window": "day"}),
            ("/admin/agents/1/budget/clear", {}),
        ):
            r = c.post(path, data=data, follow_redirects=False)
            assert r.status_code == 303, (path, r.text)
            assert "error=" in r.headers["location"], (path, r.headers["location"])
        assert _activity_count(db_file) == before
        assert c.get("/approvals/policies/1", headers=H1).json() == []
        assert c.get("/users/1/budget", headers=H1).json() is None
