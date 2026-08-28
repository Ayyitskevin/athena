"""The ranked "Now" attention queue.

Where the count card tells an operator *that* something needs them, the ranking
lists *what* needs them next. These tests cover the closed signal vocabulary,
visibility, mixed time windows, and the empty-state honesty rule.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from athena.aegis import fleet_attention
from athena.core import activity, budgets, db, security_events
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="ranking.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", scopes=("read", "issue:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _claimed_issue(client, agent, *, title, reporting=True):
    issue = client.post("/issues", json={"title": title}, headers=H1).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    read = client.get(f"/issues/{issue['id']}", headers=_bearer(agent))
    claimed = client.post(
        f"/issues/{issue['id']}/claim",
        headers={
            **_bearer(agent),
            "If-Match": read.headers["ETag"],
            "X-Athena-Run": f"run-{issue['id']}",
        },
    )
    assert claimed.status_code in (200, 201), claimed.text
    if reporting:
        beat = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": f"run-{issue['id']}"},
            headers=_bearer(agent),
        )
        assert beat.status_code == 200, beat.text
    return issue


def _expire_lease(db_file, issue_id):
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE issue_leases SET expires_at = datetime('now', '-1 hour') "
        "WHERE issue_id = ?",
        (issue_id,),
    )
    conn.commit()
    conn.close()


def _set_webhook_failing(db_file, webhook_id, failure_count=3, error="timeout"):
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE webhooks SET failure_count = ?, last_error = ?, "
        "last_attempt_at = datetime('now') WHERE id = ?",
        (failure_count, error, webhook_id),
    )
    conn.commit()
    conn.close()


def _set_rule_failing(db_file, rule_id, error="boom"):
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE automation_rules SET failure_count = ?, last_error = ?, "
        "last_error_at = datetime('now') WHERE id = ?",
        (1, error, rule_id),
    )
    conn.commit()
    conn.close()


def test_ranking_is_admin_only(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = c.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "password": "pw"},
            headers=H1,
        ).json()
        assert c.get("/attention/ranking").status_code == 401
        assert (
            c.get("/attention/ranking", headers={"X-Athena-Actor": str(member["id"])})
            .status_code
            == 403
        )
        assert c.get("/attention/ranking", headers=H1).status_code == 200


def test_empty_state_discloses_denominator(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.get("/attention/ranking", headers=H1)
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["examined"] == 0
        # The total counts considered candidates, not returned rows, so an empty
        # result is not reported as "all clear" without a disclosed denominator.
        assert body["total"] >= 0
        assert body["signals"] == sorted(fleet_attention.RANKING_SIGNALS)


def test_unknown_signal_fails_closed(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.get(
            "/attention/ranking?signals=open_blocker,nonsense",
            headers=H1,
        )
        assert r.status_code == 422
        assert "unknown signal types" in r.json()["detail"]


def test_signal_filter_returns_only_requested_signals(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])

        only_claims = c.get(
            "/attention/ranking?signals=claim_needs_attention",
            headers=H1,
        ).json()
        assert [item["signal"] for item in only_claims["items"]] == [
            "claim_needs_attention"
        ]
        assert only_claims["signals"] == ["claim_needs_attention"]

        only_blockers = c.get(
            "/attention/ranking?signals=open_blocker",
            headers=H1,
        ).json()
        assert only_blockers["items"] == []


def test_open_blocker_signal_surfaces_blocked_issues(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        blocked = c.post("/issues", json={"title": "blocked"}, headers=H1).json()
        blocker = c.post("/issues", json={"title": "blocker"}, headers=H1).json()
        c.post(
            f"/issues/{blocked['id']}/links",
            json={"target_ref": str(blocker["id"]), "relation": "blocked_by"},
            headers=H1,
        )

        ranking = c.get("/attention/ranking?signals=open_blocker", headers=H1).json()
        assert len(ranking["items"]) == 1
        item = ranking["items"][0]
        assert item["signal"] == "open_blocker"
        assert item["source_id"] == blocked["id"]
        assert "blocker" in item["reason"]
        assert item["next_action"] == "operator-link"
        assert item["examined"] == 2
        assert item["total"] == 2


def test_mixed_windows_affect_event_signals_not_state_signals(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)

        # Make a claim need attention — this is standing state, not windowed.
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])

        # Make a security refusal, then age it past the short window so the
        # windowed signal drops while the standing claim remains.
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "wrong"},
            follow_redirects=False,
        )
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE activity SET created_at = datetime('now', '-2 hours') "
            "WHERE verb = ? AND imported_at IS NULL",
            (security_events.VERB_LOGIN_FAILED,),
        )
        conn.commit()
        conn.close()

        short = c.get(
            "/attention/ranking?signals=claim_needs_attention,security_refusal",
            headers=H1,
            params={"window_hours": 1},
        ).json()
        assert any(item["signal"] == "claim_needs_attention" for item in short["items"])
        assert not any(
            item["signal"] == "security_refusal" for item in short["items"]
        )

        # Move the clock forward so the refusal is older than the window, and
        # verify the event-counted signal drops while the standing signal stays.
        future = datetime.now(UTC) + timedelta(days=2)
        future_ranking = fleet_attention.build_attention_ranking(
            db.connect(db_file),
            signals={"claim_needs_attention", "security_refusal"},
            actor={"id": 1, "role": "admin"},
            window_hours=1,
            now=future,
        )
        assert any(
            item.signal == "claim_needs_attention" for item in future_ranking["items"]
        )
        assert not any(
            item.signal == "security_refusal" for item in future_ranking["items"]
        )


def test_failing_webhooks_rank_by_severity(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        wh = c.post(
            "/webhooks",
            json={"url": "https://93.184.216.34/hook"},
            headers=H1,
        ).json()
        _set_webhook_failing(db_file, wh["id"], failure_count=5, error="timeout")

        ranking = c.get(
            "/attention/ranking?signals=failing_webhook", headers=H1
        ).json()
        assert len(ranking["items"]) == 1
        item = ranking["items"][0]
        assert item["signal"] == "failing_webhook"
        assert item["severity"] == "critical"
        assert item["reason"].startswith("5 consecutive failures")
        assert item["next_action"] == "operator-link"


def test_failing_automation_rule_appears(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        rule = c.post(
            "/automation/rules",
            json={
                "name": "test",
                "trigger_verb": "created",
                "action_type": "comment",
                "action_params": {"body": "hello"},
            },
            headers=H1,
        ).json()
        _set_rule_failing(db_file, rule["id"], error="bad action")

        ranking = c.get(
            "/attention/ranking?signals=failing_automation_rule", headers=H1
        ).json()
        assert len(ranking["items"]) == 1
        item = ranking["items"][0]
        assert item["signal"] == "failing_automation_rule"
        assert "bad action" in item["reason"]


def test_pending_approval_appears_and_links(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        c.put(
            f"/approvals/policies/{agent_id}",
            json={"action_kind": "issue.close"},
            headers=H1,
        )
        gated = c.post("/issues", json={"title": "gated"}, headers=_bearer(agent)).json()
        c.patch(
            f"/issues/{gated['id']}",
            json={"status": "done"},
            headers=_bearer(agent),
        )

        ranking = c.get("/attention/ranking?signals=pending_approval", headers=H1).json()
        assert len(ranking["items"]) == 1
        item = ranking["items"][0]
        assert item["signal"] == "pending_approval"
        assert item["source_kind"] == "approval_request"
        assert "issue.close" in item["reason"]
        assert item["next_action"] == "operator-link"


def test_budget_exhaustion_and_security_refusal_are_excluded_when_imported(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)

    conn = db.connect(db_file)
    for verb in (security_events.VERB_LOGIN_FAILED, budgets.VERB_BUDGET_EXHAUSTED):
        activity.record(
            conn,
            actor_id=1,
            verb=verb,
            target_kind="user",
            target_id=1,
            imported_at="2026-01-01 00:00:00",
        )
    conn.close()

    with TestClient(app) as c:
        ranking = c.get(
            "/attention/ranking?signals=budget_exhaustion,security_refusal",
            headers=H1,
        ).json()
        assert ranking["items"] == []


def test_agent_command_authorization_degrades_to_operator_link(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])

        # Admin (actor 1) is NOT the assignee, so an agent-command degrades to a
        # link — the slice never executes a command, and it only labels one when
        # the caller could actually perform it.
        ranking = c.get(
            "/attention/ranking?signals=claim_needs_attention", headers=H1
        ).json()
        item = ranking["items"][0]
        assert item["signal"] == "claim_needs_attention"
        assert item["next_action"] == "operator-link"
        assert item["command"] is None

        # When the caller IS the assignee, the same row surfaces as a command.
        agent_view = fleet_attention.build_attention_ranking(
            db.connect(db_file),
            signals={"claim_needs_attention"},
            actor=agent["user"],
        )
        agent_item = fleet_attention.to_public_rank_item(
            db.connect(db_file), agent_view["items"][0], actor=agent["user"]
        )
        assert agent_item["next_action"] == "agent-command"
        assert agent_item["command"] == "check in"


def test_ranking_sorts_by_severity_then_freshness(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        # A medium-severity budget exhaustion.
        c.put(
            f"/users/{agent['user']['id']}/budget",
            json={"window": "day", "action_limit": 0},
            headers=H1,
        )
        c.post("/issues", json={"title": "over"}, headers=_bearer(agent))
        # A high-severity failing webhook.
        wh = c.post(
            "/webhooks", json={"url": "https://93.184.216.34/hook"}, headers=H1
        ).json()
        _set_webhook_failing(db_file, wh["id"], failure_count=1)

        ranking = c.get("/attention/ranking", headers=H1).json()
        signals = [item["signal"] for item in ranking["items"]]
        assert "failing_webhook" in signals
        assert "budget_exhaustion" in signals
        # High severity rows sort before medium severity rows.
        high_index = next(
            i for i, s in enumerate(signals) if s == "failing_webhook"
        )
        medium_index = next(
            i for i, s in enumerate(signals) if s == "budget_exhaustion"
        )
        assert high_index < medium_index
