"""Password-hashing policy: cost, transparent upgrades, and timing parity.

Three security properties pinned here:

  * the PRODUCTION iteration policy is at least OWASP's 600k for
    PBKDF2-HMAC-SHA256 (asserted against the source constant, because the test
    suite deliberately runs at a cheap patched cost — see conftest);
  * a hash stored at an older, lower cost keeps verifying and is transparently
    re-hashed to current policy on the next successful login;
  * login rejection burns the same PBKDF2 work whether the email exists or not
    (the dummy-verify path), so "no such account" is not a timing oracle beside
    a docstring promising "never reveal which".
"""

from pathlib import Path
import re

from fastapi.testclient import TestClient

from athena.core import db, deployment, passwords, users
from athena.main import create_app


def _conn(tmp_path, name="pw.db"):
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    return conn


# --- policy ----------------------------------------------------------------


def test_production_iteration_policy_is_at_least_owasp_600k():
    # conftest patches passwords._ITERATIONS down for suite speed, so read the
    # policy from the source of record instead of the (patched) module attribute.
    source = Path(passwords.__file__).read_text()
    match = re.search(r"^_ITERATIONS = ([\d_]+)$", source, re.MULTILINE)
    assert match is not None
    assert int(match.group(1).replace("_", "")) >= 600_000


def test_hash_format_round_trips_and_rejects_garbage():
    stored = passwords.hash_password("s3cret")
    assert stored.startswith("pbkdf2_sha256$")
    assert stored.endswith("$bounded")
    assert passwords.is_bounded_hash(stored)
    assert passwords.verify_password("s3cret", stored)
    assert not passwords.verify_password("wrong", stored)
    assert not passwords.verify_password("s3cret", None)
    assert not passwords.verify_password("s3cret", "not$a$hash")


def test_new_password_rejects_characters_the_browser_input_strips():
    for raw in ("line\nbreak", "line\rbreak", "line\r\nbreak"):
        try:
            passwords.hash_password(raw)
        except ValueError as exc:
            assert "carriage returns or line feeds" in str(exc)
        else:
            raise AssertionError("browser-unreproducible password was accepted")


def test_legacy_hash_is_marked_after_the_next_verified_login():
    legacy = passwords._encode_password("short password", marker=None)
    assert len(legacy.split("$")) == 4
    assert passwords.is_valid_hash(legacy)
    assert not passwords.is_bounded_hash(legacy)
    assert passwords.needs_rehash(legacy)
    assert passwords.verify_password("short password", legacy)

    upgraded = passwords.rehash_verified_legacy_password("short password")
    assert upgraded.endswith("$bounded")
    assert passwords.is_bounded_hash(upgraded)
    assert not passwords.needs_rehash(upgraded)


def test_verified_legacy_password_above_the_new_bound_is_marked_unbounded():
    raw = "x" * (passwords.MAX_PASSWORD_BYTES + 1)
    upgraded = passwords.rehash_verified_legacy_password(raw)
    assert upgraded.endswith("$unbounded")
    assert passwords.is_valid_hash(upgraded)
    assert not passwords.is_bounded_hash(upgraded)
    assert not passwords.needs_rehash(upgraded)
    assert passwords.verify_password(raw, upgraded)


# --- needs_rehash ----------------------------------------------------------


def test_needs_rehash_flags_lower_cost_and_foreign_algo_only(monkeypatch):
    monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)
    current = passwords.hash_password("pw")
    assert not passwords.needs_rehash(current)

    monkeypatch.setattr(passwords, "_ITERATIONS", 1_000)
    old = passwords.hash_password("pw")
    monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)
    assert passwords.needs_rehash(old)

    assert passwords.needs_rehash("scrypt$16384$aa$bb")  # foreign algo tag
    assert not passwords.needs_rehash(None)
    assert not passwords.needs_rehash("")
    assert not passwords.needs_rehash("malformed")
    assert not passwords.needs_rehash("pbkdf2_sha256$not-a-number$aa$bb")


# --- rehash-on-verify ------------------------------------------------------


