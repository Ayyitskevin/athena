"""The OIDC flow core — token validation and provision-or-link policy.

The security-critical slice, so the validation is tested with REAL crypto: an RSA
keypair signs ID tokens in-test and decode_id_token validates them, exercising every
rejection path (bad signature, wrong audience/issuer, expiry, missing sub, nonce
mismatch, and the HS256 key-confusion forgery). The provision-or-link policy is
tested against a real DB. Network calls (discovery, token exchange) are stubbed.
"""
import base64
import hashlib
import json
import time
import urllib.request

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from athena.core import db, oidc, oidc_flow, users

ISS = "https://idp.example.com"
CLIENT = "athena-client"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _KEY.public_key()
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OMIT = object()


def _token(key=_KEY, alg="RS256", **overrides):
    now = int(time.time())
    claims = {
        "iss": ISS, "aud": CLIENT, "sub": "sub-1", "iat": now, "exp": now + 3600,
        "nonce": "nonce-1", "email": "a@e.com", "email_verified": True, "name": "A",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not _OMIT}
    return jwt.encode(claims, key, algorithm=alg)


def _decode(token, *, nonce="nonce-1", issuer=ISS, client_id=CLIENT):
    return oidc_flow.decode_id_token(
        token, _PUB, issuer=issuer, client_id=client_id, nonce=nonce
    )


# --- ID token validation ----------------------------------------------------


def test_valid_token_decodes():
    claims = _decode(_token())
    assert claims["sub"] == "sub-1" and claims["email"] == "a@e.com"


def test_bad_signature_rejected():
    # Signed by a key we don't trust → signature check fails.
    with pytest.raises(jwt.InvalidTokenError):
        _decode(_token(key=_OTHER))


def test_wrong_audience_rejected():
    with pytest.raises(jwt.InvalidAudienceError):
        _decode(_token(), client_id="someone-else")


def test_wrong_issuer_rejected():
    with pytest.raises(jwt.InvalidIssuerError):
        _decode(_token(), issuer="https://evil.example.com")


def test_expired_rejected():
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(_token(exp=int(time.time()) - 30, iat=int(time.time()) - 60))


def test_missing_sub_rejected():
    with pytest.raises(jwt.MissingRequiredClaimError):
        _decode(_token(sub=_OMIT))


def test_nonce_mismatch_rejected():
    with pytest.raises(oidc_flow.OidcError):
        _decode(_token(nonce="a-different-nonce"))


def test_absent_nonce_rejected():
    with pytest.raises(oidc_flow.OidcError):
        _decode(_token(nonce=_OMIT))


def test_hmac_forgery_rejected():
    # The classic confusion attack: an attacker who knows the PUBLIC key forges an
    # HS256 token using it as the HMAC secret. We hand-craft that token (PyJWT's own
    # encode refuses to misuse an asymmetric key, so we build the JWT directly) and
    # assert decode rejects it — restricting to asymmetric algorithms means an HS256
    # token is refused before any signature is even checked.
    pub_pem = _PUB.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    def _seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=")

    now = int(time.time())
    header = _seg({"alg": "HS256", "typ": "JWT"})
    payload = _seg({"iss": ISS, "aud": CLIENT, "sub": "x", "exp": now + 60,
                    "iat": now, "nonce": "nonce-1"})
    signing_input = header + b"." + payload
    import hmac
    sig = base64.urlsafe_b64encode(
        hmac.new(pub_pem, signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (signing_input + b"." + sig).decode("ascii")

    with pytest.raises(jwt.InvalidTokenError):
        _decode(forged)


# --- redirect construction + PKCE -------------------------------------------


def test_authorization_url_carries_the_right_params():
    url = oidc_flow.build_authorization_url(
        "https://idp.example.com/authorize",
        client_id=CLIENT, redirect_uri="https://app.example.com/cb",
        state="st-1", nonce="no-1", code_challenge="chal",
    )
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == [CLIENT]
    assert q["redirect_uri"] == ["https://app.example.com/cb"]
    assert q["state"] == ["st-1"] and q["nonce"] == ["no-1"]
    assert q["code_challenge"] == ["chal"] and q["code_challenge_method"] == ["S256"]
    assert "openid" in q["scope"][0]


def test_pkce_challenge_is_unpadded_s256():
    verifier = oidc_flow.new_pkce_verifier()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    challenge = oidc_flow.pkce_challenge(verifier)
    assert challenge == expected and "=" not in challenge


# --- discovery + token exchange (network stubbed) ---------------------------


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(monkeypatch, payload):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload)
    )


def test_discover_returns_endpoints(monkeypatch):
    _stub_urlopen(monkeypatch, {
        "issuer": ISS, "authorization_endpoint": "https://idp/auth",
        "token_endpoint": "https://idp/token", "jwks_uri": "https://idp/jwks",
    })
    doc = oidc_flow.discover(ISS)
    assert doc["token_endpoint"] == "https://idp/token"


