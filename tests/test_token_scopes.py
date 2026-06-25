"""Scoped API-token permission tests.

Token scopes narrow bearer-token authority below the user's role:

- read: authenticated reads only
- issue:write: Aegis writes
- docs:write: Mentor writes
- admin: token management, user admin endpoints, and all writes
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

_AUTH_ADMIN = {"X-Athena-Actor": "1"}


def _bootstrap_admin(client) -> int:
    r = client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mint(client, *, scopes=None, actor_id=1, name="scoped") -> dict:
    payload = {"name": name}
    if scopes is not None:
        payload["scopes"] = scopes
    r = client.post("/tokens", json=payload, headers={"X-Athena-Actor": str(actor_id)})
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_read_only_token_can_read_but_cannot_write(tmp_path):
    app = create_app(tmp_path / "scope_read.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        issue = client.post(
            "/issues", json={"title": "readable"}, headers=_AUTH_ADMIN
        ).json()
        token = _mint(client, scopes=["read"], name="reader")
        auth = _bearer(token["token"])

        listing = client.get("/tokens", headers=auth)
        assert listing.status_code == 200
        assert listing.json()[0]["scopes"] == ["read"]
        assert client.get(f"/issues/{issue['id']}", headers=auth).status_code == 200

        denied = client.post("/issues", json={"title": "blocked"}, headers=auth)
        assert denied.status_code == 403
        assert denied.json()["detail"] == "token scope required: issue:write"

        remint = client.post("/tokens", json={"name": "escape"}, headers=auth)
        assert remint.status_code == 403
        assert remint.json()["detail"] == "token scope required: admin"


def test_issue_write_token_cannot_write_docs_or_admin_users(tmp_path):
    app = create_app(tmp_path / "scope_issue.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        token = _mint(client, scopes=["issue:write"], name="issues")
        auth = _bearer(token["token"])

        issue = client.post("/issues", json={"title": "allowed"}, headers=auth)
        assert issue.status_code == 201

        docs = client.post(
            "/spaces",
            json={"key": "ENG", "name": "Engineering"},
            headers=auth,
        )
        assert docs.status_code == 403
        assert docs.json()["detail"] == "token scope required: docs:write"

        user = client.post(
            "/users",
            json={"email": "new@e.com", "name": "New"},
            headers=auth,
        )
        assert user.status_code == 403
        assert user.json()["detail"] == "token scope required: admin"


def test_docs_write_token_cannot_write_issues(tmp_path):
    app = create_app(tmp_path / "scope_docs.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        token = _mint(client, scopes=["docs:write"], name="docs")
        auth = _bearer(token["token"])

        space = client.post(
            "/spaces",
            json={"key": "DOCS", "name": "Docs"},
            headers=auth,
        )
        assert space.status_code == 201

        issue = client.post("/issues", json={"title": "blocked"}, headers=auth)
        assert issue.status_code == 403
        assert issue.json()["detail"] == "token scope required: issue:write"


def test_admin_scope_and_default_scope_preserve_full_token_access(tmp_path):
    app = create_app(tmp_path / "scope_admin.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        default_token = _mint(client, name="default")
        explicit_admin = _mint(client, scopes=["admin"], name="admin")

        assert default_token["scopes"] == ["admin"]
        assert explicit_admin["scopes"] == ["admin"]

        auth = _bearer(explicit_admin["token"])
        assert (
            client.post("/issues", json={"title": "issue"}, headers=auth).status_code
            == 201
        )
        assert (
            client.post(
                "/spaces", json={"key": "ADM", "name": "Admin"}, headers=auth
            ).status_code
            == 201
        )
        user = client.post(
            "/users",
            json={"email": "member@e.com", "name": "Member"},
            headers=auth,
        )
        assert user.status_code == 201


def test_scope_validation_and_normalization(tmp_path):
    app = create_app(tmp_path / "scope_validation.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)

        duplicate = _mint(client, scopes=["docs:write", "read", "read"])
        assert duplicate["scopes"] == ["read", "docs:write"]

        with_admin = _mint(client, scopes=["read", "admin"])
        assert with_admin["scopes"] == ["admin"]

        empty = client.post(
            "/tokens", json={"name": "empty", "scopes": []}, headers=_AUTH_ADMIN
        )
        assert empty.status_code == 422
        assert empty.json()["detail"] == "at least one token scope is required"

        invalid = client.post(
            "/tokens",
            json={"name": "bad", "scopes": ["issue-write"]},
            headers=_AUTH_ADMIN,
        )
        assert invalid.status_code == 422


def test_token_scope_migration_defaults_existing_tokens_to_admin(tmp_path):
    db_file = tmp_path / "scope_migration.db"
    conn = db.connect(db_file)
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE api_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')), last_used_at TEXT, revoked_at TEXT)"
    )
    conn.execute("INSERT INTO users (email, name) VALUES ('admin@e.com', 'Admin')")
    conn.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash) VALUES (1, 'legacy', 'hash')"
    )
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if path.name != "0020_token_scopes.sql":
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (path.name,)
            )
    conn.commit()

    applied = db.migrate(conn)
    row = conn.execute("SELECT scopes FROM api_tokens WHERE name = 'legacy'").fetchone()
    conn.close()

    assert applied == ["0020_token_scopes.sql"]
    assert row["scopes"] == "admin"
