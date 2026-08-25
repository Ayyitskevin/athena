"""The ATHENA_ANONYMOUS_READS=0 contract, pinned before the live flip.

The operator ruling (2026-08-24) closes anonymous reads on the tailnet on the
strength of one promise: monitoring keeps working. These tests are that
promise's regression net, written from live measurement rather than the
earlier report (which claimed the flip closes /readyz and did not reproduce —
/readyz is registered outside the browser gate, so the anonymous-reads
middleware structurally cannot touch it).

Each boundary here is one the flip must NOT break:
- /healthz and /readyz keep answering — else the flip silently kills the
  uptime check and the operator learns about it from the pager.
- browser reads actually close (the flip must do its one job),
- the API stays token-gated exactly as before,
- login stays reachable (a closed instance you cannot sign in to is a brick),
- first-user bootstrap on an empty database stays possible (the flag must
  never lock the operator out of their own box).
"""

from fastapi.testclient import TestClient

from athena import config
from athena.main import create_app


def _closed_app(tmp_path, monkeypatch):
    """An app with anonymous reads closed at the gate the middleware reads.

    The env variable is read once at config import; the middleware consults
    ``config.ANONYMOUS_READS`` per request, so patching the attribute
    exercises the exact production code path without a subprocess."""
    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    return create_app(tmp_path / "closed.db")


def test_monitoring_survives_the_flip(tmp_path, monkeypatch):
    app = _closed_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        # /readyz is not in _ANONYMOUS_ALWAYS_ALLOWED and must not need to be:
        # it is registered with no browser dependency. If a refactor ever moves
        # it behind the browser gate, this is the test that says so BEFORE the
        # flip kills the uptime check in production.
        assert c.get("/readyz").status_code == 200


def test_the_flip_actually_closes_browser_reads(tmp_path, monkeypatch):
    app = _closed_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        for path in ("/", "/aegis/issues/new", "/mentor", "/find"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code in (302, 303, 401, 403), (path, r.status_code)
        # Control: the same instance with reads open serves the board page.
        monkeypatch.setattr(config, "ANONYMOUS_READS", True)
        assert c.get("/").status_code == 200


def test_api_reads_stay_token_gated(tmp_path, monkeypatch):
    app = _closed_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        assert c.get("/issues").status_code == 401
        assert c.get("/desk").status_code == 401


def test_login_and_bootstrap_stay_reachable(tmp_path, monkeypatch):
    app = _closed_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        assert c.get("/login").status_code == 200
        # First user on an empty database: the one-time bootstrap path must
        # work with reads closed, or the flag locks the operator out.
        r = c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        assert r.status_code in (200, 201), r.text
