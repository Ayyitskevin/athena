"""Role and permission regression tests.

Athena has three coarse roles:

- admin: can manage users and perform normal writes.
- member: can perform normal writes but not administer users.
- viewer: can authenticate and read, but cannot mutate state.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

_AUTH_ADMIN = {"X-Athena-Actor": "1"}


def _create_user(client, email, name, *, role=None, password=None, actor="1"):
    payload = {"email": email, "name": name}
    if role is not None:
        payload["role"] = role
    if password is not None:
        payload["password"] = password
    headers = {"X-Athena-Actor": actor} if actor is not None else {}
    r = client.post("/users", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _csrf_login(client, email, password):
    r = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_bootstrap_user_is_admin_and_later_users_default_member(tmp_path):
    app = create_app(tmp_path / "roles_bootstrap.db")
    with TestClient(app) as client:
        first = client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
        assert first.status_code == 201
        assert first.json()["role"] == "admin"

        second = _create_user(client, "member@e.com", "Member")
        assert second["role"] == "member"


def test_only_admin_can_create_users_after_bootstrap(tmp_path):
    app = create_app(tmp_path / "roles_admin.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
        member = _create_user(client, "member@e.com", "Member")

        denied = client.post(
            "/users",
            json={"email": "other@e.com", "name": "Other"},
            headers={"X-Athena-Actor": str(member["id"])},
        )
        assert denied.status_code == 403

        listing = client.get("/users", headers={"X-Athena-Actor": str(member["id"])})
        assert listing.status_code == 403
        own = client.get(f"/users/{member['id']}", headers={"X-Athena-Actor": str(member["id"])})
        assert own.status_code == 200


def test_admin_can_create_viewer(tmp_path):
    app = create_app(tmp_path / "roles_viewer_create.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "admin@e.com", "name": "Admin"})

        viewer = _create_user(client, "viewer@e.com", "Viewer", role="viewer")
        assert viewer["role"] == "viewer"


def test_viewer_can_read_but_cannot_write_api_resources(tmp_path):
    app = create_app(tmp_path / "roles_api.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
        viewer = _create_user(client, "viewer@e.com", "Viewer", role="viewer")

        issue = client.post(
            "/issues", json={"title": "readable"}, headers=_AUTH_ADMIN
        ).json()
        readable = client.get(f"/issues/{issue['id']}", headers={"X-Athena-Actor": str(viewer["id"])})
        assert readable.status_code == 200

        create_issue = client.post(
            "/issues",
            json={"title": "blocked"},
            headers={"X-Athena-Actor": str(viewer["id"])},
        )
        assert create_issue.status_code == 403
        assert create_issue.json()["detail"] == "viewer role is read-only"

        comment = client.post(
            f"/issues/{issue['id']}/comments",
            json={"body": "blocked"},
            headers={"X-Athena-Actor": str(viewer["id"])},
        )
        assert comment.status_code == 403

        token = client.post(
            "/tokens",
            json={"name": "viewer-token"},
            headers={"X-Athena-Actor": str(viewer["id"])},
        )
        assert token.status_code == 403


def test_member_write_permissions_stay_intact(tmp_path):
    app = create_app(tmp_path / "roles_member.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
        member = _create_user(client, "member@e.com", "Member")
        auth = {"X-Athena-Actor": str(member["id"])}

        issue = client.post("/issues", json={"title": "by member"}, headers=auth)
        assert issue.status_code == 201
        comment = client.post(
            f"/issues/{issue.json()['id']}/comments",
            json={"body": "member comment"},
            headers=auth,
        )
        assert comment.status_code == 201


def test_viewer_web_controls_hidden_and_writes_rejected(tmp_path):
    app = create_app(tmp_path / "roles_web.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
        viewer = _create_user(
            client,
            "viewer@e.com",
            "Viewer",
            role="viewer",
            password="secret",
        )
        issue = client.post(
            "/issues", json={"title": "web readable"}, headers=_AUTH_ADMIN
        ).json()

        _csrf_login(client, "viewer@e.com", "secret")
        dashboard = client.get("/aegis")
        assert dashboard.status_code == 200
        assert "/aegis/issues/new" not in dashboard.text

        detail = client.get(f"/aegis/issues/{issue['id']}")
        assert detail.status_code == 200
        assert "Viewer role is read-only." in detail.text
        assert "comment-form" not in detail.text
        assert "Quick Actions" in detail.text
        assert "Change Status" not in detail.text

        post_issue = client.post("/aegis/issues", data={"title": "blocked"})
        assert post_issue.status_code == 403
        assert "Viewer role is read-only" in post_issue.text

        comment = client.post(
            f"/aegis/issues/{issue['id']}/comments", data={"body": "blocked"}
        )
        assert comment.status_code == 403
        assert "Viewer role is read-only" in comment.text

        # The session is really viewer-owned, not falling back to user 1.
        assert viewer["id"] != 1


def test_migration_promotes_existing_first_user_to_admin(tmp_path):
    db_file = tmp_path / "roles_migration.db"
    conn = db.connect(db_file)
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("INSERT INTO users (email, name) VALUES ('first@e.com', 'First')")
    conn.execute("INSERT INTO users (email, name) VALUES ('second@e.com', 'Second')")
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if path.name != "0019_user_roles.sql":
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (path.name,))
    conn.commit()

    applied = db.migrate(conn)
    rows = conn.execute("SELECT id, role FROM users ORDER BY id").fetchall()
    conn.close()

    assert applied == ["0019_user_roles.sql"]
    assert [dict(row) for row in rows] == [
        {"id": 1, "role": "admin"},
        {"id": 2, "role": "member"},
    ]