def test_successful_login_transparently_upgrades_an_old_hash(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    # Store the hash at a LOWER cost than current policy (simulating a pre-upgrade row).
    monkeypatch.setattr(passwords, "_ITERATIONS", 1_000)
    users.create_user(conn, email="a@e.com", name="A", password="pw")
    old_stored = conn.execute(
        "SELECT password_hash FROM users WHERE id = 1"
    ).fetchone()["password_hash"]
    assert "$1000$" in old_stored

    # Raise policy; the next successful login rewrites the hash at the new cost.
    monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)
    user = users.verify_credentials(conn, email="a@e.com", password="pw")
    assert user is not None and user["id"] == 1
    upgraded = conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()[
        "password_hash"
    ]
    assert "$2000$" in upgraded and upgraded != old_stored

    # The upgraded hash still verifies, and no further rewrite happens.
    assert users.verify_credentials(conn, email="a@e.com", password="pw") is not None
    assert (
        conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()[
            "password_hash"
        ]
        == upgraded
    )


def test_failed_login_never_rewrites_the_hash(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(passwords, "_ITERATIONS", 1_000)
    users.create_user(conn, email="a@e.com", name="A", password="pw")
    stored = conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()[
        "password_hash"
    ]
    monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)
    assert users.verify_credentials(conn, email="a@e.com", password="WRONG") is None
    assert (
        conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()[
            "password_hash"
        ]
        == stored
    )


# --- timing parity (dummy verify) ------------------------------------------


def test_unknown_email_and_passwordless_account_burn_a_dummy_verify(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    users.create_user(conn, email="api-only@e.com", name="Bot")  # no password

    burned: list[str] = []
    monkeypatch.setattr(passwords, "dummy_verify", lambda raw: burned.append(raw))

    assert users.verify_credentials(conn, email="ghost@e.com", password="x") is None
    assert users.verify_credentials(conn, email="api-only@e.com", password="x") is None
    assert len(burned) == 2  # both no-hash rejections paid the dummy cost


def test_dummy_verify_builds_once_and_performs_a_real_verification(monkeypatch):
    monkeypatch.setattr(passwords, "_ITERATIONS", 1_000)
    monkeypatch.setattr(passwords, "_dummy_hash", None)
    passwords.dummy_verify("anything")
    first = passwords._dummy_hash
    assert first is not None and first.startswith("pbkdf2_sha256$")
    passwords.dummy_verify("something-else")
    assert passwords._dummy_hash == first  # reused, not rebuilt


# --- end to end ------------------------------------------------------------


def test_web_login_still_works_and_upgrades(tmp_path, monkeypatch):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as c:
        monkeypatch.setattr(passwords, "_ITERATIONS", 1_000)
        c.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)
        r = c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303
    conn = db.connect(tmp_path / "web.db")
    assert (
        "$2000$"
        in conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()[
            "password_hash"
        ]
    )
    conn.close()


def test_legacy_long_password_can_login_and_rehash_without_a_new_write(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-long.db"
    conn = _conn(tmp_path, "legacy-long.db")
    legacy_password = "x" * (passwords.MAX_PASSWORD_BYTES + 1)
    monkeypatch.setattr(passwords, "_ITERATIONS", 1_000)
    legacy = users.create_user(
        conn,
        email="legacy@example.com",
        name="Legacy",
        role="admin",
    )
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (passwords._encode_password(legacy_password, marker=None), legacy["id"]),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(passwords, "_ITERATIONS", 2_000)
    app = create_app(
        db_path,
        max_request_body_bytes=deployment.MIN_SUPPORTED_REQUEST_BODY_BYTES,
    )
    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"email": "legacy@example.com", "password": legacy_password},
            follow_redirects=False,
        )

    assert response.status_code == 303, response.text
    conn = db.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT password_hash FROM users WHERE id = 1"
        ).fetchone()["password_hash"]
        assert "$2000$" in stored
        assert stored.endswith("$unbounded")
        assert not passwords.needs_rehash(stored)
    finally:
        conn.close()
