"""The OpenID Connect authorization-code flow mechanics (the security core of SSO).

This module is the careful part: it builds the redirect to the IdP, exchanges the
returned code for tokens, and — the bit that actually authenticates someone —
validates the ID token. It also maps a validated token to a local Athena user
(provision-or-link). The web routes (web/auth.py) are a thin wrapper that supplies
the per-request state/nonce and turns the result into a session.

Trust model, made explicit so it can't quietly erode:
  * The ID token signature is verified against the IdP's JWKS, restricted to
    ASYMMETRIC algorithms — never "none", never HMAC (passing a public key as an
    HMAC secret is the classic JWT key-confusion forgery).
  * issuer and audience are checked (the token must be FROM our IdP and FOR us),
    along with exp/iat, and the per-login `nonce` (replay defense).
  * PKCE (S256) binds the code to this client; `state` (handled by the caller) binds
    the callback to this browser.
  * A user is only provisioned/linked from a VERIFIED email — an unverified email is
    refused, so the IdP can't be used to seize an existing account by claiming its
    address.

HTTP uses stdlib urllib (like core/webhooks), so SSO adds only PyJWT to the deps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import sqlite3
import urllib.parse
import urllib.request

import jwt

from athena.core import oidc, oidc_commands, user_commands, users

logger = logging.getLogger(__name__)

# Asymmetric signatures only. Deliberately excludes "none" and all HMAC (HS*)
# algorithms: with HS*, the verifier key is the SAME shared secret used to sign, so
# accepting HS* while we hand jwt.decode the IdP's PUBLIC key would let anyone who
# knows that public key forge a token. JWKS keys are RSA/EC, so this is also correct.
_ID_TOKEN_ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
]

# Scopes we request: openid (required) + email/profile so we can provision a user.
_SCOPES = ("openid", "email", "profile")

_HTTP_TIMEOUT = 10


class OidcError(Exception):
    """A flow-level failure (bad discovery, token exchange, or claim policy). Token
    SIGNATURE/claim failures surface as PyJWT's own jwt.InvalidTokenError subclasses;
    the caller treats both as 'authentication failed'."""


# --- PKCE -------------------------------------------------------------------


def new_pkce_verifier() -> str:
    """A high-entropy PKCE code_verifier (RFC 7636: 43–128 unreserved chars)."""
    return secrets.token_urlsafe(64)


def pkce_challenge(verifier: str) -> str:
    """The S256 code_challenge: base64url(sha256(verifier)), no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state() -> str:
    """An unguessable value tying the callback to the browser that started the flow."""
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    """An unguessable value bound into the ID token to detect replay."""
    return secrets.token_urlsafe(32)


# --- the redirect to the IdP ------------------------------------------------


def build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    """Build the URL we send the browser to so the IdP can authenticate the user."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(_SCOPES),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorization_endpoint}?{urllib.parse.urlencode(params)}"


# --- talking to the IdP (stdlib urllib) -------------------------------------


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def discover(issuer: str) -> dict:
    """Fetch the IdP's OpenID configuration. RFC 8414 requires the document's own
    `issuer` to equal the one we asked about — checking it defends against a
    discovery mix-up pointing us at someone else's endpoints."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = _get_json(url)
    if doc.get("issuer") != issuer:
        raise OidcError("discovery document issuer does not match configured issuer")
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(required):
            raise OidcError(f"discovery document missing {required}")
    return doc


