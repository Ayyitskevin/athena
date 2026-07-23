"""Password hashing for browser login.

Stdlib pbkdf2-hmac-sha256 with a per-password random salt — no third-party
dependency. A stored hash looks like:

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

The salt and iteration count travel with the hash, so we can verify old hashes
even after raising the cost later. Verification is constant-time to avoid
leaking how much of the hash matched.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
# OWASP's current PBKDF2-HMAC-SHA256 recommendation. Hashes stored at the old
# cost (240k) keep verifying — the iteration count travels with the hash — and
# are transparently re-hashed on the next successful login (see needs_rehash and
# users.verify_credentials).
_ITERATIONS = 600_000
_SALT_BYTES = 16

# Lazily-built hash used to burn a full PBKDF2 verification when there is no
# real hash to check (unknown email, or an API-only account with no password).
# Without it, "no such account" answers in microseconds while "wrong password"
# takes the full derivation — a timing oracle that contradicts the opaque-None
# contract of verify_credentials.
_dummy_hash: str | None = None


def hash_password(raw: str) -> str:
    """Hash a plaintext password into a self-describing storable string."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def needs_rehash(stored: str | None) -> bool:
    """True when a stored hash should be upgraded on the next successful login —
    an older algorithm tag or a lower iteration count than current policy. False
    for missing/malformed values (verification already rejects those)."""
    if not stored:
        return False
    try:
        algo, iters, _salt_hex, _hash_hex = stored.split("$")
        return algo != _ALGO or int(iters) < _ITERATIONS
    except ValueError:
        return False


def dummy_verify(raw: str) -> None:
    """Burn the same PBKDF2 cost as a real (failed) verification, discarding the
    result. Called on login paths that have no stored hash so their timing is
    indistinguishable from a wrong password."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_hex(16))
    verify_password(raw, _dummy_hash)


def verify_password(raw: str, stored: str | None) -> bool:
    """True iff `raw` produced `stored`. False for any malformed or missing hash
    (e.g. a user with no password set) — never raises on bad input."""
    if not stored:
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", raw.encode(), bytes.fromhex(salt_hex), int(iters)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)
