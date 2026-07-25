"""Human-in-the-loop approval gates.

VISION.md's Intervene step promises the operator can "approve/reject risky
actions". The only gate before this was the blocked-close policy, which can
refuse but cannot ASK. These pin the gate + retry contract: a gated action is
refused with a recorded ask, approving opens the gate for exactly ONE retry by
that requester against that target, the retry re-validates everything, a
rejection is an answer rather than a delay, and gating stays strictly opt-in.

The mechanism is deliberately NOT deferred execution: Athena never stores an
agent's mutation and replays it later on the agent's behalf, which would
re-execute a stale payload under authorization evaluated at a different moment.
"""

import pytest
from fastapi.testclient import TestClient

from athena.core import approvals, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="approvals.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _gate(client, user_id, action_kind="issue.close"):
    r = client.put(
        f"/approvals/policies/{user_id}",
        json={"action_kind": action_kind},
        headers=H1,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _close(client, issue_id, headers):
    return client.patch(f"/issues/{issue_id}", json={"status": "done"}, headers=headers)


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, target_id, detail FROM activity WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _status(client, issue_id):
    return client.get(f"/issues/{issue_id}", headers=H1).json()["status"]


def test_ungated_actors_close_freely(tmp_path):
    # Opt-in: applying migration 0063 changes nothing until an operator gates someone.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue = c.post("/issues", json={"title": "free"}, headers=_bearer(agent)).json()
        assert _close(c, issue["id"], _bearer(agent)).status_code == 200
        assert _status(c, issue["id"]) == "done"
        assert c.get("/approvals", headers=H1).json() == []


def test_an_agent_can_read_its_own_gates(tmp_path):
    # Same principle as `scopes` and `budget`: an agent learns its limits by
    # asking, not by provoking a refusal.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        assert (
            c.get("/users/me", headers=_bearer(agent)).json()["approval_required"] == []
        )
        _gate(c, agent["user"]["id"])
        assert c.get("/users/me", headers=_bearer(agent)).json()[
            "approval_required"
        ] == ["issue.close"]


def test_gated_close_is_refused_and_recorded_then_approved_once(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _gate(c, agent_id)
        issue = c.post(
            "/issues", json={"title": "gated"}, headers=_bearer(agent)
        ).json()

        refused = _close(c, issue["id"], {**_bearer(agent), "X-Athena-Run": "sol-1"})
        # 202: the ASK was accepted, the action was not.
        assert refused.status_code == 202
        body = refused.json()
        assert body["code"] == "approval_required"
        assert body["approval"]["state"] == "pending"
        assert body["approval"]["action_kind"] == "issue.close"
        assert body["approval"]["target_id"] == issue["id"]
        # The ask joins the run story rather than floating outside it.
        assert body["approval"]["run_id"] == "sol-1"
        # The close did NOT happen.
        assert _status(c, issue["id"]) == "open"
        request_id = body["approval"]["id"]

        # Retrying while waiting re-reads the same ask instead of flooding the queue.
        again = _close(c, issue["id"], _bearer(agent))
        assert again.status_code == 202
        assert again.json()["approval"]["id"] == request_id
        assert [
            a["id"] for a in c.get("/approvals?state=pending", headers=H1).json()
        ] == [request_id]

        # The operator approves; the AGENT retries and it now succeeds.
        decided = c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve", "note": "reviewed"},
            headers=H1,
        )
        assert decided.status_code == 200
        assert decided.json()["state"] == "approved"
        assert _close(c, issue["id"], _bearer(agent)).status_code == 200
        assert _status(c, issue["id"]) == "done"

        # SINGLE-USE: the approval is spent. A second gated close needs a new ask.
        c.patch(f"/issues/{issue['id']}", json={"status": "open"}, headers=H1)
        second = _close(c, issue["id"], _bearer(agent))
        assert second.status_code == 202
        assert second.json()["approval"]["id"] != request_id

    assert [e["detail"] for e in _events(db_file, "approval_consumed")] == [
        f"issue.close (approval #{request_id})"
    ]
    assert len(_events(db_file, "approval_requested")) == 2
    assert len(_events(db_file, "approval_approved")) == 1


