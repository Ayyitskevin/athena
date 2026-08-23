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


def _count_sweeps(limiter: rate_limits.FixedWindowRateLimiter) -> list[float]:
    """Record each real sweep, returning the list that accumulates them."""
    sweeps: list[float] = []
    real_prune = limiter._prune

    def counting_prune(now: float) -> None:
        sweeps.append(now)
        real_prune(now)

    limiter._prune = counting_prune  # type: ignore[method-assign]
    return sweeps


def test_prune_is_amortized_not_per_call():
    # WHY: the sweep is O(keys). Running it on every check() made per-request cost
    # scale with the number of distinct callers seen — worst precisely under the
    # flood the limiter exists to absorb, so the throttle was quietly an
    # amplifier. This pins that a burst inside one window sweeps ONCE, not once
    # per call. Without the throttle this test sees 500 sweeps.
    now = {"t": 1000.0}
    limiter = rate_limits.TokenRateLimiter(10_000, clock=lambda: now["t"])
    sweeps = _count_sweeps(limiter)

    for key in range(500):
        limiter.check(key)

    assert len(sweeps) == 1, f"expected one sweep inside a window, got {len(sweeps)}"

    # A new window earns exactly one more sweep, not one per call.
    now["t"] += rate_limits.WINDOW_SECONDS + 1
    for key in range(500):
        limiter.check(key)
    assert len(sweeps) == 2, f"expected one sweep per window, got {len(sweeps)}"


def test_prune_size_escape_hatch_fires_inside_a_window():
    # WHY: throttling to one sweep per window is only safe if a pathological burst
    # of distinct keys cannot sit unswept until the interval elapses. The size
    # escape hatch is what keeps "amortized" from becoming "unbounded"; if someone
    # removes it, this fails.
    now = {"t": 1000.0}
    limiter = rate_limits.TokenRateLimiter(10_000, clock=lambda: now["t"])
    sweeps = _count_sweeps(limiter)

    # Stay inside one window, but push past the escape threshold.
    for key in range(rate_limits._PRUNE_SIZE_ESCAPE + 5):
        limiter.check(key)

    assert len(sweeps) > 1, "size escape hatch never fired inside the window"


def test_bounded_growth_survives_the_throttle():
    # WHY: the whole point of pruning is that the map cannot accumulate a
    # permanent entry per caller ever seen. The throttle delays a sweep; it must
    # not cancel one. Abandoned keys still have to disappear.
    now = {"t": 1000.0}
    limiter = rate_limits.TokenRateLimiter(5, clock=lambda: now["t"])
    for key in range(50):
        limiter.check(key)
    assert len(limiter._windows) == 50

    # Well past the retention horizon, one more call sweeps every abandoned key.
    now["t"] += rate_limits.WINDOW_SECONDS * 3
    limiter.check("survivor")
    assert len(limiter._windows) == 1
    assert "survivor" in limiter._windows


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
