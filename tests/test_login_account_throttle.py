"""Credential stuffing is bounded per ACCOUNT, not just per IP — without a new oracle.

The per-IP throttle bounds one peer. It does not bound one account, and credential
stuffing is distributed by construction: a thousand hosts guessing ten passwords each
at one address stays under every per-IP ceiling while making ten thousand attempts on
that account. The per-account throttle closes that axis.

The delicate part is that it must not undo the login route's existing discipline. That
route works hard to be existence-opaque: `users.verify_credentials` burns a dummy PBKDF2
for an unknown email so both rejections cost the same, and the failed-login audit event
is written on a response background task so the write (which happens only for a real
account) is off the client-measured path. A throttle keyed by USER ID would have thrown
that away — a real address would be throttled and an unknown one would not, and the
difference is observable. So it is keyed by the submitted email, and these tests pin
that an unknown address is throttled exactly like a real one.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.core import db, security_events
from athena.main import create_app


def _seed(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def _app(tmp_path, name, **kwargs):
    # The per-IP limiter is off in these tests: it would refuse first and mask whether
    # the per-account limiter did anything.
    return create_app(
        tmp_path / name,
        login_rate_limit_per_minute=0,
        **kwargs,
    )


def _attempt(client, email="a@e.com", password="wrong"):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def test_attempts_at_one_account_are_bounded_even_from_many_peers(tmp_path):
    """The whole point: the per-IP limiter is disabled here, so if the account survived
    unbounded guessing this test would pass forever."""
    app = _app(tmp_path, "acct.db", login_account_rate_limit_per_minute=3)
    with TestClient(app) as client:
        _seed(client)
        for _ in range(3):
            assert _attempt(client).status_code == 401
        blocked = _attempt(client)
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1


def test_the_throttle_refuses_the_correct_password_too(tmp_path):
    """It runs BEFORE credential verification, which is what makes it a bound on the
    attempt rate rather than on the failure rate — and what protects PBKDF2's CPU."""
    app = _app(tmp_path, "before.db", login_account_rate_limit_per_minute=2)
    with TestClient(app) as client:
        _seed(client)
        for _ in range(2):
            assert _attempt(client).status_code == 401
        assert _attempt(client, password="pw").status_code == 429


def test_an_unknown_address_is_throttled_exactly_like_a_real_one(tmp_path):
    """The existence oracle this design exists to avoid. If the throttle keyed off the
    resolved account, hammering an unknown address would stay 401 forever while a real
    one turned 429 — handing an attacker a membership test that costs nothing."""
    app = _app(tmp_path, "oracle.db", login_account_rate_limit_per_minute=2)
    with TestClient(app) as client:
        _seed(client)
        real = [_attempt(client, email="a@e.com").status_code for _ in range(3)]
        unknown = [_attempt(client, email="nobody@e.com").status_code for _ in range(3)]
        assert real == unknown == [401, 401, 429]


def test_each_address_gets_its_own_budget(tmp_path):
    """One hammered address must not lock out everybody else — that would turn the
    defense into the outage."""
    app = _app(tmp_path, "perkey.db", login_account_rate_limit_per_minute=2)
    with TestClient(app) as client:
        _seed(client)
        # Only the first user bootstraps without a credential; the second is created
        # by the admin.
        created = client.post(
            "/users",
            json={"email": "b@e.com", "name": "B", "password": "pw2"},
            headers={"X-Athena-Actor": "1"},
        )
        assert created.status_code == 201, created.text
        for _ in range(2):
            assert _attempt(client, email="a@e.com").status_code == 401
        assert _attempt(client, email="a@e.com").status_code == 429
        # b@e.com is untouched, and can still log in with the right password.
        signin = _attempt(client, email="b@e.com", password="pw2")
        assert signin.status_code == 303


def test_case_variants_do_not_reset_the_counter_for_one_account(tmp_path):
    """users.email is case-SENSITIVE (no COLLATE NOCASE), so a differently-cased address
    is a different account that the target's password cannot open. Varying the case
    therefore buys an attacker a fresh budget against a DIFFERENT identity, never more
    guesses at this one — which is the property that matters."""
    app = _app(tmp_path, "case.db", login_account_rate_limit_per_minute=2)
    with TestClient(app) as client:
        _seed(client)
        for _ in range(2):
            assert _attempt(client, email="a@e.com").status_code == 401
        assert _attempt(client, email="a@e.com").status_code == 429
        # The variant is a separate key AND a separate (nonexistent) account: it gets
        # its own budget, but the right password for a@e.com does not work on it.
        assert _attempt(client, email="A@e.com", password="pw").status_code == 401


def test_zero_disables_the_per_account_throttle(tmp_path):
    app = _app(tmp_path, "off.db", login_account_rate_limit_per_minute=0)
    with TestClient(app) as client:
        _seed(client)
        for _ in range(6):
            assert _attempt(client).status_code == 401


def test_a_throttled_attempt_on_a_real_account_lands_on_the_trail(tmp_path):
    """A run of guessing at one address is the signal an operator wants before a
    compromise, so it is a security event with its OWN verb — a throttled attempt is a
    different fact from a wrong password and must not inflate that count."""
    path = tmp_path / "audit.db"
    app = _app(tmp_path, "audit.db", login_account_rate_limit_per_minute=2)
    with TestClient(app) as client:
        _seed(client)
        for _ in range(3):
            _attempt(client)

    conn = db.connect(path)
    try:
        counts = security_events.failure_counts(conn)
        assert counts[security_events.VERB_LOGIN_THROTTLED] >= 1
        assert (
            counts[security_events.VERB_LOGIN_FAILED] == 2
        )  # the two that got through
        rows = security_events.list_failures(
            conn, verb=security_events.VERB_LOGIN_THROTTLED
        )
        assert rows and rows[0]["actor_email"] == "a@e.com"
    finally:
        conn.close()


def test_a_throttled_unknown_address_writes_nothing(tmp_path):
    """The audit write happens only when the address names a real account — so it is
    done on a background task, off the measured path. Recording an event for an unknown
    address would also be recording an event attributed to nobody."""
    path = tmp_path / "unknown.db"
    app = _app(tmp_path, "unknown.db", login_account_rate_limit_per_minute=2)
    with TestClient(app) as client:
        _seed(client)
        for _ in range(3):
            _attempt(client, email="nobody@e.com")

    conn = db.connect(path)
    try:
        assert (
            security_events.failure_counts(conn)[security_events.VERB_LOGIN_THROTTLED]
            == 0
        )
    finally:
        conn.close()


def test_the_throttle_verb_is_in_the_closed_security_set(tmp_path):
    """Being in SECURITY_VERBS is what puts it on /admin/security, in the zero-filled
    counts, and in the fleet-attention refusal signal — without any surface having to
    learn a new name."""
    assert security_events.VERB_LOGIN_THROTTLED in security_events.SECURITY_VERBS
    path = tmp_path / "zero.db"
    conn = db.connect(path)
    db.migrate(conn)
    try:
        # Zero-filled on a quiet instance rather than a missing key.
        assert (
            security_events.failure_counts(conn)[security_events.VERB_LOGIN_THROTTLED]
            == 0
        )
    finally:
        conn.close()