def test_rejection_is_an_answer_not_a_delay(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        _gate(c, agent["user"]["id"])
        issue = c.post("/issues", json={"title": "no"}, headers=_bearer(agent)).json()
        request_id = _close(c, issue["id"], _bearer(agent)).json()["approval"]["id"]

        c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "reject", "note": "not this one"},
            headers=H1,
        )
        # 409, not 202: the agent should stop retrying, not keep waiting.
        blocked = _close(c, issue["id"], _bearer(agent))
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "approval_rejected"
        assert "not this one" in blocked.json()["detail"]
        assert _status(c, issue["id"]) == "open"

        # A settled request cannot be re-decided into a different answer.
        assert (
            c.post(
                f"/approvals/{request_id}/decision",
                json={"decision": "approve"},
                headers=H1,
            ).status_code
            == 409
        )
    assert len(_events(db_file, "approval_rejected")) == 1


def test_an_approval_is_bound_to_its_requester_and_target(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        first = _agent(c)
        second = _agent(c, email="luna@e.com")
        _gate(c, first["user"]["id"])
        _gate(c, second["user"]["id"])
        mine = c.post("/issues", json={"title": "mine"}, headers=_bearer(first)).json()
        other = c.post(
            "/issues", json={"title": "other"}, headers=_bearer(first)
        ).json()

        request_id = _close(c, mine["id"], _bearer(first)).json()["approval"]["id"]
        c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=H1,
        )
        # Approving agent A closing issue X must not let A close a DIFFERENT issue…
        assert _close(c, other["id"], _bearer(first)).status_code == 202
        # …nor let a different agent close X.
        c.post(
            f"/issues/{mine['id']}/delegate",
            json={"user_id": second["user"]["id"]},
            headers=H1,
        )
        assert _close(c, mine["id"], _bearer(second)).status_code == 202
        assert _status(c, mine["id"]) == "open"
        # The original pairing still works.
        assert _close(c, mine["id"], _bearer(first)).status_code == 200


def test_the_retry_revalidates_rather_than_replaying(tmp_path):
    # An approval authorizes an INTENT, never a stored side effect: the retry is an
    # ordinary write, so every other guard still applies. Here the agent's durable
    # budget is exhausted between approval and retry — the close is refused despite
    # holding a valid approval, and the approval stays unspent for a later retry.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _gate(c, agent_id)
        issue = c.post(
            "/issues", json={"title": "gated"}, headers=_bearer(agent)
        ).json()
        request_id = _close(c, issue["id"], _bearer(agent)).json()["approval"]["id"]
        c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=H1,
        )
        c.put(
            f"/users/{agent_id}/budget",
            json={"window": "day", "action_limit": 0},
            headers=H1,
        )
        assert _close(c, issue["id"], _bearer(agent)).status_code == 429
        assert _status(c, issue["id"]) == "open"
        # The approval was NOT consumed by the refused retry.
        assert (
            c.get(f"/approvals/{request_id}", headers=H1).json()["consumed_at"] is None
        )

        c.delete(f"/users/{agent_id}/budget", headers=H1)
        assert _close(c, issue["id"], _bearer(agent)).status_code == 200
        assert c.get(f"/approvals/{request_id}", headers=H1).json()["consumed_at"]


def test_humans_and_automation_are_not_gated(tmp_path):
    from athena.aegis import automation

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        # Gating the admin would be odd, but it proves the gate follows the POLICY,
        # not the actor's kind: an ungated human closes freely.
        issue = c.post("/issues", json={"title": "human"}, headers=H1).json()
        assert _close(c, issue["id"], H1).status_code == 200

        conn = db.connect(db_file)
        system_id = automation.system_actor_id(conn)
        conn.commit()
        conn.close()
        # Even fully gated, the operator's own automation must never wait on the
        # operator: rule firings go through the ungated command path.
        _gate(c, system_id)
        conn = db.connect(db_file)
        automation.create_rule(
            conn,
            name="close it",
            trigger_verb="created",
            action_type="set_status",
            action_params={"status": "done"},
            created_by=1,
        )
        conn.commit()
        conn.close()
        target = c.post("/issues", json={"title": "auto close"}, headers=H1).json()
        conn = db.connect(db_file)
        fired = automation.process_pending(
            conn,
            executor=lambda cx, r, e: automation.execute_action(
                cx, r, e, actor_id=automation.system_actor_id(cx)
            ),
        )
        conn.commit()
        conn.close()
        # Two `created` events were pending (the human issue's and the target's),
        # so the rule fires twice. What matters is that a fully gated automation
        # actor completed both closes without ever queuing an ask.
        assert fired == 2
        assert _status(c, target["id"]) == "done"
        assert c.get("/approvals", headers=H1).json() == []


