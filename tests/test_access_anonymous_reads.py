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
(identity.optional_actor) and the browser one (the session middleware) — plus the two
things that must stay open, since a switch that locks the operator out of the login
page or an empty database is not a security control, it is an outage.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from athena import config
from athena.aegis import projects
from athena.core import db, identity, token_commands, tokens, users
from athena.mentor import spaces
from athena.main import create_app

BOOTSTRAP = {"X-Athena-Bootstrap-Token": "test-bootstrap-token-0000000000000001"}
# A browser navigation says so in Accept; an API client does not. The refusal differs
# by idiom (sign-in page vs 401), so the tests have to ask in the right one.
BROWSER = {"Accept": "text/html"}


@pytest.fixture
def closed(monkeypatch):
    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    # The actor header is a signed-in identity in tests; these tests are about the
    # caller who presents nothing at all.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)


def _seeded_app(tmp_path, name="closed.db"):
    app = create_app(tmp_path / name)
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "pw"},
            headers=BOOTSTRAP,
        )
    return app


def test_every_optional_identity_read_is_closed_by_one_dependency(tmp_path, closed):
    """The enumeration that matters: rather than testing routes one by one, assert
    that every optional-identity REST read resolves its caller through the ONE gated
    dependency. A route that used its own resolution would be invisible to the flag,
    and this is the check that would catch it."""
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


def test_anonymous_rest_reads_are_refused(tmp_path, closed):
    app = _seeded_app(tmp_path)
    with TestClient(app) as client:
        for path in ("/issues", "/projects", "/spaces", "/pages", "/sprints"):
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
    """REST authenticates with a token, not with the browser's cookie — so the
    middleware must not refuse a bearer caller before the route can resolve it.
    Closing anonymity has to leave the agent path working, or the flag breaks every
    agent instead of every stranger."""
    app = _seeded_app(tmp_path, "bearer.db")
    # Minted through the command rather than over HTTP: REST authenticates with a
    # token, so bootstrapping the FIRST token over REST is a chicken-and-egg the
    # browser normally resolves. What this test is about is the middleware, not the
    # mint.
    conn = db.connect(tmp_path / "bearer.db")
    try:
        admin = users.get_user_by_email(conn, "a@e.com")
        token = token_commands.mint_token(
            conn, actor_id=admin["id"], name="reader", scopes=[tokens.READ_SCOPE]
        )["token"]
    finally:
        conn.close()

    with TestClient(app) as fresh:  # no cookie jar: the token is the only credential
        authed = fresh.get("/issues", headers={"Authorization": f"Bearer {token}"})
        assert authed.status_code == 200, authed.text


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
