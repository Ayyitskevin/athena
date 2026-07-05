"""Per-IP rate limit on anonymous reads.

The per-token limiter only guards authenticated (bearer) traffic, so a
credential-free caller could otherwise hammer the public `optional_actor` reads
unbounded — a DoS vector. These pin the anonymous throttle: an anonymous caller
is bounded by client IP, an authenticated caller bypasses it entirely, the limit
is per-IP (one noisy IP doesn't throttle another), and it stays OFF by default so
normal anonymous browsing is untouched.
"""

from fastapi.testclient import TestClient

from athena.core import db, rate_limits
from athena.main import create_app

# An anonymous, optional_actor read endpoint (no credential required).
_ANON_READ = "/issues/search?q=x"


def _seed_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    conn.close()


def test_anonymous_reads_are_ip_rate_limited(tmp_path):
    # WHY (load-bearing): the credential-free hammering vector must be bounded. With a
    # budget of 2/min, the third anonymous read from the same IP is refused (429) with
    # the standard Retry-After / X-RateLimit headers.
    app = create_app(tmp_path / "anon.db", anon_rate_limit_per_minute=2)
    with TestClient(app) as client:
        assert client.get(_ANON_READ).status_code == 200
        assert client.get(_ANON_READ).status_code == 200

        limited = client.get(_ANON_READ)
        assert limited.status_code == 429
        assert limited.json() == {"detail": "anonymous rate limit exceeded"}
        assert 1 <= int(limited.headers["retry-after"]) <= 60
        assert limited.headers["x-ratelimit-limit"] == "2"
        assert limited.headers["x-ratelimit-remaining"] == "0"


def test_authenticated_reads_bypass_the_anon_limit(tmp_path):
    # WHY: the anon limit keys on IP and must NOT touch identified callers — an agent or
    # signed-in user is already accountable (and token-limited). Even with a budget of 1,
    # authenticated reads keep succeeding well past it.
    db_file = tmp_path / "authed.db"
    app = create_app(db_file, anon_rate_limit_per_minute=1)
    with TestClient(app) as client:
        _seed_user(db_file)  # so the actor header resolves to a real user
        auth = {"X-Athena-Actor": "1"}
        for _ in range(4):
            assert client.get(_ANON_READ, headers=auth).status_code == 200


def test_anon_limit_is_off_by_default(tmp_path):
    # WHY: the default must not throttle ordinary anonymous browsing (or the test suite).
    # With no limit configured (config default 0 = off), anonymous reads are unbounded.
    app = create_app(tmp_path / "off.db")  # no anon_rate_limit_per_minute
    with TestClient(app) as client:
        for _ in range(6):
            assert client.get(_ANON_READ).status_code == 200


def test_limiter_is_scoped_per_key():
    # WHY: the throttle is per-IP — one noisy client can't rate-limit everyone. Drive the
    # limiter directly with two keys (an HTTP test can't vary TestClient's client IP) to
    # prove one key spending its budget leaves the other's untouched.
    limiter = rate_limits.FixedWindowRateLimiter(1)
    assert limiter.check("10.0.0.1").allowed is True
    assert limiter.check("10.0.0.1").allowed is False  # first IP is now spent
    assert limiter.check("10.0.0.2").allowed is True  # a different IP is unaffected
