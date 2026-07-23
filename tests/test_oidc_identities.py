"""SSO identity links — binding an external OIDC (issuer, subject) to an Athena user.

These pin the contract the login flow will rest on: a linked identity resolves to
its user (so SSO can start a session like a password login would); an unlinked one
is a clean None (a first login); one provider-identity maps to exactly one user (the
composite PK refuses a re-link); the SAME subject under different issuers is distinct
(sub is only unique within an issuer); and a link to a missing user is refused by the
foreign key.
"""

import sqlite3

import pytest

from athena.core import db, oidc, users

ISS = "https://idp.example.com"


def _conn(tmp_path):
    conn = db.connect(tmp_path / "oidc.db")
    db.migrate(conn)
    return conn


def _user(conn, email="a@e.com", name="A"):
    return users.create_user(conn, email=email, name=name)["id"]


def test_link_then_resolve_to_user(tmp_path):
    conn = _conn(tmp_path)
    uid = _user(conn)
    link = oidc.link_identity(conn, issuer=ISS, subject="sub-1", user_id=uid)
    assert link["user_id"] == uid and link["created_at"]

    found = oidc.find_user_by_identity(conn, issuer=ISS, subject="sub-1")
    assert found is not None and found["id"] == uid
    # The resolved user is the canonical shape (a session can start from it).
    assert found["email"] == "a@e.com"
    conn.close()


def test_unlinked_identity_is_none(tmp_path):
    conn = _conn(tmp_path)
    _user(conn)
    # A first SSO login: the identity exists at the IdP but isn't linked here yet.
    assert oidc.find_user_by_identity(conn, issuer=ISS, subject="nobody") is None
    conn.close()


def test_one_identity_maps_to_one_user(tmp_path):
    # WHY: re-linking the same (issuer, subject) to a different user would make
    # "who is this login" ambiguous — the composite PK refuses it.
    conn = _conn(tmp_path)
    u1 = _user(conn, "a@e.com", "A")
    u2 = _user(conn, "b@e.com", "B")
    oidc.link_identity(conn, issuer=ISS, subject="sub-1", user_id=u1)
    with pytest.raises(sqlite3.IntegrityError):
        oidc.link_identity(conn, issuer=ISS, subject="sub-1", user_id=u2)
    conn.close()


def test_same_subject_distinct_across_issuers(tmp_path):
    # WHY: `sub` is only unique WITHIN an issuer, so the key must include the issuer.
    conn = _conn(tmp_path)
    u1 = _user(conn, "a@e.com", "A")
    u2 = _user(conn, "b@e.com", "B")
    other = "https://other-idp.example.com"
    oidc.link_identity(conn, issuer=ISS, subject="shared", user_id=u1)
    oidc.link_identity(conn, issuer=other, subject="shared", user_id=u2)

    assert oidc.find_user_by_identity(conn, issuer=ISS, subject="shared")["id"] == u1
    assert oidc.find_user_by_identity(conn, issuer=other, subject="shared")["id"] == u2
    conn.close()


def test_list_and_unlink_identities(tmp_path):
    conn = _conn(tmp_path)
    uid = _user(conn)
    oidc.link_identity(conn, issuer=ISS, subject="sub-1", user_id=uid)
    oidc.link_identity(
        conn, issuer="https://other.example.com", subject="x", user_id=uid
    )

    listed = oidc.list_identities(conn, uid)
    assert {i["issuer"] for i in listed} == {ISS, "https://other.example.com"}

    assert oidc.unlink_identity(conn, issuer=ISS, subject="sub-1") is True
    assert oidc.find_user_by_identity(conn, issuer=ISS, subject="sub-1") is None
    # Unlinking what isn't there is a clean False (the caller can 404).
    assert oidc.unlink_identity(conn, issuer=ISS, subject="sub-1") is False
    # The other link and the user row are untouched.
    assert len(oidc.list_identities(conn, uid)) == 1
    assert users.get_user(conn, uid) is not None
    conn.close()


def test_link_to_missing_user_is_refused(tmp_path):
    # WHY: a link must point at a real account — the FK refuses an orphan.
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        oidc.link_identity(conn, issuer=ISS, subject="sub-1", user_id=999)
    conn.close()
