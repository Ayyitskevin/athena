"""Admin agents REST API (agent admin V2)."""

from fastapi.testclient import TestClient

from athena.aegis import projects
from athena.core import db, tokens, users
from athena.main import create_app
from athena.mentor import spaces

H_ADMIN = {"X-Athena-Actor": "1"}


def _bootstrap(client):
    r = client.post(
        "/users",
        json={"email": "admin@e.com", "name": "Admin", "password": "secret"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _agent(client, email="bot@e.com", name="Bot"):
    r = client.post(
        "/users",
        json={
            "email": email,
            "name": name,
            "password": "secret",
            "role": users.MEMBER_ROLE,
            "is_agent": True,
        },
        headers=H_ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_agents_api_list_and_detail_and_token_actions(tmp_path):
    db_path = tmp_path / "agents_api.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)

        denied = client.get("/api/admin/agents")
        assert denied.status_code in (401, 403)

        listed = client.get("/api/admin/agents", headers=H_ADMIN)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] == 1
        assert body["agents"][0]["user"]["email"] == "bot@e.com"

        detail = client.get(f"/api/admin/agents/{agent['id']}", headers=H_ADMIN)
        assert detail.status_code == 200
        assert detail.json()["user"]["is_agent"] is True

        minted = client.post(
            f"/api/admin/agents/{agent['id']}/tokens",
            headers=H_ADMIN,
            json={"name": "loop", "scopes": ["read", "issue:write"]},
        )
        assert minted.status_code == 200, minted.text
        assert minted.json()["token"]["token"].startswith("ath_")

        revoked = client.post(
            f"/api/admin/agents/{agent['id']}/tokens/revoke-all",
            headers=H_ADMIN,
        )
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] >= 1

        conn = db.connect(db_path)
        try:
            live = [
                t
                for t in tokens.list_tokens(conn, agent["id"])
                if t["revoked_at"] is None
            ]
            assert live == []
        finally:
            conn.close()


def test_agents_api_membership_and_flag(tmp_path):
    db_path = tmp_path / "agents_membership.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        admin = _bootstrap(client)
        agent = _agent(client, email="writer@e.com", name="Writer")

        conn = db.connect(db_path)
        try:
            project = projects.create_project(
                conn, name="Ops", key="OPS", created_by=admin["id"]
            )
            space = spaces.create_space(
                conn, key="KB", name="KB", created_by=admin["id"]
            )
        finally:
            conn.close()

        grant_p = client.post(
            f"/api/admin/agents/{agent['id']}/memberships",
            headers=H_ADMIN,
            json={"project_id": project["id"]},
        )
        assert grant_p.status_code == 200, grant_p.text
        assert grant_p.json()["granted"] is True

        grant_s = client.post(
            f"/api/admin/agents/{agent['id']}/memberships",
            headers=H_ADMIN,
            json={"space_id": space["id"]},
        )
        assert grant_s.status_code == 200, grant_s.text

        flag = client.patch(
            f"/api/admin/agents/{agent['id']}/agent-flag",
            headers=H_ADMIN,
            json={"is_agent": False},
        )
        assert flag.status_code == 200
        assert flag.json()["user"]["is_agent"] is False

        missing = client.get(f"/api/admin/agents/{agent['id']}", headers=H_ADMIN)
        assert missing.status_code == 404
