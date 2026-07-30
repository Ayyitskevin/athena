"""CSRF protection for the browser write path — the synchronizer-token contract.

These encode WHY the guard earns its place and exactly where it does (and does
NOT) apply:

  * every state-changing request made WITH a session cookie must echo the
    session's unguessable token back, or it is refused (403). A cross-site page
    can make the victim's browser SEND the cookie, but same-origin policy stops
    it from READING the token, so it cannot forge a valid submission;
  * the token is accepted two ways — the hidden `csrf_token` form field (plain
    server-rendered forms, no JavaScript needed) OR an `X-CSRF-Token` header
    (HTMX / scripted clients). Both must work;
  * the guard is scoped to COOKIE auth only. An anonymous request carries no
    session to forge, so the dependency is a no-op and the route's own auth
    (401) still applies — it must NOT degrade to a 403;
  * the REST API authenticates agents by bearer token / actor header, never the
    session cookie, so those requests carry no ambient cookie to forge and need
    no token — they must keep working untouched;
  * logout is itself a state-changing POST, so it is protected too.
"""

from fastapi.testclient import TestClient

from athena.main import create_app


def _login(client):
    """Bootstrap user 1, log the browser in, and hand back the CSRF token the
    server set in the readable cookie. Leaves client.headers clean so each test
    controls how (or whether) it presents the token."""
    client.post(
        "/users",
        json={"email": "kevin@example.com", "name": "Kevin", "password": "secret"},
    )
    client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
    return client.cookies.get("athena_csrf")


def _make_issue(client, title="ship it", actor="1") -> int:
    r = client.post("/issues", json={"title": title}, headers={"X-Athena-Actor": actor})
    assert r.status_code == 201
    return r.json()["id"]


def test_protected_post_without_token_is_403(tmp_path):
    # WHY: a logged-in session is exactly the ambient credential CSRF defends. A
    # write that omits the token is precisely the forged-submission shape — refuse.
    app = create_app(tmp_path / "no.db")
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        r = client.post(f"/aegis/issues/{issue_id}/status", data={"status": "done"})
        assert r.status_code == 403


def test_valid_form_field_token_is_accepted(tmp_path):
    # WHY: the no-JavaScript path. A server-rendered form submits the token as a
    # hidden field; that must satisfy the guard.
    app = create_app(tmp_path / "field.db")
    with TestClient(app) as client:
        token = _login(client)
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "done", "csrf_token": token},
            follow_redirects=False,
        )
        assert r.status_code == 303


def test_valid_header_token_is_accepted(tmp_path):
    # WHY: the scripted/HTMX path. The same token in an X-CSRF-Token header must
    # also satisfy the guard, with no form field present.
    app = create_app(tmp_path / "header.db")
    with TestClient(app) as client:
        token = _login(client)
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "done"},
            headers={"X-CSRF-Token": token},
            follow_redirects=False,
        )
        assert r.status_code == 303


def test_wrong_token_is_403(tmp_path):
    # WHY: presence isn't enough — the token must MATCH the session's. A guessed
    # or stale value is rejected by the constant-time compare.
    app = create_app(tmp_path / "wrong.db")
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "done", "csrf_token": "not-the-real-token"},
        )
        assert r.status_code == 403


def test_non_ascii_token_is_403_not_500(tmp_path):
    # WHY: secrets.compare_digest raises TypeError on a non-ASCII str, so a
    # mangled token used to turn the refusal into a 500. A real token is ASCII;
    # anything else is simply wrong, and wrong means 403.
    app = create_app(tmp_path / "nonascii.db")
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        form = client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "done", "csrf_token": "tökén-ÿÿ"},
        )
        assert form.status_code == 403
        header = client.post(
            f"/aegis/issues/{issue_id}/status",
            content=b"status=done",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                b"X-CSRF-Token": "tökén-ÿÿ".encode(),
            },
        )
        assert header.status_code == 403


def test_anonymous_post_is_401_not_403(tmp_path):
    # WHY: scope. With no session there is nothing to forge, so the CSRF guard is
    # a no-op and the route's OWN auth gate must answer — 401, never 403. A 403
    # here would mean the guard is firing on requests it shouldn't.
    app = create_app(tmp_path / "anon.db")
    with TestClient(app) as client:
        r = client.post("/aegis/issues", data={"title": "anon"})
        assert r.status_code == 401


def test_api_token_route_needs_no_csrf(tmp_path):
    # WHY: the REST API authenticates by actor header / bearer token, never the
    # session cookie. Such a request carries no ambient cookie to forge, so it
    # needs no CSRF token — the write must go through untouched.
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        # seed user 1 so the actor header resolves to a real user
        client.post(
            "/users", json={"email": "a@e.com", "name": "A", "password": "secret"}
        )
        r = client.post(
            "/issues", json={"title": "via api"}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 201


def test_logout_is_csrf_protected(tmp_path):
    # WHY: logout is a state-changing POST (it destroys the session). A cross-site
    # forced-logout is a real annoyance attack, so the same token is required —
    # without it the session must survive; with it, logout proceeds.
    app = create_app(tmp_path / "logout.db")
    with TestClient(app) as client:
        token = _login(client)
        # No token → refused, session intact.
        assert client.post("/logout").status_code == 403
        assert client.get("/").text.count("Sign out") == 1  # still logged in
        # With the token → logout proceeds.
        r = client.post(
            "/logout", headers={"X-CSRF-Token": token}, follow_redirects=False
        )
        assert r.status_code == 303


def test_rendered_form_embeds_the_token(tmp_path):
    # WHY: the whole no-JavaScript story rests on the server stamping the token
    # into the form it renders. The detail page's hidden input must carry the
    # session's token verbatim, so a plain submit already satisfies the guard.
    app = create_app(tmp_path / "render.db")
    with TestClient(app) as client:
        token = _login(client)
        issue_id = _make_issue(client)
        page = client.get(f"/aegis/issues/{issue_id}").text
        assert f'name="csrf_token" value="{token}"' in page
