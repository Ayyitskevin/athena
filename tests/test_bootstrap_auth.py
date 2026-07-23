"""Tests for the bootstrap-authentication hardening (pre-hosting).

The contract these encode:

- The X-Athena-Actor header is NOT trusted by default. An unconfigured instance
  exposed to the network must reject a spoofed identity header — only a real
  bearer token (or an explicitly enabled header on a trusted box) authenticates.
- First-run bootstrap still works: the very first user can be created without
  authentication (nobody could be authenticated yet), and the header can be
  explicitly enabled to mint that first user's first token.
- After bootstrap, user management (create/list/show) requires authentication,
  so an exposed instance can't be enumerated or have accounts minted anonymously.

The suite-wide conftest enables the header by default (it models a trusted local
box); tests here that assert the locked-down default flip it back OFF explicitly.
"""

from fastapi.testclient import TestClient

from athena import config
from athena.main import create_app


def _bootstrap_user(client, email="boot@e.com", name="Boot") -> int:
    """Create the first user — allowed without auth on a fresh install."""
    r = client.post("/users", json={"email": email, "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- the locked-down default ---------------------------------------------------


def test_default_rejects_actor_header_spoof(tmp_path, monkeypatch):
    # WHY (load-bearing): with the header untrusted, claiming to be user 1 proves
    # nothing. A write authenticated only by X-Athena-Actor must be rejected.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "spoof.db")
    with TestClient(app) as client:
        uid = _bootstrap_user(client)
        spoof = client.post(
            "/issues",
            json={"title": "as someone else"},
            headers={"X-Athena-Actor": str(uid)},
        )
        assert spoof.status_code == 401


def test_explicit_enable_allows_actor_header(tmp_path, monkeypatch):
    # WHY: the header path is not removed — on a trusted box you may enable it
    # (the bootstrap mode). Then the same write authenticates and succeeds.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
    app = create_app(tmp_path / "enabled.db")
    with TestClient(app) as client:
        uid = _bootstrap_user(client)
        ok = client.post(
            "/issues",
            json={"title": "on a trusted box"},
            headers={"X-Athena-Actor": str(uid)},
        )
        assert ok.status_code == 201
        assert ok.json()["created_by"] == uid


# --- first-run bootstrap still possible ---------------------------------------


def test_first_user_bootstrap_works_with_header_off(tmp_path, monkeypatch):
    # WHY: bootstrap can't depend on the header — on a fresh, locked-down install
    # there is nobody to authenticate as, so the FIRST user must be creatable
    # without auth regardless of the header setting.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "first.db")
    with TestClient(app) as client:
        r = client.post("/users", json={"email": "founder@e.com", "name": "Founder"})
        assert r.status_code == 201
        assert r.json()["id"] == 1


def test_bootstrap_then_enable_header_to_mint_first_token(tmp_path, monkeypatch):
    # WHY: the documented bootstrap flow end to end — create the first user, turn
    # the header on just long enough to mint that user's first bearer token, then
    # the token alone authenticates and the header can go back off.
    app = create_app(tmp_path / "flow.db")
    with TestClient(app) as client:
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        uid = _bootstrap_user(client, "admin@e.com", "Admin")

        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
        minted = client.post(
            "/tokens",
            json={"name": "bootstrap", "scopes": ["admin"]},
            headers={"X-Athena-Actor": str(uid)},
        )
        assert minted.status_code == 201
        raw = minted.json()["token"]

        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        ok = client.get("/tokens", headers={"Authorization": f"Bearer {raw}"})
        assert ok.status_code == 200


# --- user management requires auth after bootstrap ----------------------------


def test_second_user_create_requires_auth(tmp_path, monkeypatch):
    # WHY: once a user exists, anonymous account creation is the hole — an exposed
    # instance must not let a stranger add a user (and then bootstrap a token).
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "second.db")
    with TestClient(app) as client:
        _bootstrap_user(client)
        # No auth, and the header is untrusted: rejected.
        denied = client.post("/users", json={"email": "intruder@e.com", "name": "X"})
        assert denied.status_code == 401


def test_authenticated_user_management_works(tmp_path, monkeypatch):
    # WHY: with the header enabled (trusted box), an authenticated actor can fully
    # manage users — create a second, list, and show.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
    app = create_app(tmp_path / "manage.db")
    with TestClient(app) as client:
        _bootstrap_user(client)
        auth = {"X-Athena-Actor": "1"}

        second = client.post(
            "/users", json={"email": "colleague@e.com", "name": "Coll"}, headers=auth
        )
        assert second.status_code == 201

        listing = client.get("/users", headers=auth)
        assert listing.status_code == 200
        assert len(listing.json()) == 2

        show = client.get("/users/2", headers=auth)
        assert show.status_code == 200
        assert show.json()["email"] == "colleague@e.com"


def test_user_listing_requires_auth(tmp_path, monkeypatch):
    # WHY: don't let an exposed instance be enumerated anonymously.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "enum.db")
    with TestClient(app) as client:
        _bootstrap_user(client)
        assert client.get("/users").status_code == 401
        assert client.get("/users/1").status_code == 401
