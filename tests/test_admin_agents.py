"""Admin agent overview regressions."""

from fastapi.testclient import TestClient

from athena.aegis import contributors, issues, projects
from athena.core import access, activity, db, tokens, users
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
