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

        remint = client.post(
            "/tokens", json={"name": "escape", "scopes": ["admin"]}, headers=auth
        )
        assert remint.status_code == 403
        assert remint.json()["detail"] == "token scope required: admin"


def test_read_only_token_cannot_mutate_personal_state(tmp_path):
    # WHY: personal writes (your own saved filters, watches, inbox) don't need the write
    # ROLE — a viewer may manage their own — but a READ-ONLY bearer token must still be
    # refused, or the read-vs-write token boundary leaks: a "read" agent could create
    # filters, watch issues, and clear its owner's inbox.
    app = create_app(tmp_path / "scope_personal.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        issue = client.post(
            "/issues", json={"title": "watchable"}, headers=_AUTH_ADMIN
        ).json()
        reader = _bearer(_mint(client, scopes=["read"], name="reader")["token"])
        writer = _bearer(_mint(client, scopes=["issue:write"], name="writer")["token"])

        # The read token still READS its personal surfaces fine.
        assert client.get("/notifications", headers=reader).status_code == 200
        assert client.get("/filters", headers=reader).status_code == 200

        # ...but every personal WRITE is refused (403) with the write-scope message.
        watch = {"target_kind": "issue", "target_id": issue["id"]}
        for resp in (
            client.post("/filters", json={"name": "mine"}, headers=reader),
            client.post("/watches", json=watch, headers=reader),
            client.post("/notifications/read_all", headers=reader),
        ):
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == "token scope required: a write scope"

        # A write-capable token (issue:write) may do the same personal writes.
        assert (
            client.post("/filters", json={"name": "ok"}, headers=writer).status_code
            == 201
        )
        assert client.post("/watches", json=watch, headers=writer).status_code == 204
        assert client.post("/notifications/read_all", headers=writer).status_code == 200


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


def test_admin_scope_preserves_full_token_access_and_omitted_scopes_are_refused(
    tmp_path,
):
    app = create_app(tmp_path / "scope_admin.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        # Omitting scopes used to silently mint ADMIN (fail-open); it is now a
        # clear 422 — an agent-credential mint must say what it may do.
        refused = client.post(
            "/tokens", json={"name": "default"}, headers={"X-Athena-Actor": "1"}
        )
        assert refused.status_code == 422
        assert "scopes are required" in refused.json()["detail"]

        explicit_admin = _mint(client, scopes=["admin"], name="admin")
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


def test_token_scope_migration_defaults_existing_tokens_to_admin(
    tmp_path, migration_inventory_through
):
    migration_inventory_through("0020_token_scopes.sql")
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


def test_read_only_token_cannot_start_a_playbook(tmp_path):
    # WHY: found by the scope audit for MCP tool filtering. The playbook route
    # shipped on current_actor, so a READ-ONLY token could create a parent issue
    # and children — a mutation by the one token class that must never mutate.
    # Starting a playbook is an Aegis write and takes the same scope POST /issues
    # itself requires.
    app = create_app(tmp_path / "scope_playbook.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        space = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=_AUTH_ADMIN
        ).json()
        page = client.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Deploy", "body": "- [ ] step one"},
            headers=_AUTH_ADMIN,
        ).json()
        label = client.post(
            "/labels", json={"name": "playbook"}, headers=_AUTH_ADMIN
        ).json()
        client.post(
            f"/pages/{page['id']}/labels",
            json={"label_id": label["id"]},
            headers=_AUTH_ADMIN,
        )
        reader = _bearer(_mint(client, scopes=["read"], name="pb-reader")["token"])

        denied = client.post(
            f"/pages/{page['id']}/start-playbook", json={}, headers=reader
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "token scope required: issue:write"
        # Nothing was created: the refusal happened at the boundary.
        assert client.get("/issues", headers=_AUTH_ADMIN).json() == []

        # A token with the right scope still starts it — the gate narrows tokens,
        # not the feature.
        writer = _bearer(
            _mint(client, scopes=["read", "issue:write"], name="pb-writer")["token"]
        )
        started = client.post(
            f"/pages/{page['id']}/start-playbook", json={}, headers=writer
        )
        assert started.status_code == 201, started.text


def test_read_only_token_cannot_advance_the_desk_cursor(tmp_path):
    # WHY: same audit, same rule. The cursor is personal state, and personal
    # state is still state — "a read-only bearer token must never mutate
    # anything" includes an agent's own read receipt.
    app = create_app(tmp_path / "scope_cursor.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        client.post("/issues", json={"title": "one event"}, headers=_AUTH_ADMIN)
        reader = _bearer(_mint(client, scopes=["read"], name="desk-reader")["token"])

        # The desk itself is a read and stays open to a read token.
        desk = client.get("/desk", headers=reader)
        assert desk.status_code == 200
        latest = desk.json()["signals"]["latest_visible_event_id"]

        denied = client.post("/desk/cursor", json={"after_id": latest}, headers=reader)
        assert denied.status_code == 403
        assert denied.json()["detail"] == "token scope required: a write scope"

        # Any write scope suffices — the personal-state rule, not a module gate.
        writer = _bearer(
            _mint(client, scopes=["read", "docs:write"], name="desk-writer")["token"]
        )
        assert (
            client.post(
                "/desk/cursor", json={"after_id": latest}, headers=writer
            ).status_code
            == 200
        )
