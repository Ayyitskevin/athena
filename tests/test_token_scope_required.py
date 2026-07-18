"""Minting a token now requires an explicit scope choice — no fail-open default.

DEFAULT_SCOPES = (ADMIN_SCOPE,) meant POST /tokens with `scopes` omitted silently
minted ADMIN: forgetting a field granted everything, in an agent-credential
product. These tests pin the inversion: an omitted/None scopes list is a clear
422 on every mint surface, the empty-list rejection stays, and LEGACY stored
rows with no scopes value still read as full access (parse_scopes owns that
compatibility; new mints can never create such a row).
"""
import pytest
from fastapi.testclient import TestClient

from athena.core import activity, db, tokens
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="scopes.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def test_rest_mint_without_scopes_is_422_and_mints_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post("/tokens", json={"name": "oops"}, headers=H1)
        assert r.status_code == 422
        assert "scopes are required" in r.json()["detail"]

    conn = db.connect(db_file)
    assert conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"] == 0
    # The refused mint is not a lifecycle moment — nothing on the trail either.
    assert [
        e for e in activity.list_activity(conn, limit=50) if e["verb"] == "minted_token"
    ] == []
    conn.close()


def test_normalize_scopes_refuses_none_and_empty():
    with pytest.raises(ValueError, match="scopes are required"):
        tokens.normalize_scopes(None)
    with pytest.raises(ValueError, match="at least one"):
        tokens.normalize_scopes([])


def test_legacy_stored_rows_still_read_as_full_access(tmp_path):
    # A row minted before scopes existed has NULL/empty scopes; it must keep its
    # meaning (full access) — the compatibility lives on the READ path only.
    assert tokens.parse_scopes(None) == (tokens.ADMIN_SCOPE,)
    assert tokens.parse_scopes("") == (tokens.ADMIN_SCOPE,)

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        raw = c.post(
            "/tokens", json={"name": "t", "scopes": ["admin"]}, headers=H1
        ).json()["token"]
        # Simulate a legacy row: blank the stored scopes for this token (the
        # column is NOT NULL; pre-scope rows were backfilled to '').
        conn = db.connect(db_file)
        conn.execute("UPDATE api_tokens SET scopes = ''")
        conn.commit()
        conn.close()
        # The legacy token still authenticates with full access.
        r = c.get("/users", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200


def test_web_form_with_no_boxes_checked_is_still_a_clear_400(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post("/login", data={"email": "a@e.com", "password": "pw"}, follow_redirects=False)
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        r = c.post("/settings/tokens", data={"name": "no-boxes"})
        assert r.status_code == 400
        assert "at least one token scope" in r.text
