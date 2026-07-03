"""Per-token API rate limits.

These tests pin the agent-safety contract: a runaway bearer token is bounded
without blocking other tokens or the local bootstrap actor-header path.
"""

from fastapi.testclient import TestClient

from athena.core import rate_limits
from athena.main import create_app


def _bootstrap_admin(client) -> int:
    response = client.post(
        "/users", json={"email": "admin@example.com", "name": "Admin"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _mint(client, *, actor_id: int = 1, name: str = "agent") -> str:
    response = client.post(
        "/tokens",
        json={"name": name, "scopes": ["read"]},
        headers={"X-Athena-Actor": str(actor_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_bearer_token_is_limited_after_configured_requests(tmp_path):
    app = create_app(tmp_path / "limit.db", token_rate_limit_per_minute=2)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        raw = _mint(client)

        assert client.get("/users/me", headers=_auth(raw)).status_code == 200
        assert client.get("/users/me", headers=_auth(raw)).status_code == 200

        limited = client.get("/users/me", headers=_auth(raw))

        assert limited.status_code == 429
        assert limited.json() == {"detail": "token rate limit exceeded"}
        assert 1 <= int(limited.headers["retry-after"]) <= 60
        assert limited.headers["x-ratelimit-limit"] == "2"
        assert limited.headers["x-ratelimit-remaining"] == "0"


def test_rate_limit_is_scoped_to_one_token(tmp_path):
    app = create_app(tmp_path / "per_token.db", token_rate_limit_per_minute=1)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        first = _mint(client, name="first")
        second = _mint(client, name="second")

        assert client.get("/users/me", headers=_auth(first)).status_code == 200
        assert client.get("/users/me", headers=_auth(first)).status_code == 429

        other_token = client.get("/users/me", headers=_auth(second))

        assert other_token.status_code == 200
        assert other_token.json()["email"] == "admin@example.com"


def test_rate_limit_window_resets_and_prunes_stale_state():
    # WHY: the limiter's window rollover and stale-entry eviction are time-driven, but
    # every HTTP-level test above uses the real monotonic clock and so can never advance
    # past a window. Drive it with an injected clock to pin two properties the map's
    # unbounded-growth safety rests on: (a) a token is allowed again once its window rolls
    # over, and (b) an abandoned token's state is pruned while an actively-used token's
    # state survives.
    now = {"t": 1000.0}
    limiter = rate_limits.TokenRateLimiter(2, clock=lambda: now["t"])

    # Token 1 spends its two-per-window budget, then is refused inside the same window.
    assert limiter.check(1).allowed is True
    assert limiter.check(1).allowed is True
    assert limiter.check(1).allowed is False
    # Token 2 registers a window at t=1000 and is then never touched again.
    assert limiter.check(2).allowed is True

    # One full window later, token 1's window has rolled over: it is allowed once more.
    now["t"] += rate_limits.WINDOW_SECONDS + 1
    assert limiter.check(1).allowed is True

    # More than two windows past token 2's last (and only) use, so its entry is now stale.
    # The next call prunes it; the just-used token 1 is kept, so the map can't accumulate
    # a permanent entry per token ever seen.
    now["t"] += rate_limits.WINDOW_SECONDS * 2
    limiter.check(1)
    assert 2 not in limiter._windows
    assert 1 in limiter._windows


def test_actor_header_bootstrap_path_is_not_token_limited(tmp_path):
    app = create_app(tmp_path / "header.db", token_rate_limit_per_minute=1)
    with TestClient(app) as client:
        admin_id = _bootstrap_admin(client)
        raw = _mint(client, actor_id=admin_id)

        assert client.get("/users/me", headers=_auth(raw)).status_code == 200
        assert client.get("/users/me", headers=_auth(raw)).status_code == 429

        header_only = client.get("/users", headers={"X-Athena-Actor": str(admin_id)})

        assert header_only.status_code == 200
        assert [row["email"] for row in header_only.json()] == ["admin@example.com"]
