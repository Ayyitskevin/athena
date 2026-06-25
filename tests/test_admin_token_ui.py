"""Browser admin and token-management regressions."""

import re

from fastapi.testclient import TestClient

from athena.core import db, tokens, users
from athena.main import create_app


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
):
    r = client.post(
        "/users",
        json={"email": email, "name": name, "password": password, "role": role},
        headers={"X-Athena-Actor": "1"},
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


def _role_for(db_path, user_id):
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,)
        ).fetchone()["role"]
    finally:
        conn.close()


def _token_row(db_path, token_id=None):
    conn = db.connect(db_path)
    try:
        if token_id is None:
            return dict(conn.execute("SELECT * FROM api_tokens ORDER BY id").fetchone())
        return dict(
            conn.execute(
                "SELECT * FROM api_tokens WHERE id = ?", (token_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def _has_password(db_path, user_id):
    conn = db.connect(db_path)
    try:
        return bool(
            conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()["password_hash"]
        )
    finally:
        conn.close()


def test_token_settings_mints_scoped_token_and_hides_secret_on_reload(tmp_path):
    db_path = tmp_path / "token_settings.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)

        page = client.get("/settings/tokens")
        assert page.status_code == 200
        assert "Create token" in page.text

        created = client.post(
            "/settings/tokens",
            data={"name": "ci", "scope_read": "1", "scope_issue_write": "1"},
        )
        assert created.status_code == 201, created.text
        raw = re.search(r"ath_[A-Za-z0-9_-]+", created.text)
        assert raw is not None
        assert "ci" in created.text
        assert "issue:write" in created.text

        row = _token_row(db_path)
        assert row["name"] == "ci"
        assert row["scopes"] == "read issue:write"

        reloaded = client.get("/settings/tokens")
        assert reloaded.status_code == 200
        assert raw.group(0) not in reloaded.text
        assert "ci" in reloaded.text


def test_token_settings_revokes_owned_token(tmp_path):
    db_path = tmp_path / "token_revoke.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)
        created = client.post(
            "/settings/tokens", data={"name": "laptop", "scope_read": "1"}
        )
        assert created.status_code == 201, created.text
        token_id = _token_row(db_path)["id"]

        revoked = client.post(
            f"/settings/tokens/{token_id}/revoke", follow_redirects=False
        )
        assert revoked.status_code == 303
        assert revoked.headers["location"] == "/settings/tokens"
        assert _token_row(db_path, token_id)["revoked_at"] is not None


def test_viewer_can_view_tokens_but_cannot_manage_them(tmp_path):
    db_path = tmp_path / "viewer_tokens.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        viewer = _create_user(client, "viewer@e.com", "Viewer", role=users.VIEWER_ROLE)
        conn = db.connect(db_path)
        try:
            created = tokens.create_token(
                conn,
                user_id=viewer["id"],
                name="viewer-token",
                scopes=[tokens.READ_SCOPE],
            )
        finally:
            conn.close()

        _login(client, "viewer@e.com")
        page = client.get("/settings/tokens")
        assert page.status_code == 200
        assert "viewer-token" in page.text
        assert "Viewer role is read-only." in page.text
        assert "Create Token" not in page.text
        assert f"/settings/tokens/{created['id']}/revoke" not in page.text

        blocked_create = client.post(
            "/settings/tokens", data={"name": "blocked", "scope_read": "1"}
        )
        assert blocked_create.status_code == 403
        assert "Viewer role is read-only" in blocked_create.text

        blocked_revoke = client.post(f"/settings/tokens/{created['id']}/revoke")
        assert blocked_revoke.status_code == 403
        assert _token_row(db_path, created["id"])["revoked_at"] is None


def test_admin_users_page_sets_and_resets_passwords(tmp_path):
    db_path = tmp_path / "admin_passwords.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        target = _create_user(
            client,
            "api-only@e.com",
            "API Only",
            role=users.MEMBER_ROLE,
            password=None,
        )
        assert not _has_password(db_path, target["id"])

        _login(client)
        page = client.get("/admin/users")
        assert page.status_code == 200
        assert "not set" in page.text
        assert f"/admin/users/{target['id']}/password" in page.text

        set_password = client.post(
            f"/admin/users/{target['id']}/password",
            data={"password": "first-pass"},
            follow_redirects=False,
        )
        assert set_password.status_code == 303, set_password.text
        assert _has_password(db_path, target["id"])

        _login(client, "api-only@e.com", "first-pass")

        _login(client)
        reset = client.post(
            f"/admin/users/{target['id']}/password",
            data={"password": "second-pass"},
            follow_redirects=False,
        )
        assert reset.status_code == 303, reset.text

        old_password = client.post(
            "/login",
            data={"email": "api-only@e.com", "password": "first-pass"},
            follow_redirects=False,
        )
        assert old_password.status_code == 401
        _login(client, "api-only@e.com", "second-pass")


def test_admin_password_reset_rejects_non_admin_missing_user_and_blank_password(
    tmp_path,
):
    db_path = tmp_path / "admin_password_guard.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _create_user(client, "member@e.com", "Member", role=users.MEMBER_ROLE)
        target = _create_user(
            client, "api-only@e.com", "API Only", role=users.MEMBER_ROLE, password=None
        )

        _login(client, "member@e.com")
        denied = client.post(
            f"/admin/users/{target['id']}/password", data={"password": "blocked"}
        )
        assert denied.status_code == 403
        assert not _has_password(db_path, target["id"])

        _login(client)
        blank = client.post(
            f"/admin/users/{target['id']}/password", data={"password": "   "}
        )
        assert blank.status_code == 400
        assert "Password is required" in blank.text
        assert not _has_password(db_path, target["id"])

        missing = client.post("/admin/users/999/password", data={"password": "x"})
        assert missing.status_code == 404
        assert "No such user" in missing.text


def test_admin_users_page_creates_users_and_changes_roles(tmp_path):
    db_path = tmp_path / "admin_users.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)

        page = client.get("/admin/users")
        assert page.status_code == 200
        assert "New User" in page.text

        created = client.post(
            "/admin/users",
            data={
                "email": "new@e.com",
                "name": "New User",
                "password": "secret",
                "role": users.VIEWER_ROLE,
            },
            follow_redirects=False,
        )
        assert created.status_code == 303, created.text

        conn = db.connect(db_path)
        try:
            new_user = users.get_user_by_email(conn, "new@e.com")
        finally:
            conn.close()
        assert new_user is not None
        assert new_user["role"] == users.VIEWER_ROLE

        changed = client.post(
            f"/admin/users/{new_user['id']}/role",
            data={"role": users.MEMBER_ROLE},
            follow_redirects=False,
        )
        assert changed.status_code == 303, changed.text
        assert _role_for(db_path, new_user["id"]) == users.MEMBER_ROLE


def test_admin_users_requires_admin_and_preserves_last_admin(tmp_path):
    db_path = tmp_path / "admin_users_guard.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        admin = _bootstrap_admin(client)
        _create_user(client, "member@e.com", "Member", role=users.MEMBER_ROLE)

        _login(client, "member@e.com")
        denied = client.get("/admin/users")
        assert denied.status_code == 403
        assert "Admin role required" in denied.text

        _login(client)
        demoted = client.post(
            f"/admin/users/{admin['id']}/role", data={"role": users.MEMBER_ROLE}
        )
        assert demoted.status_code == 409
        assert "Cannot remove the last admin" in demoted.text
        assert _role_for(db_path, admin["id"]) == users.ADMIN_ROLE


def test_user_role_api_requires_admin_scope_and_keeps_last_admin(tmp_path):
    db_path = tmp_path / "role_api.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        admin = _bootstrap_admin(client)
        member = _create_user(client, "member@e.com", "Member", role=users.MEMBER_ROLE)
        read_token = client.post(
            "/tokens",
            json={"name": "reader", "scopes": [tokens.READ_SCOPE]},
            headers={"X-Athena-Actor": str(admin["id"])},
        ).json()["token"]
        admin_token = client.post(
            "/tokens",
            json={"name": "admin", "scopes": [tokens.ADMIN_SCOPE]},
            headers={"X-Athena-Actor": str(admin["id"])},
        ).json()["token"]

        denied = client.put(
            f"/users/{member['id']}/role",
            json={"role": users.VIEWER_ROLE},
            headers={"Authorization": f"Bearer {read_token}"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "token scope required: admin"

        changed = client.put(
            f"/users/{member['id']}/role",
            json={"role": users.VIEWER_ROLE},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["role"] == users.VIEWER_ROLE

        protected = client.put(
            f"/users/{admin['id']}/role",
            json={"role": users.MEMBER_ROLE},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert protected.status_code == 409
        assert protected.json()["detail"] == "cannot remove the last admin"
        assert _role_for(db_path, admin["id"]) == users.ADMIN_ROLE
