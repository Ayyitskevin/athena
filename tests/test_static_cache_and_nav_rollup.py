"""Static assets are browser-cacheable, and fetching one costs nothing server-side.

Two findings that turned out to be one problem. `_apply_private_cache_policy` marked
every cookie-carrying response `private, no-store` — including `/static/*` — so a
signed-in browser re-fetched the stylesheet, the htmx bundle and the confirm script on
every page load. And the session middleware ran for those fetches too, which for an
ADMIN meant opening SQLite, resolving the session, and building the whole
fleet-attention rollup once per asset. The re-fetch guaranteed it happened again next
time.

So the fix is one skip and one policy: static requests bypass session resolution
entirely and are cached hard, busted by a startup fingerprint in the URL rather than
by revalidation. What the nav rollup costs on a real page render is a separate
question, measured and answered in main.py's comment — deliberately without a cache,
because staleness in an attention badge is a worse trade than half a millisecond.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

import athena.main as main_module
from athena.aegis import fleet_attention
from athena.main import STATIC_CACHE_CONTROL, create_app, static_version

BROWSER = {"Accept": "text/html"}


def _signed_in(tmp_path, name="static.db"):
    app = create_app(tmp_path / name)
    client = TestClient(app)
    client.__enter__()
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    client.post(
        "/login",
        data={"email": "a@e.com", "password": "pw"},
        follow_redirects=False,
    )
    return app, client


def test_static_assets_are_publicly_cacheable_even_with_a_session_cookie(tmp_path):
    app, client = _signed_in(tmp_path)
    try:
        response = client.get("/static/styles.css")
        assert response.status_code == 200
        assert response.headers["cache-control"] == STATIC_CACHE_CONTROL
        # A public cache entry must never carry a session.
        assert "set-cookie" not in response.headers
    finally:
        client.__exit__(None, None, None)


def test_authenticated_pages_are_still_never_cached(tmp_path):
    """The exemption is for packaged assets only. A page carries the operator's own
    work and must keep its private, no-store policy."""
    app, client = _signed_in(tmp_path, "pages.db")
    try:
        page = client.get("/aegis", headers=BROWSER)
        assert page.headers["cache-control"] == "private, no-store"
        assert "Cookie" in page.headers.get("vary", "")
    finally:
        client.__exit__(None, None, None)


def test_fetching_a_static_asset_costs_no_session_work(tmp_path):
    """The expensive half. Before the skip, every asset request by an admin opened a
    connection, resolved the session and built the fleet-attention rollup — so one
    page load paid for the rollup once per asset on top of the page's own."""
    app, client = _signed_in(tmp_path, "rollup.db")
    try:
        calls: list[int] = []
        real = fleet_attention.build_attention

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        with patch.object(
            main_module.aegis_fleet_attention, "build_attention", counting
        ):
            client.get("/static/styles.css")
            client.get("/static/htmx.min.js")
            assert calls == [], "a static fetch still builds the nav rollup"

            # ...and a real page still builds it exactly once, so the badge is live.
            client.get("/aegis", headers=BROWSER)
            assert len(calls) == 1
    finally:
        client.__exit__(None, None, None)


def test_static_urls_carry_the_fingerprint(tmp_path):
    app, client = _signed_in(tmp_path, "bust.db")
    try:
        page = client.get("/aegis", headers=BROWSER).text
        version = app.state.static_version
        assert f"/static/styles.css?v={version}" in page
        assert f"/static/htmx.min.js?v={version}" in page
        # The asset still serves with the query string attached.
        assert client.get(f"/static/styles.css?v={version}").status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_the_fingerprint_changes_when_an_asset_changes(tmp_path):
    """Without this the year-long cache is a trap: a released fix to styles.css would
    never reach a browser holding the old copy."""
    static_dir = tmp_path / "static"
    (static_dir / "sub").mkdir(parents=True)
    (static_dir / "a.css").write_text("body { color: red }")
    (static_dir / "sub" / "b.js").write_text("console.log(1)")

    before = static_version(static_dir)
    assert len(before) == 12
    assert static_version(static_dir) == before  # stable for unchanged content

    (static_dir / "a.css").write_text("body { color: blue }")
    assert static_version(static_dir) != before

    # A rename counts as a change too: the path is part of the fingerprint, so
    # shipping the same bytes under a new name still busts the cache.
    renamed = static_version(static_dir)
    (static_dir / "a.css").rename(static_dir / "c.css")
    assert static_version(static_dir) != renamed


def test_the_nav_rollup_does_not_scale_with_trail_size(tmp_path):
    """What the 0075 index bought, pinned so it cannot silently regress. The rollup's
    activity inputs must seek their index rather than walking the append-only table —
    that is the difference between a cost that grows with the fleet (fine, measured in
    main.py) and one that grows with the log forever (not fine)."""
    from athena.core import db, security_events

    conn = db.connect(tmp_path / "plan.db")
    db.migrate(conn)
    try:
        plan = " ".join(
            row["detail"]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT verb, COUNT(*) AS n FROM activity "
                f"WHERE verb IN ({','.join('?' * len(security_events.SECURITY_VERBS))}) "
                "AND imported_at IS NULL AND created_at >= ? GROUP BY verb",
                (*security_events.SECURITY_VERBS, "2026-01-01 00:00:00"),
            )
        )
        assert "idx_activity_verb_window" in plan
        assert "SCAN activity" not in plan
    finally:
        conn.close()
