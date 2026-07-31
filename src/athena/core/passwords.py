"""Password hashing for browser login.

Stdlib pbkdf2-hmac-sha256 with a per-password random salt — no third-party
dependency. A stored hash looks like:

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>$bounded

The salt and iteration count travel with the hash, so we can verify old hashes
even after raising the cost later. The optional final marker distinguishes new,
bounded credentials from legacy hashes whose original plaintext length is
unknowable. Verification is constant-time to avoid leaking how much matched.
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
MAX_PASSWORD_BYTES = 1024
_BOUNDED_MARKER = "bounded"
_UNBOUNDED_MARKER = "unbounded"

# Lazily-built hash used to burn a full PBKDF2 verification when there is no
# real hash to check (unknown email, or an API-only account with no password).
# Without it, "no such account" answers in microseconds while "wrong password"
# takes the full derivation — a timing oracle that contradicts the opaque-None
# contract of verify_credentials.
_dummy_hash: str | None = None


def validate_new_password(raw: str) -> None:
    """Reject credentials outside the supported hashing/login envelope."""
    if "\r" in raw or "\n" in raw:
        raise ValueError("password must not contain carriage returns or line feeds")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("password must be valid UTF-8") from exc
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} UTF-8 bytes")


def _encode_password(raw: str, *, marker: str | None = _BOUNDED_MARKER) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt, _ITERATIONS)
    encoded = f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"
    return encoded if marker is None else f"{encoded}${marker}"


def _parse_hash(stored: object) -> tuple[int, bytes, bytes, str | None] | None:
    if not isinstance(stored, str):
        return None
    parts = stored.split("$")
    if len(parts) == 4:
        algo, raw_iterations, salt_hex, hash_hex = parts
        marker = None
    elif len(parts) == 5:
        algo, raw_iterations, salt_hex, hash_hex, marker = parts
        if marker not in {_BOUNDED_MARKER, _UNBOUNDED_MARKER}:
            return None
    else:
        return None
    try:
        if (
            algo != _ALGO
            or not raw_iterations.isascii()
            or not raw_iterations.isdecimal()
        ):
            return None
        iterations = int(raw_iterations)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(hash_hex)
    except ValueError:
        return None
    if not (
        1 <= iterations <= _ITERATIONS
        and len(salt) == _SALT_BYTES
        and len(digest) == hashlib.sha256().digest_size
        and salt_hex == salt.hex()
        and hash_hex == digest.hex()
    ):
        return None
    return iterations, salt, digest, marker


def is_valid_hash(stored: object) -> bool:
    """Whether a stored value has a verifier shape this release can use."""
    return _parse_hash(stored) is not None


def is_bounded_hash(stored: object) -> bool:
    """Whether a valid hash records the supported new-password bound."""
    parsed = _parse_hash(stored)
    return parsed is not None and parsed[3] == _BOUNDED_MARKER


def hash_password(raw: str) -> str:
    """Hash a plaintext password into a self-describing storable string."""
    validate_new_password(raw)
    return _encode_password(raw)


def rehash_verified_legacy_password(raw: str) -> str:
    """Re-encode a verified pre-bound credential at the current work factor.

    Callers must first verify ``raw`` against a stored legacy hash. New password
    writes use ``hash_password`` and remain bounded.
    """
    try:
        validate_new_password(raw)
    except ValueError:
        marker = _UNBOUNDED_MARKER
    else:
        marker = _BOUNDED_MARKER
    return _encode_password(raw, marker=marker)


def needs_rehash(stored: str | None) -> bool:
    """True when a stored hash should be upgraded on the next successful login —
    an older algorithm tag or a lower iteration count than current policy. False
    for missing/malformed values (verification already rejects those)."""
    if not stored:
        return False
    parts = stored.split("$")
    if parts[0] != _ALGO:
        return len(parts) == 4
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    iterations, _salt, _digest, marker = parsed
    return marker is None or iterations < _ITERATIONS


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
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    iterations, salt, expected, _marker = parsed
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            raw.encode(),
            salt,
            iterations,
        )
    except (UnicodeEncodeError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)
