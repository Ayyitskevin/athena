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
_ITERATIONS = 240_000
_SALT_BYTES = 16


def hash_password(raw: str) -> str:
    """Hash a plaintext password into a self-describing storable string."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


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