def test_discover_rejects_issuer_mismatch(monkeypatch):
    _stub_urlopen(monkeypatch, {
        "issuer": "https://evil.example.com", "authorization_endpoint": "a",
        "token_endpoint": "t", "jwks_uri": "j",
    })
    with pytest.raises(oidc_flow.OidcError):
        oidc_flow.discover(ISS)


def test_exchange_code_requires_id_token(monkeypatch):
    _stub_urlopen(monkeypatch, {"access_token": "x", "token_type": "Bearer"})
    with pytest.raises(oidc_flow.OidcError):
        oidc_flow.exchange_code(
            "https://idp/token", code="c", redirect_uri="r",
            client_id=CLIENT, client_secret="s", code_verifier="v",
        )


# --- provision-or-link policy -----------------------------------------------


def _conn(tmp_path):
    conn = db.connect(tmp_path / "flow.db")
    db.migrate(conn)
    return conn


def test_first_login_provisions_a_member(tmp_path):
    conn = _conn(tmp_path)
    claims = {"sub": "s1", "email": "new@acme.com", "email_verified": True, "name": "New"}
    user = oidc_flow.provision_or_link(conn, issuer=ISS, claims=claims)
    assert user["email"] == "new@acme.com" and user["role"] == users.MEMBER_ROLE
    # Idempotent: a second login resolves by sub to the SAME user, no duplicate.
    again = oidc_flow.provision_or_link(conn, issuer=ISS, claims=claims)
    assert again["id"] == user["id"]
    assert users.count_users(conn) == 1
    conn.close()


def test_existing_account_auto_link_requires_a_domain_allow_list(tmp_path):
    # Auto-linking SSO to a PRE-EXISTING local account by verified email is an
    # account-takeover vector if the IdP's email verification can't be trusted. Without
    # an OIDC_ALLOWED_DOMAINS allow-list, Athena can't make that trust judgement, so the
    # silent link is REFUSED — the pre-existing admin is not seized.
    conn = _conn(tmp_path)
    boss = users.create_user(
        conn, email="boss@acme.com", name="Boss", password="pw", role=users.ADMIN_ROLE
    )
    with pytest.raises(oidc_flow.OidcError):
        oidc_flow.provision_or_link(
            conn, issuer=ISS,
            claims={"sub": "s2", "email": "boss@acme.com", "email_verified": True},
        )
    # No identity was linked, no account created — the admin is untouched.
    assert oidc.find_user_by_identity(conn, issuer=ISS, subject="s2") is None
    assert users.count_users(conn) == 1

    # WITH a domain allow-list the operator has declared this IdP verifies the domain,
    # so the link to the existing admin is trusted and goes through.
    linked = oidc_flow.provision_or_link(
        conn, issuer=ISS,
        claims={"sub": "s2", "email": "boss@acme.com", "email_verified": True},
        allowed_domains=("acme.com",),
    )
    assert linked["id"] == boss["id"] and linked["role"] == users.ADMIN_ROLE
    assert users.count_users(conn) == 1
    conn.close()


def test_unverified_email_is_refused(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(oidc_flow.OidcError):
        oidc_flow.provision_or_link(
            conn, issuer=ISS,
            claims={"sub": "s", "email": "x@acme.com", "email_verified": False},
        )
    assert users.count_users(conn) == 0
    conn.close()


def test_disallowed_domain_is_refused(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(oidc_flow.OidcError):
        oidc_flow.provision_or_link(
            conn, issuer=ISS,
            claims={"sub": "s", "email": "x@evil.com", "email_verified": True},
            allowed_domains=("acme.com",),
        )
    # The allowed domain goes through.
    ok = oidc_flow.provision_or_link(
        conn, issuer=ISS,
        claims={"sub": "s", "email": "x@acme.com", "email_verified": True},
        allowed_domains=("acme.com",),
    )
    assert ok["email"] == "x@acme.com"
    conn.close()


def test_missing_sub_is_refused(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(oidc_flow.OidcError):
        oidc_flow.provision_or_link(
            conn, issuer=ISS,
            claims={"email": "x@acme.com", "email_verified": True},
        )
    conn.close()


def test_already_linked_resolves_by_sub_regardless_of_email(tmp_path):
    # Once linked, later logins resolve by sub — email can change at the IdP and the
    # domain/verified policy no longer gates an established account.
    conn = _conn(tmp_path)
    first = oidc_flow.provision_or_link(
        conn, issuer=ISS,
        claims={"sub": "s", "email": "x@acme.com", "email_verified": True},
        allowed_domains=("acme.com",),
    )
    again = oidc_flow.provision_or_link(
        conn, issuer=ISS,
        claims={"sub": "s", "email": "changed@other.com", "email_verified": False},
        allowed_domains=("acme.com",),
    )
    assert again["id"] == first["id"]
    conn.close()
