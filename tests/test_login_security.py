"""Login hardening: per-IP throttle + Secure cookies on HTTPS.

POST /login is credential-free at the door, so neither the token nor the anonymous
limiter guards it. These tests pin (a) a per-IP throttle that returns 429 before the
password hash once the attempt rate is exceeded, and (b) that the session cookie carries
the Secure flag when the request arrives over HTTPS even if ATHENA_COOKIE_SECURE was
never set — so a forgotten flag can't leak the session on a direct-TLS deploy.
"""

from fastapi.testclient import TestClient

from athena.main import create_app


def _seed(client):
    # First user is the admin; the suite fixture supplies the bootstrap grant.
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def test_login_is_throttled_per_ip_before_the_password_hash(tmp_path):
    # limit=3: three attempts are allowed (each a normal 401 on bad creds), the fourth is
    # refused with 429 + Retry-After — the brute-force bound, hit before verify runs.
    app = create_app(tmp_path / "throttle.db", login_rate_limit_per_minute=3)
    with TestClient(app) as client:
        _seed(client)
        for _ in range(3):
            r = client.post(
                "/login",
                data={"email": "a@e.com", "password": "wrong"},
                follow_redirects=False,
            )
            assert r.status_code == 401
        # Over the limit now — even the CORRECT password is refused with 429, proving the
        # throttle runs before credential verification (protects pbkdf2 CPU too).
        blocked = client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1


def test_login_throttle_off_when_limit_is_zero(tmp_path):
    # 0 disables the throttle: many bad attempts stay plain 401s, never 429. BOTH
    # limiters are switched off here — this test is about the per-IP off-switch, and
    # leaving the per-account one at its default would refuse the sixth attempt for a
    # different reason and prove nothing about the switch under test.
    app = create_app(
        tmp_path / "off.db",
        login_rate_limit_per_minute=0,
        login_account_rate_limit_per_minute=0,
    )
    with TestClient(app) as client:
        _seed(client)
        for _ in range(6):
            r = client.post(
                "/login",
                data={"email": "a@e.com", "password": "wrong"},
                follow_redirects=False,
            )
            assert r.status_code == 401


def _session_setcookie(response):
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith("athena_session="):
            return raw
    return None


def test_session_cookie_is_secure_over_https(tmp_path):
    # Same app (ATHENA_COOKIE_SECURE stays off), two schemes: the cookie gets Secure when
    # the request is HTTPS and does not on plain HTTP.
    app = create_app(tmp_path / "cookie.db")
    with TestClient(app, base_url="https://testserver") as client:
        _seed(client)
        r = client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        cookie = _session_setcookie(r)
        assert cookie is not None and "secure" in cookie.lower()

    with TestClient(app, base_url="http://testserver") as client:
        r = client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        cookie = _session_setcookie(r)
        assert cookie is not None and "secure" not in cookie.lower()


def test_hsts_emitted_over_https_without_cookie_secure(tmp_path):
    # HSTS auto-emits on a real HTTPS request even if ATHENA_COOKIE_SECURE is off, and
    # never appears over plain HTTP.
    app = create_app(tmp_path / "hsts.db")
    with TestClient(app, base_url="https://testserver") as client:
        assert "strict-transport-security" in client.get("/healthz").headers

    with TestClient(app, base_url="http://testserver") as client:
        assert "strict-transport-security" not in client.get("/healthz").headers