def test_queue_and_policy_surfaces_are_admin_only(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _gate(c, agent_id)
        issue = c.post("/issues", json={"title": "x"}, headers=_bearer(agent)).json()
        _close(c, issue["id"], _bearer(agent))

        # The gated agent cannot read the queue, decide its own ask, or ungate itself.
        assert c.get("/approvals", headers=_bearer(agent)).status_code == 403
        request_id = c.get("/approvals?state=pending", headers=H1).json()[0]["id"]
        assert (
            c.post(
                f"/approvals/{request_id}/decision",
                json={"decision": "approve"},
                headers=_bearer(agent),
            ).status_code
            == 403
        )
        assert (
            c.delete(
                f"/approvals/policies/{agent_id}/issue.close", headers=_bearer(agent)
            ).status_code
            == 403
        )
        assert c.get(f"/approvals/policies/{agent_id}", headers=H1).json() == [
            "issue.close"
        ]


def test_policy_is_idempotent_and_its_vocabulary_is_closed(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        assert _gate(c, agent_id) == ["issue.close"]
        # Re-gating records nothing new.
        assert _gate(c, agent_id) == ["issue.close"]
        # An unknown action kind is refused rather than stored as an inert policy.
        assert (
            c.put(
                f"/approvals/policies/{agent_id}",
                json={"action_kind": "issue.delete_everything"},
                headers=H1,
            ).status_code
            == 422
        )
        assert (
            c.put(
                "/approvals/policies/9999",
                json={"action_kind": "issue.close"},
                headers=H1,
            ).status_code
            == 404
        )
        # Ungating is idempotent too.
        assert (
            c.delete(f"/approvals/policies/{agent_id}/issue.close", headers=H1).json()
            == []
        )
        assert (
            c.delete(f"/approvals/policies/{agent_id}/issue.close", headers=H1).json()
            == []
        )
        assert c.get(f"/approvals/{9999}", headers=H1).status_code == 404
        # Reading or ungating a policy for a user who does not exist is a 404,
        # not an empty list — a typo'd id must not read as "already ungated".
        assert c.get("/approvals/policies/9999", headers=H1).status_code == 404
        assert (
            c.delete("/approvals/policies/9999/issue.close", headers=H1).status_code
            == 404
        )
    assert len(_events(db_file, "approval_policy_set")) == 1
    assert len(_events(db_file, "approval_policy_cleared")) == 1


def test_decide_rejects_an_unknown_decision_and_request(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    with pytest.raises(approvals.ApprovalDecisionError) as bad_decision:
        approvals.decide(conn, actor_id=1, request_id=1, decision="maybe")
    assert bad_decision.value.status_code == 422
    with pytest.raises(approvals.ApprovalDecisionError) as missing:
        approvals.decide(conn, actor_id=1, request_id=999, decision="approve")
    assert missing.value.status_code == 404
    # An anonymous actor has no policy to consult — the gate is per-identity.
    approvals.require(
        conn, None, action_kind="issue.close", target_kind="issue", target_id=1
    )
    conn.close()


def test_cockpit_shows_and_decides_the_queue(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        r = c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        agent = _agent(c)
        agent_id = agent["user"]["id"]

        # Gate from the cockpit, then provoke an ask.
        assert (
            c.post(
                f"/admin/agents/{agent_id}/approval-policy",
                data={"action_kind": "issue.close", "gate": "on"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        issue = c.post(
            "/issues", json={"title": "gate me"}, headers=_bearer(agent)
        ).json()
        request_id = _close(c, issue["id"], _bearer(agent)).json()["approval"]["id"]

        page = c.get("/admin/agents")
        assert "Waiting on you" in page.text
        assert "issue.close" in page.text

        assert (
            c.post(
                f"/admin/approvals/{request_id}/decision",
                data={"decision": "approve", "note": "go ahead"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert _close(c, issue["id"], _bearer(agent)).status_code == 200

        # Deciding it again is refused, and surfaces as an error rather than a flip.
        conflict = c.post(
            f"/admin/approvals/{request_id}/decision",
            data={"decision": "reject"},
            follow_redirects=False,
        )
        assert "error=" in conflict.headers["location"]

        # Ungate from the cockpit.
        assert (
            c.post(
                f"/admin/agents/{agent_id}/approval-policy",
                data={"action_kind": "issue.close", "gate": "off"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert c.get(f"/approvals/policies/{agent_id}", headers=H1).json() == []
    assert len(_events(db_file, "approval_approved")) == 1
