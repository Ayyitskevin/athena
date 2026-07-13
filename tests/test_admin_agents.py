"""Admin agent overview regressions."""

from fastapi.testclient import TestClient

from athena.aegis import contributors, issues, projects
from athena.core import access, activity, db, run_context, tokens, users
from athena.main import create_app
from athena.mentor import spaces

H_ADMIN = {"X-Athena-Actor": "1"}


def _bootstrap_admin(client, *, email="admin@e.com", password="secret"):
    r = client.post(
        "/users",
        json={"email": email, "name": "Admin", "password": password},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_user(
    client,
    email,
    name,
    *,
    role=users.MEMBER_ROLE,
    password="secret",
    is_agent=False,
):
    r = client.post(
        "/users",
        json={
            "email": email,
            "name": name,
            "password": password,
            "role": role,
            "is_agent": is_agent,
        },
        headers=H_ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email="admin@e.com", password="secret"):
    r = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _record_with_run(conn, *, actor_id, run_id, parent_run_id=None, target_id=1):
    run_token = run_context.set_run_id(run_id)
    parent_token = run_context.set_parent_run_id(parent_run_id)
    try:
        return activity.record(
            conn,
            actor_id=actor_id,
            verb="changed_status",
            target_kind="issue",
            target_id=target_id,
            detail="open to done",
        )
    finally:
        run_context.reset_parent_run_id(parent_token)
        run_context.reset_run_id(run_token)


def test_agents_admin_requires_admin(tmp_path):
    db_path = tmp_path / "agent_admin_guard.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _create_user(client, "member@e.com", "Member")

        anonymous = client.get("/admin/agents")
        assert anonymous.status_code == 401

        _login(client, "member@e.com")
        denied = client.get("/admin/agents")
        assert denied.status_code == 403
        assert "Admin role required" in denied.text


def test_agents_admin_empty_state(tmp_path):
    db_path = tmp_path / "agent_admin_empty.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)

        page = client.get("/admin/agents")
        assert page.status_code == 200
        assert "No agent accounts yet" in page.text
        assert "/admin/users" in page.text


def test_agents_admin_shows_tokens_access_assignments_and_activity(tmp_path):
    db_path = tmp_path / "agent_admin_populated.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        admin = _bootstrap_admin(client)
        agent = _create_user(
            client,
            "review-bot@e.com",
            "Review Bot",
            is_agent=True,
        )
        _create_user(client, "human@e.com", "Human Reviewer")

        conn = db.connect(db_path)
        try:
            tokens.create_token(
                conn,
                user_id=agent["id"],
                name="review-bot-ci",
                scopes=[tokens.READ_SCOPE, tokens.ISSUE_WRITE_SCOPE],
            )
            old = tokens.create_token(
                conn,
                user_id=agent["id"],
                name="old-review-bot",
                scopes=[tokens.READ_SCOPE],
            )
            assert tokens.revoke_token(
                conn, user_id=agent["id"], token_id=old["id"]
            )

            project = projects.create_project(
                conn, name="Launch", key="LAN", created_by=admin["id"]
            )
            space = spaces.create_space(
                conn, key="KB", name="Knowledge Base", created_by=admin["id"]
            )
            assert access.add_project_member(
                conn, project["id"], agent["id"], admin["id"]
            )
            assert access.add_space_member(conn, space["id"], agent["id"], admin["id"])

            issue = issues.create_issue(
                conn,
                title="Draft launch plan",
                body="",
                created_by=admin["id"],
                project_id=project["id"],
            )
            assert contributors.add_contributor(
                conn, issue["id"], agent["id"], admin["id"]
            )
            activity.record(
                conn,
                actor_id=agent["id"],
                verb="changed_status",
                target_kind="issue",
                target_id=issue["id"],
                detail="open to in_progress",
            )
        finally:
            conn.close()

        _login(client)
        page = client.get("/admin/agents")
        assert page.status_code == 200
        body = page.text

        assert "Review Bot" in body
        assert "review-bot@e.com" in body
        assert "Human Reviewer" not in body
        assert "review-bot-ci" in body
        assert "read, issue:write" in body
        assert "old-review-bot" in body
        assert "revoked" in body
        assert "Launch" in body
        assert "Knowledge Base" in body
        assert "LAN-1" in body
        assert "Draft launch plan" in body
        assert "changed status" in body
        assert "open to in_progress" in body


def test_agents_admin_shows_token_posture_warnings(tmp_path):
    db_path = tmp_path / "agent_token_posture.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        no_live = _create_user(client, "offline@e.com", "Offline Bot", is_agent=True)
        admin_bot = _create_user(client, "adminbot@e.com", "Admin Bot", is_agent=True)
        never_bot = _create_user(client, "never@e.com", "Never Bot", is_agent=True)
        stale_bot = _create_user(client, "stale@e.com", "Stale Bot", is_agent=True)
        fresh_bot = _create_user(client, "fresh@e.com", "Fresh Bot", is_agent=True)
        _create_user(client, "human@e.com", "Human")

        conn = db.connect(db_path)
        try:
            revoked = tokens.create_token(
                conn,
                user_id=no_live["id"],
                name="revoked-only",
                scopes=[tokens.READ_SCOPE],
            )
            assert tokens.revoke_token(
                conn, user_id=no_live["id"], token_id=revoked["id"]
            )

            admin_token = tokens.create_token(
                conn,
                user_id=admin_bot["id"],
                name="operator-admin",
                scopes=[tokens.ADMIN_SCOPE],
            )
            tokens.create_token(
                conn,
                user_id=never_bot["id"],
                name="never-used",
                scopes=[tokens.READ_SCOPE],
            )
            stale_token = tokens.create_token(
                conn,
                user_id=stale_bot["id"],
                name="stale-reader",
                scopes=[tokens.READ_SCOPE],
            )
            fresh_token = tokens.create_token(
                conn,
                user_id=fresh_bot["id"],
                name="fresh-reader",
                scopes=[tokens.READ_SCOPE],
            )
            conn.execute(
                "UPDATE api_tokens SET last_used_at = datetime('now') WHERE id = ?",
                (admin_token["id"],),
            )
            conn.execute(
                "UPDATE api_tokens SET last_used_at = datetime('now', '-45 days') "
                "WHERE id = ?",
                (stale_token["id"],),
            )
            conn.execute(
                "UPDATE api_tokens SET last_used_at = datetime('now', '-5 days') "
                "WHERE id = ?",
                (fresh_token["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        _login(client)
        page = client.get("/admin/agents")
        assert page.status_code == 200
        body = page.text

        assert "Offline Bot" in body
        assert "No live token" in body
        assert "Admin Bot" in body
        assert "Admin scoped" in body
        assert "Never Bot" in body
        assert "Never used" in body
        assert "Stale Bot" in body
        assert "Stale token" in body
        assert "Fresh Bot" in body
        assert "fresh-reader" in body
        assert "Human" not in body


def test_agent_run_health_requires_admin(tmp_path):
    db_path = tmp_path / "agent_run_health_guard.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        member = _create_user(client, "member@e.com", "Member")

        assert client.get("/admin/agents/runs").status_code == 401
        assert client.get("/activity/agent-runs").status_code == 401
        assert client.get(
            "/activity/agent-runs", headers={"X-Athena-Actor": str(member["id"])}
        ).status_code == 403

        _login(client, "member@e.com")
        denied = client.get("/admin/agents/runs")
        assert denied.status_code == 403


def test_agent_run_health_rolls_up_tagged_and_heuristic_runs(tmp_path):
    db_path = tmp_path / "agent_run_health.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        admin = _bootstrap_admin(client)
        bot = _create_user(client, "bot@e.com", "Bot", is_agent=True)
        _create_user(client, "quiet@e.com", "Quiet Bot", is_agent=True)
        human = _create_user(client, "human@e.com", "Human")

        conn = db.connect(db_path)
        try:
            issue = issues.create_issue(
                conn, title="Replay me", body="", created_by=admin["id"]
            )
            _record_with_run(
                conn,
                actor_id=bot["id"],
                run_id="agent-goal",
                target_id=issue["id"],
            )
            _record_with_run(
                conn,
                actor_id=bot["id"],
                run_id="agent-goal:child",
                parent_run_id="agent-goal",
                target_id=issue["id"],
            )
            activity.record(
                conn,
                actor_id=bot["id"],
                verb="commented",
                target_kind="issue",
                target_id=issue["id"],
                detail="heuristic follow-up",
            )
            _record_with_run(
                conn,
                actor_id=human["id"],
                run_id="human-goal",
                target_id=issue["id"],
            )
        finally:
            conn.close()

        api = client.get(
            "/activity/agent-runs", headers={"X-Athena-Actor": str(admin["id"])}
        )
        assert api.status_code == 200
        payload = api.json()
        assert [row["user"]["id"] for row in payload["agents"]] == [
            bot["id"],
            bot["id"] + 1,
        ]
        assert all(row["user"]["is_agent"] for row in payload["agents"])
        assert all("has_password" not in row["user"] for row in payload["agents"])
        assert all(
            "replay_export_command" not in run
            for row in payload["agents"]
            for run in row["runs"]
        )
        assert payload["totals"]["agents_with_activity_count"] == 1
        assert "active_agent_count" not in payload["totals"]

        filtered_api = client.get(
            f"/activity/agent-runs?agent_id={bot['id']}",
            headers={"X-Athena-Actor": str(admin["id"])},
        ).json()
        assert [row["user"]["id"] for row in filtered_api["agents"]] == [bot["id"]]

        _login(client)
        page = client.get("/admin/agents/runs")
        assert page.status_code == 200
        body = page.text

        assert "Agent Mission Control" in body
        assert "agents with activity" in body
        assert "Bot" in body
        assert "Quiet Bot" in body
        assert "Human" not in body
        assert "Mixed runs" in body
        assert "No activity" in body
        assert "run agent-goal" in body
        assert "run agent-goal:child" in body
        assert "/aegis/activity/runs/agent-goal/lineage" in body
        assert "/admin/agents/runs/agent-goal/replay.json" in body
        assert "athena-export-run /var/lib/athena/athena.db agent-goal /exports/agent-goal.replay.json" in body
        assert (
            "athena-export-run /var/lib/athena/athena.db agent-goal:child "
            "/exports/agent-goal-child.replay.json"
        ) in body
        assert 'data-copy-command="athena-export-run' in body
        assert 'name="agent_id"' in body
        assert "1 child run" in body
        assert "heuristic run" in body

        filtered = client.get(f"/admin/agents/runs?agent_id={bot['id']}")
        assert filtered.status_code == 200
        assert "run agent-goal" in filtered.text
        assert "No activity" not in filtered.text

        human_filter = client.get(f"/admin/agents/runs?agent_id={human['id']}")
        assert human_filter.status_code == 200
        assert "No agent runs match this filter" in human_filter.text
        assert "Human" not in human_filter.text

        replay = client.get("/admin/agents/runs/agent-goal/replay.json")
        assert replay.status_code == 200
        artifact = replay.json()
        assert artifact["run_id"] == "agent-goal"
        assert artifact["event_count"] == 1

        human_replay = client.get("/admin/agents/runs/human-goal/replay.json")
        assert human_replay.status_code == 404
