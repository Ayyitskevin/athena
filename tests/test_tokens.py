"""Tests for API-token authentication.

These encode the security contract, not just HTTP shapes: a token authenticates
as exactly one user, the raw secret is shown once and never stored, revocation
takes effect immediately, tokens are owner-scoped, and the X-Athena-Actor
fallback can be switched off so only real tokens authenticate.
"""

from fastapi.testclient import TestClient

from athena import config
from athena.core import tokens
from athena.main import create_app


def _user(client, email="kevin@example.com", name="Kevin") -> int:
    # User creation is authenticated after the first (bootstrap) user; act as
    # user 1, who always exists once the first _user has run.
    return client.post(
        "/users",
        json={"email": email, "name": name},
        headers={"X-Athena-Actor": "1"},
    ).json()["id"]


def _mint(client, user_id, name="laptop") -> str:
    """Bootstrap a token via the actor-header fallback; return the raw secret."""
    r = client.post(
        "/tokens",
        json={"name": name, "scopes": ["admin"]},
        headers={"X-Athena-Actor": str(user_id)},
    )
    assert r.status_code == 201
    return r.json()["token"]


def test_token_authenticates_as_its_user(tmp_path):
    # WHY: the whole point of a token is to PROVE identity. An issue created with
    # only a bearer token must be stamped created_by that token's user — no
    # actor header in sight.
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        uid = _user(client)
        raw = _mint(client, uid)
        created = client.post(
            "/issues",
            json={"title": "via token"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert created.status_code == 201
        assert created.json()["created_by"] == uid


def test_raw_secret_returned_once_and_never_stored(tmp_path):
    # WHY: a leak of the tokens table must not yield a usable credential. The raw
    # value appears only in the POST response; the DB holds a hash, and listing
    # never echoes the secret.
    app = create_app(tmp_path / "once.db")
    with TestClient(app) as client:
        uid = _user(client)
        raw = _mint(client, uid)

        listing = client.get("/tokens", headers={"Authorization": f"Bearer {raw}"})
        assert listing.status_code == 200
        items = listing.json()
        assert len(items) == 1
        assert "token" not in items[0]  # raw never echoed on list
        assert "token_hash" not in items[0]  # hash never exposed either

        # The persisted value is the hash, not the raw token.
        from athena.core import db

        c = db.connect(tmp_path / "once.db")
        stored = c.execute("SELECT token_hash FROM api_tokens").fetchone()["token_hash"]
        c.close()
        assert stored != raw
        assert stored == tokens._hash(raw)


def test_unknown_token_is_401(tmp_path):
    app = create_app(tmp_path / "unknown.db")
    with TestClient(app) as client:
        _user(client)  # a user exists, but this token was never issued
        r = client.post(
            "/issues",
            json={"title": "x"},
            headers={"Authorization": "Bearer ath_not-real"},
        )
        assert r.status_code == 401


def test_revoked_token_stops_working_immediately(tmp_path):
    # WHY: revocation is the kill switch. The moment a token is revoked it must
    # fail auth, even though its row still exists (so the owner can audit it).
    app = create_app(tmp_path / "revoke.db")
    with TestClient(app) as client:
        uid = _user(client)
        raw = _mint(client, uid)
        token_id = client.get(
            "/tokens", headers={"Authorization": f"Bearer {raw}"}
        ).json()[0]["id"]

        revoked = client.delete(
            f"/tokens/{token_id}", headers={"Authorization": f"Bearer {raw}"}
        )
        assert revoked.status_code == 204

        after = client.post(
            "/issues",
            json={"title": "after"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert after.status_code == 401


def test_tokens_are_owner_scoped(tmp_path):
    # WHY: one user must not revoke another's token. Cross-owner revoke is a 404,
    # not a silent success, and the victim's token keeps working.
    app = create_app(tmp_path / "scope.db")
    with TestClient(app) as client:
        a = _user(client, "a@example.com", "A")
        b = _user(client, "b@example.com", "B")
        a_raw = _mint(client, a, "a-token")
        b_raw = _mint(client, b, "b-token")
        a_token_id = client.get(
            "/tokens", headers={"Authorization": f"Bearer {a_raw}"}
        ).json()[0]["id"]

        # B tries to revoke A's token.
        attempt = client.delete(
            f"/tokens/{a_token_id}", headers={"Authorization": f"Bearer {b_raw}"}
        )
        assert attempt.status_code == 404
        # A's token is untouched.
        still_ok = client.get("/tokens", headers={"Authorization": f"Bearer {a_raw}"})
        assert still_ok.status_code == 200


def test_actor_header_fallback_can_be_disabled(tmp_path, monkeypatch):
    # WHY: this is the network-exposure gate. With TRUST_ACTOR_HEADER off, the
    # spoofable header buys nothing — only a real bearer token authenticates.
    app = create_app(tmp_path / "gate.db")
    with TestClient(app) as client:
        uid = _user(client)
        raw = _mint(client, uid)  # mint while the fallback is still on

        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)

        # Header alone is now rejected.
        header_only = client.post(
            "/issues", json={"title": "spoof"}, headers={"X-Athena-Actor": str(uid)}
        )
        assert header_only.status_code == 401

        # The real token still works.
        with_token = client.post(
            "/issues",
            json={"title": "legit"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert with_token.status_code == 201


def test_managing_tokens_requires_auth(tmp_path):
    # WHY: you cannot mint a token from nothing. With no auth at all, token
    # management is 401 (the bootstrap path needs the actor header on first use).
    app = create_app(tmp_path / "noauth.db")
    with TestClient(app) as client:
        assert (
            client.post("/tokens", json={"name": "x", "scopes": ["admin"]}).status_code
            == 401
        )
        assert client.get("/tokens").status_code == 401
