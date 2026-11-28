"""Every app in a process serves the same included routers, and its own state.

WHY: FastAPI works out a route's dependency graph and models per included
router object, on first match: 0.74 s for Athena's routes. ``create_app``
included 62 routers into every app, so each of the suite's seven thousand apps
paid it again, and the local product gate took 34:54 (K1.1 of
docs/plans/CONSOLIDATION_SPEC.md). The routers are now included once per
process and every app takes those same objects.

Sharing is only sound if nothing an app owns is shared with them. These tests
hold that: two apps over two databases see only their own rows and their own
limits, and the one FastAPI feature that cannot work across shared routes
(``app.dependency_overrides``) is refused out loud, with a seam that does.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, bootstrap, make_app

from athena import main
from athena.core.deps import get_conn
from athena.main import create_app


def _included(app) -> list:
    return [route for route in app.router.routes if hasattr(route, "original_router")]


def test_two_apps_share_the_included_routers_and_nothing_else(tmp_path):
    first, _ = make_app(tmp_path, "first.db")
    second, _ = make_app(tmp_path, "second.db")
    shared_first, shared_second = _included(first), _included(second)
    assert len(shared_first) >= 60
    assert all(a is b for a, b in zip(shared_first, shared_second, strict=True))
    assert first.state is not second.state
    assert first.router is not second.router
    assert first.user_middleware is not second.user_middleware

    with TestClient(first) as one, TestClient(second) as two:
        bootstrap(one, email="one@e.com", name="One")
        bootstrap(two, email="two@e.com", name="Two")
        made = one.post("/issues", json={"title": "Only in the first"}, headers=ADMIN)
        assert made.status_code == 201, made.text
        assert one.get("/issues/1", headers=ADMIN).json()["title"] == (
            "Only in the first"
        )
        # The same route object answered both, each from its own app's database.
        assert two.get("/issues/1", headers=ADMIN).status_code == 404
        assert one.get("/users/1", headers=ADMIN).json()["email"] == "one@e.com"
        assert two.get("/users/1", headers=ADMIN).json()["email"] == "two@e.com"
        # A browser page renders from the shared templates with its app's rows.
        assert "Only in the first" in one.get("/aegis", headers=ADMIN).text
        assert "Only in the first" not in two.get("/aegis", headers=ADMIN).text


def test_an_apps_own_settings_stay_its_own(tmp_path):
    # A limit passed to create_app is that app's middleware state, not a
    # property of the shared routes: the strict app refuses its third
    # anonymous request while the default app, on the same routes, does not.
    strict = create_app(tmp_path / "strict.db", anon_rate_limit_per_minute=2)
    default = create_app(tmp_path / "default.db")
    with TestClient(strict) as limited, TestClient(default) as open_:
        statuses = [limited.get("/issues").status_code for _ in range(3)]
        assert statuses[-1] == 429, statuses
        assert [open_.get("/issues").status_code for _ in range(3)][-1] != 429


def test_app_dependency_overrides_is_refused_and_the_site_seam_works(
    tmp_path, monkeypatch
):
    app, _ = make_app(tmp_path)

    def never():
        raise AssertionError("the override ran")

    # FastAPI's per-app mapping cannot reach routes other apps share. Setting
    # it would be silently ignored, so it is refused with the seam's name.
    with pytest.raises(TypeError, match="SITE_DEPENDENCY_OVERRIDES"):
        app.dependency_overrides[get_conn] = never
    assert app.dependency_overrides == {}

    with TestClient(app, raise_server_exceptions=False) as client:
        bootstrap(client)
        assert client.get("/issues", headers=ADMIN).status_code == 200
        with monkeypatch.context() as patched:
            patched.setitem(main.SITE_DEPENDENCY_OVERRIDES, get_conn, never)
            assert client.get("/issues", headers=ADMIN).status_code == 500
        # Undone with the patch: nothing is left for the next app or test.
        assert main.SITE_DEPENDENCY_OVERRIDES == {}
        assert client.get("/issues", headers=ADMIN).status_code == 200


def test_the_templates_are_the_process_and_the_fingerprint_is_each_apps(tmp_path):
    first, _ = make_app(tmp_path, "first.db")
    second, _ = make_app(tmp_path, "second.db")
    assert main._site_templates() is main._site_templates()
    assert first.state.static_version == second.state.static_version
    assert (
        first.state.static_version
        == main._site_templates().env.globals["static_version"]
    )