def exchange_code(
    token_endpoint: str,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    code_verifier: str,
) -> dict:
    """Exchange the authorization code for tokens (incl. the id_token). The
    code_verifier proves we are the client that began the flow (PKCE)."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        token_endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
        token = json.loads(resp.read().decode("utf-8"))
    if "id_token" not in token:
        raise OidcError("token response did not include an id_token")
    return token


# --- validating the ID token ------------------------------------------------


def decode_id_token(
    id_token: str,
    signing_key,
    *,
    issuer: str,
    client_id: str,
    nonce: str,
) -> dict:
    """Verify and decode an ID token against a known signing key. Pure (no network),
    so the security checks are exhaustively testable. Raises jwt.InvalidTokenError
    (subclass) on a bad signature/audience/issuer/expiry/missing-claim, or OidcError
    on a nonce mismatch. Returns the claims on success."""
    claims = jwt.decode(
        id_token,
        signing_key,
        algorithms=_ID_TOKEN_ALGORITHMS,
        audience=client_id,
        issuer=issuer,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
    # The nonce ties this token to the redirect we just issued — a replayed token
    # from an earlier login carries a different (or absent) nonce.
    if not nonce or claims.get("nonce") != nonce:
        raise OidcError("id_token nonce does not match this login")
    return claims


def verify_id_token(
    id_token: str,
    *,
    jwks_uri: str,
    issuer: str,
    client_id: str,
    nonce: str,
) -> dict:
    """Fetch the IdP's signing key for this token, then validate it via
    decode_id_token. A fresh PyJWKClient is built per call, so the JWKS document is
    fetched once per login — fine at self-hosted login volume; add a shared cached
    client if that ever changes."""
    signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token).key
    return decode_id_token(
        id_token, signing_key, issuer=issuer, client_id=client_id, nonce=nonce
    )


# --- mapping a validated token to a local user ------------------------------


def provision_or_link(
    conn: sqlite3.Connection,
    *,
    issuer: str,
    claims: dict,
    allowed_domains: tuple[str, ...] = (),
) -> dict:
    """Resolve validated ID-token claims to a local Athena user, creating or linking
    one on first login. Policy:
      * already linked (issuer, sub) → that user, every time (stable across email
        changes, since the link keys on sub);
      * otherwise it's a first login and needs a VERIFIED email — an unverified or
        absent email is refused (so SSO can't seize an account by claiming its
        address);
      * if allowed_domains is set, the email's domain must be in it;
      * a NEW account is provisioned when no local account has that email;
      * an EXISTING local account is auto-linked ONLY when allowed_domains is
        configured — i.e. the operator has declared the domains this IdP verifies, so a
        verified-email match is trustworthy. Without a domain allow-list Athena can't
        know the IdP actually owns the address, so silently binding SSO to a pre-existing
        (possibly admin) account would be an account-takeover vector; it is refused, and
        the user links SSO from their account settings while logged in instead.
    Raises OidcError when the claims don't satisfy the policy."""
    subject = claims.get("sub")
    if not subject:
        raise OidcError("id_token missing sub")

    linked = oidc.find_user_by_identity(conn, issuer=issuer, subject=subject)
    if linked is not None:
        return linked

    email = (claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise OidcError("a verified email is required to sign in via SSO")
    if allowed_domains and email.rsplit("@", 1)[-1] not in allowed_domains:
        raise OidcError("email domain is not allowed to sign in via SSO")

    user = users.get_user_by_email(conn, email)
    if user is None:
        # Fresh SSO account — no pre-existing account to seize, so no takeover risk.
        # Self-attributed (actor_id=None): the person signing in provisions their own
        # account, and the command records the atomic 'created_user' event.
        user = user_commands.create_user(
            conn,
            actor_id=None,
            email=email,
            name=(claims.get("name") or "").strip() or email,
            role=users.DEFAULT_ROLE,
        )
    elif not allowed_domains:
        # An account with this email already exists and no domain allow-list is
        # configured, so we can't trust that this IdP owns the address. Refuse rather
        # than silently binding SSO to a pre-existing (possibly admin) account.
        raise OidcError(
            "an account already exists for this email; set OIDC_ALLOWED_DOMAINS to "
            "enable SSO auto-link, or link SSO from your account settings"
        )
    # Record the identity link (fires once, on first login for this issuer+sub — an
    # already-linked user returned earlier). For an existing account this only runs on
    # the trusted, domain-allow-listed path above. The command binds the identity AND
    # records the atomic 'linked_identity' event, attributed to the user themselves
    # (this IS their sign-in), so a new login credential is never a silent write.
    logger.info("linking SSO identity (issuer=%s) to user %s", issuer, user["id"])
    oidc_commands.link_identity(
        conn,
        actor_id=user["id"],
        issuer=issuer,
        subject=subject,
        user_id=user["id"],
    )
    return user
