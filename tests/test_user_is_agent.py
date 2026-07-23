"""The is_agent flag — telling agent accounts apart from people.

Athena's actors are people AND agents (both are rows in users; an agent simply
acts through API tokens). This flag is the display/audit distinction between them:
it badges an agent in the delegation list and lets an admin mark accounts, but it
grants and restricts NOTHING — roles still govern that. These tests pin that
contract end to end: the column defaults to human, an admin can flip it (API + web),
the value surfaces on UserOut, and an agent contributor is badged on an issue.
"""

import re

from fastapi.testclient import TestClient

from athena.core import db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _bootstrap_admin(client):
    r = client.post(
        "/users", json={"email": "admin@e.com", "name": "Admin", "password": "secret"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email="admin@e.com", password="secret"):
    r = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- data layer -------------------------------------------------------------


def test_default_human_and_set_agent_round_trips(tmp_path):
    # WHY: every existing user must stay a person after the migration (DEFAULT 0),
    # and the value must read back as a real bool, not SQLite's 0/1.
    db_path = tmp_path / "agent.db"
    with TestClient(create_app(db_path)):
        pass  # boot once to run migrations
    conn = db.connect(db_path)
    try:
        human = users.create_user(conn, email="p@e.com", name="Person")
        assert human["is_agent"] is False

        bot = users.create_user(conn, email="bot@e.com", name="Bot", is_agent=True)
        assert bot["is_agent"] is True

        flipped = users.set_agent(conn, human["id"], True)
        assert flipped["is_agent"] is True
        assert users.get_user(conn, human["id"])["is_agent"] is True

        # set_agent on a missing user is a clean None, not a crash.
        assert users.set_agent(conn, 9999, True) is None

        listed = {u["email"]: u["is_agent"] for u in users.list_users(conn)}
        assert listed == {"p@e.com": True, "bot@e.com": True}
    finally:
        conn.close()


# --- API --------------------------------------------------------------------


def test_create_with_is_agent_and_userout_shape(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        _bootstrap_admin(client)  # first user: human by default
        created = client.post(
            "/users",
            json={"email": "agent@e.com", "name": "Agent", "is_agent": True},
            headers=H1,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["is_agent"] is True
        # UserOut never leaks the password hash even as a new column flows through.
        assert "password_hash" not in body

        admin_row = client.get("/users/1", headers=H1).json()
        assert admin_row["is_agent"] is False


def test_admin_can_toggle_agent_via_api(tmp_path):
    with TestClient(create_app(tmp_path / "toggle.db")) as client:
        _bootstrap_admin(client)
        member = client.post(
            "/users", json={"email": "m@e.com", "name": "Member"}, headers=H1
        ).json()

        on = client.put(
            f"/users/{member['id']}/agent", json={"is_agent": True}, headers=H1
        )
        assert on.status_code == 200, on.text
        assert on.json()["is_agent"] is True

        off = client.put(
            f"/users/{member['id']}/agent", json={"is_agent": False}, headers=H1
        )
        assert off.json()["is_agent"] is False

        missing = client.put("/users/999/agent", json={"is_agent": True}, headers=H1)
        assert missing.status_code == 404


def test_agent_toggle_requires_admin(tmp_path):
    # WHY: marking accounts shapes how everyone reads activity — not self-serve.
    with TestClient(create_app(tmp_path / "guard.db")) as client:
        _bootstrap_admin(client)
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "Member", "role": "member"},
            headers=H1,
        ).json()
        denied = client.put(
            "/users/1/agent",
            json={"is_agent": True},
            headers={"X-Athena-Actor": str(member["id"])},
        )
        assert denied.status_code == 403


# --- web admin --------------------------------------------------------------


def test_admin_users_page_toggles_and_badges_agent(tmp_path):
    db_path = tmp_path / "web.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)
        member = client.post(
            "/users", json={"email": "m@e.com", "name": "Member"}, headers=H1
        ).json()

        # The create form's agent checkbox marks a brand-new account.
        client.post(
            "/admin/users",
            data={"email": "bot@e.com", "name": "Botty", "is_agent": "1"},
            follow_redirects=False,
        )
        conn = db.connect(db_path)
        try:
            assert users.get_user_by_email(conn, "bot@e.com")["is_agent"] is True
        finally:
            conn.close()

        # Toggle the existing member on, then confirm the badge renders.
        marked = client.post(
            f"/admin/users/{member['id']}/agent",
            data={"is_agent": "1"},
            follow_redirects=False,
        )
        assert marked.status_code == 303, marked.text
        page = client.get("/admin/users")
        assert 'class="agent-badge"' in page.text

        # And toggling back to human sticks.
        client.post(
            f"/admin/users/{member['id']}/agent",
            data={"is_agent": "0"},
            follow_redirects=False,
        )
        conn = db.connect(db_path)
        try:
            assert users.get_user(conn, member["id"])["is_agent"] is False
        finally:
            conn.close()


# --- delegation UI ----------------------------------------------------------


def test_agent_contributor_is_badged_on_issue(tmp_path):
    with TestClient(create_app(tmp_path / "deleg.db")) as client:
        _bootstrap_admin(client)
        _login(client)
        bot = client.post(
            "/users",
            json={"email": "bot@e.com", "name": "Botty", "is_agent": True},
            headers=H1,
        ).json()
        issue = client.post("/issues", json={"title": "ship it"}, headers=H1).json()
        client.post(
            f"/issues/{issue['id']}/contributors",
            json={"user_id": bot["id"]},
            headers=H1,
        )

        page = client.get(f"/aegis/issues/{issue['id']}")
        assert page.status_code == 200
        # The agent contributor carries the badge, and the delegate select marks them.
        assert re.search(r"Botty\s*<span class=\"agent-badge\">agent</span>", page.text)
        assert "Botty (agent)" in page.text
