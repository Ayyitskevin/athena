"""ATHENA_ANONYMOUS_READS=0 makes exposure stop being disclosure.

Projects and spaces are public by default, which is right for one operator on
loopback and wrong the moment the box is reachable by anyone else — an accidental
tunnel, a Funnel left on, a port-forward that outlived its reason. Per-container
visibility does not help there: the containers really are public, and a caller with
no credential really is allowed to read them.

This flag is the fail-closed answer: with it off, a read requires an authenticated
actor regardless of any container's visibility. The tests below hold it to the whole
surface rather than to a list of routes, because a list of routes is exactly what a
future endpoint forgets to join. Both doors are covered — the REST one
(`identity.optional_actor`) and the browser-router session dependency. Sign-in,
first-user bootstrap, and row-free operational metadata stay open. A switch that
locks the operator out of the login page or an empty database is not a security
control; it is an outage.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from athena import config, main as athena_main
from athena.aegis import projects
from athena.core import activity, db, identity, token_commands, tokens, users
from athena.mentor import spaces
from athena.main import create_app

BOOTSTRAP = {"X-Athena-Bootstrap-Token": "test-bootstrap-token-0000000000000001"}
# Browser headers make response intent explicit in behavioral assertions; route metadata,
# never Accept guessing, owns the authentication transport.
BROWSER = {"Accept": "text/html"}


@pytest.fixture
def closed(monkeypatch):
    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    # The actor header is a signed-in identity in tests; these tests are about the
    # caller who presents nothing at all.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)


def _seeded_app(tmp_path, name="closed.db", **kwargs):
    app = create_app(tmp_path / name, **kwargs)
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "pw"},
            headers=BOOTSTRAP,
        )
    return app


def _events(db_path, verb):
    conn = db.connect(db_path)
    try:
        return activity.list_activity(conn, verb=verb, limit=50)
    finally:
        conn.close()


def _last_used_at(db_path, token_id):
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT last_used_at FROM api_tokens WHERE id = ?", (token_id,)
        ).fetchone()
        return row["last_used_at"]
    finally:
        conn.close()


def _activity_count(db_path):
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) AS count FROM activity").fetchone()[
            "count"
        ]
    finally:
        conn.close()


def test_only_first_user_bootstrap_uses_ungated_optional_identity(tmp_path, closed):
    """First-user creation is the sole intentionally ungated identity adapter."""
    app = _seeded_app(tmp_path)
    ungated = []
    for route in app.routes:
        inner = (
            route.original_router.routes
            if type(route).__name__ == "_IncludedRouter"
            else [route]
        )
        for entry in inner:
            for dependant in getattr(
                getattr(entry, "dependant", None), "dependencies", []
            ):
                call = getattr(dependant, "call", None)
                if call is identity.bootstrap_optional_actor:
                    # The one deliberate exemption, asserted by name below.
                    ungated.append((entry.path, call.__name__))
    # Exactly one route may use the ungated resolver: creating the first user.
    assert ungated == [("/users", "bootstrap_optional_actor")], ungated


def test_every_route_has_an_explicit_auth_or_non_actor_boundary(tmp_path, closed):
    """A new route cannot silently omit its credential authority."""
    app = _seeded_app(tmp_path, "route_inventory.db")
    intentional_non_actor_boundaries = {
        (frozenset({"GET", "HEAD"}), "/openapi.json"),
        (frozenset({"GET"}), "/healthz"),
        (frozenset({"GET"}), "/readyz"),
        (frozenset({"GET"}), "/version"),
        (frozenset({"POST"}), "/users"),
        (frozenset({"POST"}), "/callbacks/icarus"),
        (frozenset({"POST"}), "/forge/{source_name}"),
    }
    direct_identity_adapters = {
        ("athena.aegis.fleet_metrics_api", "_fleet_metrics_actor"),
        ("athena.aegis.fleet_work_api", "_active_work_admin"),
    }

    def dependency_calls(dependant):
        calls = set()
        for dependency in getattr(dependant, "dependencies", []):
            call = getattr(dependency, "call", None)
            if call is not None:
                calls.add(call)
            calls.update(dependency_calls(dependency))
        return calls

    ungated = []
    for route in app.routes:
        included = type(route).__name__ == "_IncludedRouter"
        entries = route.original_router.routes if included else [route]
        included_calls = (
            {dependency.dependency for dependency in route.include_context.dependencies}
            if included
            else set()
        )
        for entry in entries:
            methods = getattr(entry, "methods", set()) or set()
            if not methods:
                continue
            endpoint = getattr(entry, "endpoint", None)
            module = getattr(endpoint, "__module__", "")
            calls = included_calls | dependency_calls(getattr(entry, "dependant", None))
            call_ids = {(call.__module__, call.__name__) for call in calls}
            protected = (
                athena_main._require_browser_session in calls
                or any(
                    call in {identity.current_actor, identity.optional_actor}
                    for call in calls
                )
                or bool(call_ids & direct_identity_adapters)
            )
            boundary = (frozenset(methods), entry.path)
            if boundary not in intentional_non_actor_boundaries and not protected:
                ungated.append((entry.path, module, sorted(call_ids)))

    assert ungated == []


def test_browser_session_gate_is_attached_only_to_browser_routes(tmp_path, closed):
    """Route metadata, not Accept-header guesses, owns the transport split."""
    app = _seeded_app(tmp_path, "route_gate.db")
    ungated_browser = []
    gated_non_browser = []
    for route in app.routes:
        if type(route).__name__ == "_IncludedRouter":
            entries = route.original_router.routes
            calls = {
                dependency.dependency
                for dependency in route.include_context.dependencies
            }
        else:
            entries = [route]
            calls = {
                getattr(dependency, "call", None)
                for dependency in getattr(
                    getattr(route, "dependant", None), "dependencies", []
                )
            }
        gated = athena_main._require_browser_session in calls
        for entry in entries:
            endpoint = getattr(entry, "endpoint", None)
            module = getattr(endpoint, "__module__", "")
            if module.startswith("athena.web") and not gated:
                ungated_browser.append(entry.path)
            if module and not module.startswith("athena.web") and gated:
                gated_non_browser.append((entry.path, module))

    assert ungated_browser == []
    assert sorted(gated_non_browser) == [
        ("/docs", "athena.main"),
        ("/docs/oauth2-redirect", "athena.main"),
        ("/redoc", "athena.main"),
    ]


def test_anonymous_rest_reads_are_refused(tmp_path, closed):
    app = _seeded_app(tmp_path)
    with TestClient(app) as client:
        for path in (
            "/issues",
            "/projects",
            "/spaces",
            "/pages/by-title?title=x",
            "/sprints/1",
            "/labels",
            "/embeds/help",
            "/forge/help",
            "/issues/query/help",
        ):
            response = client.get(path)
            assert response.status_code == 401, (
                f"{path} answered {response.status_code}"
            )


def test_anonymous_feed_and_search_reads_are_refused(tmp_path, closed):
    """Search, the activity feed and the event stream already required an actor —
    this pins that they still do, so the acceptance covers every read surface and not
    just the ones the flag newly closed."""
    app = _seeded_app(tmp_path)
    with TestClient(app) as client:
        for path in ("/search?q=x", "/activity", "/events"):
            assert client.get(path).status_code == 401, path


def test_anonymous_browser_reads_are_sent_to_the_login_page(tmp_path, closed):
    """The browser half. A signed-out visitor gets the sign-in page instead of the
    content, for pages that never consulted optional_actor at all."""
    app = _seeded_app(tmp_path)
    with TestClient(app) as client:
        for path in ("/aegis", "/aegis/issues", "/aegis/boards", "/mentor", "/"):
            response = client.get(path, headers=BROWSER, follow_redirects=False)
            assert response.status_code == 303, (
                f"{path} answered {response.status_code}"
            )
            assert response.headers["location"] == "/login"


def test_the_login_page_and_static_assets_stay_reachable(tmp_path, closed):
    """A closed instance the operator cannot sign in to is an outage, not a control."""
    app = _seeded_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/login", headers=BROWSER).status_code == 200
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/healthz").status_code == 200


def test_framework_html_docs_require_a_browser_session(tmp_path, closed):
    """Interactive HTML is browser transport; schema and probes are metadata."""
    app = _seeded_app(tmp_path, "framework_docs.db")
    bearer_headers = {**BROWSER, "Authorization": "Bearer invalid"}
    with TestClient(app) as client:
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            missing = client.get(path, headers=BROWSER, follow_redirects=False)
            bearer = client.get(path, headers=bearer_headers, follow_redirects=False)
            assert (missing.status_code, missing.headers["location"]) == (303, "/login")
            assert (bearer.status_code, bearer.headers["location"]) == (303, "/login")

        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/openapi.json").status_code == 200

        signed_in = client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            assert client.get(path, headers=BROWSER).status_code == 200


def test_an_empty_instance_can_still_be_bootstrapped(tmp_path, closed):
    """The other thing that must stay open. A fresh database has no actor to
    authenticate as, so gating the first user-create would make a closed instance
    impossible to set up."""
    app = create_app(tmp_path / "fresh.db")
    with TestClient(app) as client:
        created = client.post(
            "/users",
            json={"email": "first@e.com", "name": "First", "password": "pw"},
            headers=BOOTSTRAP,
        )
        assert created.status_code == 201, created.text
        # ...and the exemption does not become a general opening: once a user exists,
        # an anonymous create is refused like everything else.
        again = client.post(
            "/users",
            json={"email": "second@e.com", "name": "Second", "password": "pw"},
            headers=BOOTSTRAP,
        )
        assert again.status_code in (401, 403), again.text


def test_a_signed_in_reader_is_unaffected(tmp_path, closed):
    """The flag closes anonymity, not the product."""
    app = _seeded_app(tmp_path, "signed_in.db")
    with TestClient(app) as client:
        signin = client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert signin.status_code == 303
        assert client.get("/aegis/issues", headers=BROWSER).status_code == 200
        assert client.get("/mentor", headers=BROWSER).status_code == 200


def test_a_bearer_token_still_reads_the_api(tmp_path, closed):
    """Closed REST still resolves bearer credentials through its own dependency."""
    app = _seeded_app(tmp_path, "bearer.db")
    # Minted through the command rather than over HTTP: REST authenticates with a
    # token, so bootstrapping the FIRST token over REST is a chicken-and-egg the
    # browser normally resolves. This test pins the REST adapter, not token minting.
    conn = db.connect(tmp_path / "bearer.db")
    try:
        admin = users.get_user_by_email(conn, "a@e.com")
        minted = token_commands.mint_token(
            conn, actor_id=admin["id"], name="reader", scopes=[tokens.READ_SCOPE]
        )
        token = minted["token"]
    finally:
        conn.close()

    with TestClient(app) as fresh:  # no cookie jar: the token is the only credential
        authed = fresh.get("/issues", headers={"Authorization": f"Bearer {token}"})
        assert authed.status_code == 200, authed.text


def test_a_forged_bearer_does_not_open_the_browser_surface(tmp_path, closed):
    """A header shaped like a bearer credential never authenticates browser HTML."""
    app = _seeded_app(tmp_path, "forged.db")
    forged = ("Bearer not-a-real-token", "bearer " + "x" * 64, "BEARER  ")
    with TestClient(app) as client:
        for header in forged:
            for path in ("/aegis/issues", "/aegis/boards", "/aegis/activity.csv"):
                response = client.get(
                    path,
                    headers={**BROWSER, "Authorization": header},
                    follow_redirects=False,
                )
                assert response.status_code == 303, (
                    f"{path} answered {response.status_code} for {header!r}"
                )
                assert response.headers["location"] == "/login"


def test_a_revoked_bearer_does_not_open_the_browser_surface(tmp_path, closed):
    """Browser HTML ignores even a formerly valid bearer without auth side effects."""
    app = _seeded_app(tmp_path, "revoked.db")
    conn = db.connect(tmp_path / "revoked.db")
    try:
        admin = users.get_user_by_email(conn, "a@e.com")
        minted = token_commands.mint_token(
            conn, actor_id=admin["id"], name="doomed", scopes=[tokens.READ_SCOPE]
        )
        tokens.revoke_token(conn, user_id=admin["id"], token_id=minted["id"])
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        response = client.get(
            "/aegis/issues",
            headers={**BROWSER, "Authorization": f"Bearer {minted['token']}"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.status_code
        assert response.headers["location"] == "/login"
    assert _events(tmp_path / "revoked.db", "revoked_token_used") == []


def test_a_live_bearer_does_not_open_the_browser_surface(tmp_path, closed):
    """Browser HTML is session-authenticated when anonymous reads are closed.

    Bearer credentials belong to REST/MCP. Letting one unlock a browser page
    creates a second identity path that never populates ``request.state.user`` and
    therefore cannot preserve the browser pause, visibility, or audit policy.
    """
    db_path = tmp_path / "live_bearer.db"
    app = _seeded_app(tmp_path, db_path.name, token_rate_limit_per_minute=1)
    conn = db.connect(db_path)
    try:
        admin = users.get_user_by_email(conn, "a@e.com")
        minted = token_commands.mint_token(
            conn, actor_id=admin["id"], name="reader", scopes=[tokens.READ_SCOPE]
        )
        token = minted["token"]
    finally:
        conn.close()

    with TestClient(app) as client:
        response = client.get(
            "/aegis/issues",
            headers={**BROWSER, "Authorization": f"Bearer {token}"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.status_code
        assert response.headers["location"] == "/login"
        assert _last_used_at(db_path, minted["id"]) is None

        rest_headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/issues", headers=rest_headers).status_code == 200
        assert client.get("/issues", headers=rest_headers).status_code == 429

    assert _last_used_at(db_path, minted["id"]) is not None


def test_a_paused_bearer_does_not_open_the_browser_surface(tmp_path, closed):
    """Browser HTML ignores bearer state; the paused credential is not consumed."""
    db_path = tmp_path / "paused_browser.db"
    app = _seeded_app(tmp_path, db_path.name)
    conn = db.connect(db_path)
    try:
        admin = users.get_user(conn, 1)
        minted = token_commands.mint_token(
            conn, actor_id=admin["id"], name="paused", scopes=[tokens.READ_SCOPE]
        )
        users.set_paused(conn, user_id=admin["id"], paused=True)
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        response = client.get(
            "/aegis/issues",
            headers={**BROWSER, "Authorization": f"Bearer {minted['token']}"},
            follow_redirects=False,
        )

    assert response.status_code == 303, response.status_code
    assert response.headers["location"] == "/login"
    assert _events(db_path, "paused_account_refused") == []
    assert _last_used_at(db_path, minted["id"]) is None


def test_malformed_stored_scope_does_not_open_the_browser_surface(tmp_path, closed):
    """Browser gating ignores bearer shape and leaves token metadata untouched."""
    db_path = tmp_path / "malformed_browser_scope.db"
    app = _seeded_app(tmp_path, db_path.name)
    conn = db.connect(db_path)
    try:
        admin = users.get_user(conn, 1)
        minted = token_commands.mint_token(
            conn, actor_id=admin["id"], name="broken", scopes=[tokens.READ_SCOPE]
        )
        conn.execute(
            "UPDATE api_tokens SET scopes = ? WHERE id = ?",
            ("not-a-scope", minted["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        response = client.get(
            "/aegis/issues",
            headers={**BROWSER, "Authorization": f"Bearer {minted['token']}"},
            follow_redirects=False,
        )

    assert response.status_code == 303, response.status_code
    assert response.headers["location"] == "/login"
    assert _last_used_at(db_path, minted["id"]) is None


def test_a_forged_actor_header_does_not_open_the_browser_surface(tmp_path, monkeypatch):
    """The same class on the local-trust path. TRUST_ACTOR_HEADER means "believe the
    id this header names", not "believe there is a header" — an id naming nobody is
    not a credential."""
    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
    app = _seeded_app(tmp_path, "forged_actor.db")
    with TestClient(app) as client:
        for claimed in ("999999", "not-an-int", ""):
            response = client.get(
                "/aegis/issues",
                headers={**BROWSER, identity.ACTOR_HEADER: claimed},
                follow_redirects=False,
            )
            assert response.status_code == 303, (
                f"actor {claimed!r} answered {response.status_code}"
            )


def test_a_live_trusted_header_does_not_open_the_browser_surface(tmp_path, monkeypatch):
    """The local-trust REST adapter is not a browser session."""
    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
    db_path = tmp_path / "live_actor_header.db"
    app = _seeded_app(tmp_path, db_path.name)
    with TestClient(app) as client:
        rest = client.get("/users/me", headers={identity.ACTOR_HEADER: "1"})
        assert rest.status_code == 200, rest.text
        activity_count = _activity_count(db_path)
        response = client.get(
            "/aegis/issues",
            headers={**BROWSER, identity.ACTOR_HEADER: "1"},
            follow_redirects=False,
        )

    assert response.status_code == 303, response.status_code
    assert response.headers["location"] == "/login"
    assert _activity_count(db_path) == activity_count


def test_missing_and_non_bearer_rest_credentials_share_the_anon_budget(
    tmp_path, closed
):
    """Missing and malformed authorization are anonymous, once each."""
    app = create_app(tmp_path / "missing_malformed.db", anon_rate_limit_per_minute=3)
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "pw"},
            headers=BOOTSTRAP,
        )
        missing = client.get("/issues")
        malformed = client.get("/issues", headers={"Authorization": "Basic nope"})
        limited = client.get("/issues")

    assert missing.status_code == 401
    assert malformed.status_code == 401
    assert limited.status_code == 429


def test_invalid_rest_bearers_remain_anonymously_rate_limited(tmp_path, closed):
    """Closed reads must not preempt REST identity policy.

    Invalid bearer requests reach the authoritative REST resolver, which charges
    the anonymous limiter before returning the opaque authentication refusal.
    """
    app = create_app(tmp_path / "invalid_bearer.db", anon_rate_limit_per_minute=2)
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "pw"},
            headers=BOOTSTRAP,
        )
        headers = {"Authorization": "Bearer invalid"}
        first = client.get("/issues", headers=headers)
        second = client.get("/issues", headers=headers)

    assert first.status_code == 401, first.text
    assert second.status_code == 429, second.text


def test_malformed_stored_scope_is_rejected_before_token_side_effects(tmp_path, closed):
    """Stored credential corruption is invalid authentication, not a 500.

    Scope validation must happen before the token is stamped as used; otherwise an
    unusable credential both leaks an internal error and mutates audit metadata.
    """
    db_path = tmp_path / "malformed_rest_scope.db"
    app = _seeded_app(tmp_path, db_path.name, anon_rate_limit_per_minute=2)
    conn = db.connect(db_path)
    try:
        admin = users.get_user_by_email(conn, "a@e.com")
        minted = token_commands.mint_token(
            conn, actor_id=admin["id"], name="broken", scopes=[tokens.READ_SCOPE]
        )
        conn.execute(
            "UPDATE api_tokens SET scopes = ? WHERE id = ?",
            ("not-a-scope", minted["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        denied = client.get(
            "/issues",
            headers={"Authorization": f"Bearer {minted['token']}"},
        )
        limited = client.get(
            "/issues",
            headers={"Authorization": f"Bearer {minted['token']}"},
        )

    assert denied.status_code == 401, denied.text
    assert denied.json()["detail"] == "authentication required"
    assert limited.status_code == 429
    assert _last_used_at(db_path, minted["id"]) is None


def test_reads_stay_open_by_default(tmp_path):
    """The default is unchanged: an existing loopback deployment does not silently
    become password-walled by upgrading."""
    assert config.ANONYMOUS_READS is True
    app = _seeded_app(tmp_path, "open.db")
    with TestClient(app) as client:
        assert client.get("/issues").status_code == 200
        assert (
            client.get(
                "/aegis/issues", headers=BROWSER, follow_redirects=False
            ).status_code
            == 200
        )


def test_default_visibility_private_makes_new_containers_born_private(
    tmp_path, monkeypatch
):
    """The ergonomics half. It changes only what NEW containers start as — an
    existing public project stays public, because silently reclassifying data an
    operator already shared would be its own surprise."""
    monkeypatch.setattr(config, "DEFAULT_VISIBILITY", "private")
    conn = db.connect(tmp_path / "born.db")
    db.migrate(conn)
    try:
        owner = users.create_user(conn, email="o@e.com", name="O", role="admin")
        project = projects.create_project(
            conn, name="P", key="P", created_by=owner["id"]
        )
        space = spaces.create_space(conn, key="S", name="S", created_by=owner["id"])
        assert project["visibility"] == "private"
        assert (
            conn.execute(
                "SELECT visibility FROM spaces WHERE id = ?", (space["id"],)
            ).fetchone()["visibility"]
            == "private"
        )
    finally:
        conn.close()


def test_containers_are_born_public_by_default(tmp_path):
    """Unchanged for everyone who has not opted in."""
    assert config.DEFAULT_VISIBILITY == "public"
    conn = db.connect(tmp_path / "public.db")
    db.migrate(conn)
    try:
        owner = users.create_user(conn, email="o@e.com", name="O", role="admin")
        project = projects.create_project(
            conn, name="P", key="P", created_by=owner["id"]
        )
        assert project["visibility"] == "public"
    finally:
        conn.close()
