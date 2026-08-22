"""Theme/rail preference cookies, the nav attention badge, category chip tones.

WHY each matters:
- The preference routes write COOKIES, not rows — per-device presentation
  state. The redirect target rides in a form field, so the tests pin that a
  tampered form cannot turn a preference toggle into an open redirect.
- The Intervene badge aggregates admin-scoped reads. The leak test is the
  point: a member's nav must carry no trace of the rollup, not a zeroed
  version of it.
- Status chips color by CATEGORY: a project whose done-state is named
  "shipped" must render exactly like one that calls it "done". Coloring by
  name was the documented limitation this replaces.
"""

import re

from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _login(client, email="a@e.com", password="pw"):
    r = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- /settings/theme and /settings/rail -------------------------------------


def test_theme_cookie_set_cycle_and_clear(tmp_path):
    with TestClient(create_app(tmp_path / "t.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)

        r = client.post(
            "/settings/theme",
            data={"theme": "dark", "next": "/aegis"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/aegis"
        assert client.cookies.get("athena_theme") == "dark"

        r = client.post(
            "/settings/theme",
            data={"theme": "light", "next": "/"},
            follow_redirects=False,
        )
        assert client.cookies.get("athena_theme") == "light"
        # The stamped theme reaches the page.
        assert 'data-theme="light"' in client.get("/aegis").text

        # "system" means NO cookie — deleting beats storing a synonym.
        client.post(
            "/settings/theme",
            data={"theme": "system", "next": "/"},
            follow_redirects=False,
        )
        assert client.cookies.get("athena_theme") is None
        assert "data-theme" not in client.get("/aegis").text.split("<head>")[0]

        assert (
            client.post(
                "/settings/theme",
                data={"theme": "neon", "next": "/"},
                follow_redirects=False,
            ).status_code
            == 400
        )


def test_rail_cookie_toggle(tmp_path):
    with TestClient(create_app(tmp_path / "r.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)

        client.post(
            "/settings/rail",
            data={"state": "collapsed", "next": "/"},
            follow_redirects=False,
        )
        assert client.cookies.get("athena_rail") == "collapsed"
        assert 'data-rail="collapsed"' in client.get("/aegis").text

        client.post(
            "/settings/rail",
            data={"state": "open", "next": "/"},
            follow_redirects=False,
        )
        assert client.cookies.get("athena_rail") is None


def test_preference_next_cannot_leave_the_app(tmp_path):
    # A hidden form field must not become an open redirect: absolute URLs and
    # protocol-relative //host both fall back to the home page.
    with TestClient(create_app(tmp_path / "n.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)
        for evil in (
            "https://evil.example",
            "//evil.example",
            "/\\evil.example",
            "javascript:alert(1)",
        ):
            r = client.post(
                "/settings/theme",
                data={"theme": "dark", "next": evil},
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert r.headers["location"] == "/", evil


def test_preferences_require_a_session(tmp_path):
    with TestClient(create_app(tmp_path / "s.db")) as client:
        assert (
            client.post(
                "/settings/theme", data={"theme": "dark", "next": "/"}
            ).status_code
            == 401
        )
        assert client.cookies.get("athena_theme") is None


# --- The Intervene attention badge ------------------------------------------


def test_attention_badge_reaches_admin_nav(tmp_path):
    with TestClient(create_app(tmp_path / "a.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        # A failed login is a recorded boundary refusal — the cheapest real
        # attention signal, and the one that needs no fixture beyond itself.
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "wrong"},
            follow_redirects=False,
        )
        _login(client)  # first user → admin
        page = client.get("/aegis").text
        assert "Boundary refusals" in page
        assert 'class="nav-count"' in page


def test_attention_never_leaks_to_members(tmp_path):
    with TestClient(create_app(tmp_path / "m.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw"},
            headers=H1,
        )
        # Signals exist (a refusal is on the books)...
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "wrong"},
            follow_redirects=False,
        )
        _login(client, email="m@e.com")
        page = client.get("/aegis").text
        # ...but a member's nav carries no trace of the admin rollup.
        assert "Boundary refusals" not in page
        assert "Claims needing attention" not in page


# --- Category-based chip tones ----------------------------------------------


def test_renamed_done_status_colors_as_done(tmp_path):
    with TestClient(create_app(tmp_path / "c.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid = client.post(
            "/projects", json={"name": "Ship", "key": "SHP"}, headers=H1
        ).json()["id"]
        _login(client)
        # A project whose done-state has a custom NAME...
        r = client.post(
            f"/aegis/projects/{pid}/statuses",
            data={"name": "shipped", "category": "done"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        iid = client.post(
            "/issues",
            json={"title": "cut the release", "project_id": pid},
            headers=H1,
        ).json()["id"]
        client.patch(f"/issues/{iid}", json={"status": "shipped"}, headers=H1)

        # ...colors as done everywhere the row flows: list, detail, board head.
        listing = client.get(f"/aegis/issues?project={pid}").text
        assert 'data-tone="success">shipped</span>' in listing
        detail = client.get(f"/aegis/issues/{iid}").text
        assert 'data-tone="success">shipped</span>' in detail
        board = client.get(f"/aegis/boards?project={pid}").text
        assert re.search(
            r'data-tone="success">\s*<span class="name">Shipped<', board
        ), "the Shipped column head should carry the done-category tone"
