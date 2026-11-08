"""Agent-reported usage: stored as a fact in the event detail, summed three ways, never enforced.

An agent's model spend happens inside its own process, so Athena records what the agent SAYS it
used and labels it that way. The usage object rides on the check-in and on complete_claim, lives
inside the activity detail JSON (no new column), and feeds three read models: per run, per agent
per day, and per issue. Nothing here meters, refuses, or budgets.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from athena.aegis import lease_commands, reported_spend
from athena.core import db, reported_usage
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server

H1 = {"X-Athena-Actor": "1"}
USAGE = {
    "model": "claude-sonnet-5-5",
    "input_tokens": 1200,
    "output_tokens": 300,
    "cached_tokens": 800,
    "estimated_cost": 0.0123,
    "currency": "USD",
    "reported_at": "2026-11-08T10:00:00Z",
}
LABEL = "reported by the agent, not metered by Athena"


def _app(tmp_path):
    client = TestClient(create_app(tmp_path / "spend.sqlite"))
    client.db_path = tmp_path / "spend.sqlite"
    client.__enter__()
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
    return client


def _agent(client, email="sol@e.com", name="Sol"):
    onboarded = client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()
    return onboarded["user"]["id"], {
        "Authorization": f"Bearer {onboarded['token']['token']}"
    }


def _beat(client, bearer, run_id="run-a", usage=None, **extra):
    body = {"run_id": run_id, **extra}
    if usage is not None:
        body["usage"] = usage
    return client.put("/agent-runs/heartbeat", json=body, headers=bearer)


def _events(tmp_path, verb):
    conn = db.connect(tmp_path / "spend.sqlite")
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM activity WHERE verb = ? ORDER BY id", (verb,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _issue(client, title="Work", assignee_id=None, project_id=None):
    response = client.post(
        "/issues", json={"title": title, "project_id": project_id}, headers=H1
    )
    assert response.status_code == 201, response.text
    issue = response.json()
    if assignee_id is not None:
        conn = db.connect(client.db_path)
        try:
            conn.execute(
                "UPDATE issues SET assignee_id = ? WHERE id = ?",
                (assignee_id, issue["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        issue = client.get(f"/issues/{issue['id']}", headers=H1).json()
    return issue


def _claim_and_complete(client, issue, headers, usage=None, run="run-a"):
    headers = {**headers, "X-Athena-Run": run}
    etag = client.get(f"/issues/{issue['id']}", headers=headers).headers["etag"]
    claimed = client.post(
        f"/issues/{issue['id']}/claim", json={}, headers={**headers, "If-Match": etag}
    )
    assert claimed.status_code == 201, claimed.text
    body = {"generation": claimed.json()["generation"]}
    if usage is not None:
        body["usage"] = usage
    return client.post(f"/issues/{issue['id']}/complete", json=body, headers=headers)


def _viewer(tmp_path, user_id):
    conn = db.connect(tmp_path / "spend.sqlite")
    try:
        return conn, dict(
            conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        )
    finally:
        pass


# --- the usage grammar -------------------------------------------------------------------


def test_valid_usage_is_accepted_as_written():
    assert reported_usage.normalize_usage(USAGE) == USAGE
    assert reported_usage.normalize_usage({"input_tokens": 0}) == {"input_tokens": 0}


@pytest.mark.parametrize(
    "bad",
    [
        {"input_tokens": -1},
        {"output_tokens": -5},
        {"cached_tokens": -1},
        {"estimated_cost": -0.01, "currency": "USD"},
        {"input_tokens": 1.5},
        {"input_tokens": True},
        {"input_tokens": "12"},
        {"input_tokens": 10**13},
        {"estimated_cost": float("nan"), "currency": "USD"},
        {"estimated_cost": float("inf"), "currency": "USD"},
        {"estimated_cost": True, "currency": "USD"},
        {"estimated_cost": 1e12, "currency": "USD"},
        {"model": "m", "tokens": 5},
        {"input_tokens": 1, "cost": 3},
        {"estimated_cost": 1.0, "currency": "usd"},
        {"estimated_cost": 1.0, "currency": "DOLLARS"},
        {"estimated_cost": 1.0, "currency": "ZZZ"},
        {"estimated_cost": 1.0, "currency": 840},
        {"estimated_cost": 1.0},
        {"currency": "USD"},
        {"reported_at": "yesterday"},
        {"reported_at": "2026-11-08T10:00:00"},
        {"reported_at": 1731060000},
        {"model": ""},
        {"model": "x" * 101},
        {"model": 7},
        {},
        [],
        "usage",
        None,
    ],
)
def test_malformed_usage_is_refused(bad):
    with pytest.raises(ValueError):
        reported_usage.normalize_usage(bad)


# --- check-in round trip -----------------------------------------------------------------


def test_a_heartbeat_with_usage_stores_a_fact_in_the_event_detail(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        plain = _beat(client, bearer, "run-a")
        assert plain.status_code == 200, plain.text
        assert _events(tmp_path, reported_usage.VERB_USAGE_REPORTED) == []

        response = _beat(client, bearer, "run-a", USAGE)
        assert response.status_code == 200, response.text
        # The response shape is the existing check-in projection and nothing more.
        assert set(response.json()) == set(plain.json())

        (event,) = _events(tmp_path, reported_usage.VERB_USAGE_REPORTED)
        assert event["actor_id"] == agent_id
        assert event["target_kind"] == "user" and event["target_id"] == agent_id
        stored = reported_usage.from_event(event)
        assert stored == {"run_id": "run-a", "usage": USAGE}
        # Inside the detail JSON: the table has no usage column to read it from.
        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(activity)")}
            checkin = {
                r["name"] for r in conn.execute("PRAGMA table_info(agent_run_checkins)")
            }
        finally:
            conn.close()
        assert not [c for c in columns | checkin if "usage" in c or "cost" in c]
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"input_tokens": -1},
        {"mystery": 1},
        {"estimated_cost": 1.0, "currency": "usd"},
        {},
        "tokens",
    ],
)
def test_a_heartbeat_refuses_bad_usage_and_changes_nothing(tmp_path, bad):
    client = _app(tmp_path)
    try:
        _agent_id, bearer = _agent(client)
        # Positive control: the same request with a good object is accepted.
        assert _beat(client, bearer, "run-ok", {"input_tokens": 1}).status_code == 200
        before = len(_events(tmp_path, reported_usage.VERB_USAGE_REPORTED))
        refused = _beat(client, bearer, "run-bad", bad)
        assert refused.status_code == 422, refused.text
        assert len(_events(tmp_path, reported_usage.VERB_USAGE_REPORTED)) == before
        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            row = conn.execute(
                "SELECT 1 FROM agent_run_checkins WHERE run_id = 'run-bad'"
            ).fetchone()
        finally:
            conn.close()
        assert row is None  # a refused report does not even refresh the check-in
    finally:
        client.__exit__(None, None, None)


def test_a_heartbeat_without_usage_is_unchanged(tmp_path):
    client = _app(tmp_path)
    try:
        _agent_id, bearer = _agent(client)
        for _ in range(2):
            assert _beat(client, bearer, "run-a").status_code == 200
        assert _events(tmp_path, reported_usage.VERB_USAGE_REPORTED) == []
        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            assert reported_spend.by_run(conn, viewer=None) == []
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def test_usage_is_not_accepted_by_a_bare_unknown_body_key(tmp_path):
    client = _app(tmp_path)
    try:
        _agent_id, bearer = _agent(client)
        refused = _beat(client, bearer, "run-a", None, spend=1)
        assert refused.status_code == 422
    finally:
        client.__exit__(None, None, None)


# --- complete_claim round trip -----------------------------------------------------------


def test_complete_claim_carries_usage_inside_the_report(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        issue = _issue(client, assignee_id=agent_id)
        done = _claim_and_complete(client, issue, bearer, USAGE)
        assert done.status_code == 200, done.text
        (event,) = _events(tmp_path, "claim_completed")
        report = lease_commands.reported_completion(event)
        assert report == {"usage": USAGE}
        # Usage alone counts as a report; verdict and the rest still ride beside it.
    finally:
        client.__exit__(None, None, None)


def test_complete_claim_usage_travels_with_the_rest_of_a_report(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        issue = _issue(client, assignee_id=agent_id)
        headers = {**bearer, "X-Athena-Run": "r"}
        etag = client.get(f"/issues/{issue['id']}", headers=headers).headers["etag"]
        gen = client.post(
            f"/issues/{issue['id']}/claim",
            json={},
            headers={**headers, "If-Match": etag},
        ).json()["generation"]
        done = client.post(
            f"/issues/{issue['id']}/complete",
            json={
                "generation": gen,
                "verdict": "PASS",
                "unfinished": ["docs"],
                "usage": {"input_tokens": 5},
            },
            headers=headers,
        )
        assert done.status_code == 200, done.text
        (event,) = _events(tmp_path, "claim_completed")
        assert lease_commands.reported_completion(event) == {
            "verdict": "PASS",
            "unfinished": ["docs"],
            "usage": {"input_tokens": 5},
        }
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"input_tokens": -1},
        {"mystery": 1},
        {"estimated_cost": 1.0, "currency": "usd"},
        {},
    ],
)
def test_complete_claim_refuses_bad_usage_and_keeps_the_lease(tmp_path, bad):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        issue = _issue(client, assignee_id=agent_id)
        headers = {**bearer, "X-Athena-Run": "r"}
        etag = client.get(f"/issues/{issue['id']}", headers=headers).headers["etag"]
        gen = client.post(
            f"/issues/{issue['id']}/claim",
            json={},
            headers={**headers, "If-Match": etag},
        ).json()["generation"]
        refused = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": gen, "usage": bad},
            headers=headers,
        )
        assert refused.status_code == 422, refused.text
        assert _events(tmp_path, "claim_completed") == []
        # Positive control: the same lease completes with a good object.
        ok = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": gen, "usage": {"input_tokens": 1}},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
    finally:
        client.__exit__(None, None, None)


def test_a_report_without_usage_is_unchanged(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        issue = _issue(client, assignee_id=agent_id)
        done = _claim_and_complete(client, issue, bearer)
        assert done.status_code == 200
        (event,) = _events(tmp_path, "claim_completed")
        assert "usage" not in event["detail"]
        assert lease_commands.reported_completion(event) is None
        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            assert reported_spend.by_issue(conn, viewer=None) == []
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


# --- credential refusal still applies ----------------------------------------------------


def _token_shaped_model():
    return "gh" + "p_" + "A1" * 18


def test_a_credential_in_the_model_name_is_refused_on_both_surfaces(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        # Positive control: an ordinary model name is accepted on both.
        assert (
            _beat(client, bearer, "r1", {"model": "claude-sonnet-5-5"}).status_code
            == 200
        )
        leaked = {"model": _token_shaped_model()}
        refused = _beat(client, bearer, "r2", leaked)
        assert refused.status_code == 422
        assert (
            "GitHub token" in refused.text and _token_shaped_model() not in refused.text
        )

        issue = _issue(client, assignee_id=agent_id)
        done = _claim_and_complete(client, issue, bearer, leaked)
        assert done.status_code == 422, done.text
        assert "GitHub token" in done.text and _token_shaped_model() not in done.text
        assert _events(tmp_path, "claim_completed") == []
    finally:
        client.__exit__(None, None, None)


def test_stored_usage_stays_readable_without_the_credential_scan():
    # The scan belongs to the accepting writes only; the shared grammar is reused on reads.
    assert reported_usage.normalize_usage({"model": _token_shaped_model()})


# --- the three read models ---------------------------------------------------------------


def test_totals_per_run_per_agent_day_and_per_issue(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        other_id, other = _agent(client, "tau@e.com", "Tau")
        one = _issue(client, "one", assignee_id=agent_id)
        two = _issue(client, "two", assignee_id=agent_id)

        usd = {"estimated_cost": 0.25, "currency": "USD"}
        assert (
            _beat(
                client,
                bearer,
                "run-a",
                {"input_tokens": 100, "output_tokens": 10, **usd},
            ).status_code
            == 200
        )
        assert (
            _beat(
                client, bearer, "run-a", {"input_tokens": 50, "cached_tokens": 5, **usd}
            ).status_code
            == 200
        )
        assert (
            _beat(
                client,
                bearer,
                "run-b",
                {"input_tokens": 7, "estimated_cost": 1, "currency": "EUR"},
            ).status_code
            == 200
        )
        assert _beat(client, other, "run-z", {"input_tokens": 999}).status_code == 200
        assert (
            _claim_and_complete(
                client,
                one,
                bearer,
                {"input_tokens": 1000, "output_tokens": 100, **usd},
                run="run-a",
            ).status_code
            == 200
        )
        assert (
            _claim_and_complete(
                client, two, bearer, {"input_tokens": 2000}, run="run-c"
            ).status_code
            == 200
        )

        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            runs = {
                (r["agent_id"], r["run_id"]): r["totals"]
                for r in reported_spend.by_run(conn, viewer=None)
            }
            assert runs[(agent_id, "run-a")] == {
                "reports": 3,
                "input_tokens": 1150,
                "output_tokens": 110,
                "cached_tokens": 5,
                "cost": {"USD": "0.75"},
            }
            assert runs[(agent_id, "run-b")]["cost"] == {"EUR": "1"}
            assert runs[(agent_id, "run-c")]["input_tokens"] == 2000
            assert runs[(other_id, "run-z")]["input_tokens"] == 999

            days = reported_spend.by_agent_day(conn, viewer=None, agent_id=agent_id)
            assert len(days) == 1  # all of it happened today
            assert days[0]["totals"]["input_tokens"] == 100 + 50 + 7 + 1000 + 2000
            assert days[0]["totals"]["cost"] == {"USD": "0.75", "EUR": "1"}
            assert days[0]["totals"]["reports"] == 5

            issues = {
                r["issue_id"]: r["totals"]
                for r in reported_spend.by_issue(conn, viewer=None)
            }
            assert issues[one["id"]]["input_tokens"] == 1000
            assert issues[two["id"]]["input_tokens"] == 2000
            assert issues[one["id"]]["cost"] == {"USD": "0.25"}
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def test_currencies_are_never_added_together(tmp_path):
    client = _app(tmp_path)
    try:
        _agent_id, bearer = _agent(client)
        _beat(client, bearer, "r", {"estimated_cost": 2, "currency": "USD"})
        _beat(client, bearer, "r", {"estimated_cost": 2, "currency": "EUR"})
        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            (row,) = reported_spend.by_run(conn, viewer=None)
            assert row["totals"]["cost"] == {"EUR": "2", "USD": "2"}
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def test_a_private_project_is_excluded_for_a_viewer_who_cannot_see_it(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw", "role": "member"},
            headers=H1,
        ).json()
        project = client.post(
            "/projects", json={"name": "Ops", "key": "OPS"}, headers=H1
        ).json()
        shown = _issue(
            client, "visible", assignee_id=agent_id, project_id=project["id"]
        )
        hidden_project = client.post(
            "/projects", json={"name": "Vault", "key": "VLT"}, headers=H1
        ).json()
        hidden = _issue(
            client, "secret", assignee_id=agent_id, project_id=hidden_project["id"]
        )
        assert (
            _claim_and_complete(client, shown, bearer, {"input_tokens": 10}).status_code
            == 200
        )
        assert (
            _claim_and_complete(
                client, hidden, bearer, {"input_tokens": 90}
            ).status_code
            == 200
        )
        assert _beat(client, bearer, "r", {"input_tokens": 5}).status_code == 200

        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            conn.execute(
                "UPDATE projects SET visibility='private' WHERE id=?",
                (hidden_project["id"],),
            )
            conn.commit()
            viewer = dict(
                conn.execute(
                    "SELECT * FROM users WHERE id = ?", (member["id"],)
                ).fetchone()
            )
            admin = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())

            # Positive control: the admin sees every report.
            admin_issues = {
                r["issue_id"] for r in reported_spend.by_issue(conn, viewer=admin)
            }
            assert admin_issues == {shown["id"], hidden["id"]}

            seen = {r["issue_id"] for r in reported_spend.by_issue(conn, viewer=viewer)}
            assert seen == {shown["id"]}
            days = reported_spend.by_agent_day(conn, viewer=viewer, agent_id=agent_id)
            assert [d["totals"]["input_tokens"] for d in days] == [10]
            # The agent's own check-in reports have no project and are not the viewer's to see.
            assert reported_spend.by_run(conn, viewer=viewer, agent_id=agent_id) == [
                r
                for r in reported_spend.by_run(conn, viewer=viewer, agent_id=agent_id)
                if r["run_id"] != "r"
            ]
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


# --- the cockpit -------------------------------------------------------------------------


def test_the_cockpit_shows_reported_usage_under_its_honest_label(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        assert (
            client.post(
                "/login",
                data={"email": "a@e.com", "password": "pw"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        empty = client.get("/admin/agents").text
        assert LABEL not in empty  # nothing reported, nothing to caveat

        client.put(
            f"/users/{agent_id}/budget",
            json={"window": "day", "action_limit": 50},
            headers=H1,
        )
        assert (
            _beat(
                client,
                bearer,
                "run-a",
                {
                    "input_tokens": 1234,
                    "output_tokens": 56,
                    "estimated_cost": 0.5,
                    "currency": "USD",
                },
            ).status_code
            == 200
        )
        page = client.get("/admin/agents").text
        assert LABEL in page
        assert "1234" in page and "0.5 USD" in page and "run-a" in page
        # Beside the action budget, which still reads as before.
        assert "actions this day" in page
    finally:
        client.__exit__(None, None, None)


def test_reported_usage_never_changes_the_action_budget_or_blocks_a_write(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        client.put(
            f"/users/{agent_id}/budget",
            json={"window": "day", "action_limit": 1},
            headers=H1,
        )
        before = client.get(f"/users/{agent_id}/budget", headers=H1).json()
        for n in range(5):
            assert (
                _beat(
                    client,
                    bearer,
                    "r",
                    {"input_tokens": 10**9, "estimated_cost": 10**6, "currency": "USD"},
                ).status_code
                == 200
            )
        assert client.get(f"/users/{agent_id}/budget", headers=H1).json() == before
    finally:
        client.__exit__(None, None, None)


# --- MCP ---------------------------------------------------------------------------------


def test_mcp_heartbeat_and_complete_accept_usage(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        client.headers.update(bearer)
        server = build_server(AthenaClient(client=client))
        beat = asyncio.run(
            server.call_tool(
                "heartbeat_agent_run",
                {"run_id": "run-m", "usage": {"input_tokens": 42}},
            )
        )
        assert json.loads(beat[0].text)["run_id"] == "run-m"
        assert reported_usage.from_event(_events(tmp_path, "usage_reported")[0])[
            "usage"
        ] == {"input_tokens": 42}
        tools = {t.name: t.description for t in asyncio.run(server.list_tools())}
        assert (
            "usage" in tools["heartbeat_agent_run"]
            and "usage" in tools["complete_claim"]
        )
        assert "not metered" in tools["complete_claim"]
    finally:
        client.__exit__(None, None, None)


# --- review fold: usage check-ins must not crowd out real actions -------------------------


def _summary(tmp_path, agent_id):
    from athena.core import agents

    conn = db.connect(tmp_path / "spend.sqlite")
    try:
        user = dict(
            conn.execute("SELECT * FROM users WHERE id = ?", (agent_id,)).fetchone()
        )
        return agents._agent_summary(conn, user)
    finally:
        conn.close()


def test_many_usage_check_ins_leave_recent_activity_and_last_active_unchanged(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        issue = _issue(client, assignee_id=agent_id)
        assert _claim_and_complete(client, issue, bearer).status_code == 200
        before = _summary(tmp_path, agent_id)
        assert [e["verb"] for e in before["recent_activity"]][0] == "claim_completed"

        for n in range(10):
            assert (
                _beat(client, bearer, "run-a", {"input_tokens": n}).status_code == 200
            )
        # Positive control: the ten reports are real events and the ledger reads them.
        assert len(_events(tmp_path, reported_usage.VERB_USAGE_REPORTED)) == 10
        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            (row,) = reported_spend.by_run(conn, viewer=None)
            assert row["totals"]["reports"] == 10
        finally:
            conn.close()

        after = _summary(tmp_path, agent_id)
        assert after["recent_activity"] == before["recent_activity"]
        assert after["last_activity_at"] == before["last_activity_at"]
        page = client.get("/admin/agents", headers=H1)
        assert "reported_usage_v1" not in page.text
    finally:
        client.__exit__(None, None, None)


def test_an_agent_that_only_reports_usage_has_no_recent_work(tmp_path):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        assert _beat(client, bearer, "r", {"input_tokens": 1}).status_code == 200
        summary = _summary(tmp_path, agent_id)
        assert summary["recent_activity"] == [] and summary["last_activity_at"] is None
    finally:
        client.__exit__(None, None, None)


def test_the_cockpit_says_when_the_totals_window_is_full(tmp_path, monkeypatch):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        for _ in range(3):
            _beat(client, bearer, "r", {"input_tokens": 1})
        assert "Recent window only" not in client.get("/admin/agents").text
        monkeypatch.setattr(reported_spend, "SCAN_LIMIT", 3)
        page = client.get("/admin/agents").text
        assert "Recent window only" in page
    finally:
        client.__exit__(None, None, None)


def test_a_non_object_usage_gets_the_same_refusal_shape_as_any_other(tmp_path):
    client = _app(tmp_path)
    try:
        _agent_id, bearer = _agent(client)
        shapes = set()
        for bad in ([], "x", 5, {"input_tokens": -1}):
            refused = _beat(client, bearer, "r", bad)
            assert refused.status_code == 422
            shapes.add(type(refused.json()["detail"]).__name__)
        assert shapes == {"str"}
    finally:
        client.__exit__(None, None, None)


def test_a_full_completion_window_also_counts_as_clipped(tmp_path, monkeypatch):
    client = _app(tmp_path)
    try:
        agent_id, bearer = _agent(client)
        issue = _issue(client, assignee_id=agent_id)
        assert (
            _claim_and_complete(client, issue, bearer, {"input_tokens": 1}).status_code
            == 200
        )
        conn = db.connect(tmp_path / "spend.sqlite")
        try:
            assert reported_spend.scan(conn, None).clipped is False
            monkeypatch.setattr(reported_spend, "SCAN_LIMIT", 1)
            assert reported_spend.scan(conn, None).clipped is True
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)
