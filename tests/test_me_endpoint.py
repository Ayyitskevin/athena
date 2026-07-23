"""GET /users/me — identity-and-scope introspection.

An agent that starts life with only a bearer token has, until this endpoint, no
way to learn WHO it is, what role it holds, or which scopes its token carries
short of attempting an action and reading the 403. /users/me answers all three
from data current_actor already resolved — no DB read, no migration.

These pin: the scopes reported match the token's effective (normalized) set; an
admin token collapses to ["admin"]; non-token auth (the trusted actor-header
path) reports scopes=null because it is not scope-limited; missing/invalid creds
are 401; the route resolves to the caller's own identity (not shadowed by
/{user_id}, and not admin-gated); and the password hash never leaks.
"""

from fastapi.testclient import TestClient

from athena import config
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # the bootstrap admin, via the trusted header


def _bootstrap_admin(client):
    r = client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _user(client, email, name, role="member", is_agent=False):
    r = client.post(
        "/users",
        json={"email": email, "name": name, "role": role, "is_agent": is_agent},
        headers=H1,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mint(client, *, scopes=None, actor_id=1, name="tok"):
    payload = {"name": name}
    if scopes is not None:
        payload["scopes"] = scopes
    r = client.post("/tokens", json=payload, headers={"X-Athena-Actor": str(actor_id)})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_token_scopes_are_reported_verbatim(tmp_path):
    with TestClient(create_app(tmp_path / "me_scopes.db")) as client:
        _bootstrap_admin(client)
        token = _mint(client, scopes=["issue:write", "read"], name="scoped")

        me = client.get("/users/me", headers=_bearer(token))
        assert me.status_code == 200, me.text
        body = me.json()
        # Normalized into canonical SCOPES order (read, issue:write, ...).
        assert body["scopes"] == ["read", "issue:write"]
        assert body["email"] == "admin@e.com"
        assert body["role"] == "admin"


def test_admin_token_scope_collapses(tmp_path):
    with TestClient(create_app(tmp_path / "me_admin.db")) as client:
        _bootstrap_admin(client)
        # admin subsumes everything, so the stored/effective set collapses to it.
        token = _mint(client, scopes=["read", "admin"], name="super")

        body = client.get("/users/me", headers=_bearer(token)).json()
        assert body["scopes"] == ["admin"]


def test_actor_header_auth_has_null_scopes(tmp_path):
    with TestClient(create_app(tmp_path / "me_header.db")) as client:
        _bootstrap_admin(client)
        me = client.get("/users/me", headers=H1)  # no bearer token, trusted header
        assert me.status_code == 200, me.text
        body = me.json()
        # Non-token auth is not scope-limited — null distinguishes "no token cap"
        # from "a token that allows everything".
        assert body["scopes"] is None
        assert body["id"] == 1 and body["role"] == "admin"


def test_self_introspection_is_not_admin_gated_or_shadowed(tmp_path):
    with TestClient(create_app(tmp_path / "me_self.db")) as client:
        _bootstrap_admin(client)
        uid = _user(client, "agent@e.com", "Worker", role="member", is_agent=True)
        token = _mint(client, actor_id=uid, name="worker", scopes=["read"])

        body = client.get("/users/me", headers=_bearer(token)).json()
        # A non-admin reads its OWN identity (the /{user_id} route would 403 a member
        # reading another user); "me" resolved here, not as a user-id path param.
        assert body["id"] == uid
        assert body["email"] == "agent@e.com"
        assert body["is_agent"] is True


def test_password_hash_never_leaks(tmp_path):
    with TestClient(create_app(tmp_path / "me_leak.db")) as client:
        # First user gets a password, so the row carries a hash that must NOT surface.
        client.post(
            "/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}
        )
        body = client.get("/users/me", headers=H1).json()
        assert "password_hash" not in body
        assert "_token_scopes" not in body and "_token_id" not in body


def test_unauthenticated_and_invalid_are_401(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "me_401.db")) as client:
        _bootstrap_admin(client)
        # An invalid bearer token is rejected regardless of the header trust mode.
        assert client.get("/users/me", headers=_bearer("ath_nope")).status_code == 401
        # With the actor-header trust OFF and no token, there is no actor → 401.
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        assert client.get("/users/me").status_code == 401
        assert client.get("/users/me", headers=H1).status_code == 401
