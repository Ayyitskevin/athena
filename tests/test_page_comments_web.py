"""The page-comments web thread (/mentor/pages/{id} discussion).

A thin client over the page-comment data layer: reading a page's comments is open,
posting/editing/deleting is the session user's call (author-only for edit/delete).
These pin the rendered thread, the web post, and the author-ownership gate.
"""

from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _login(client, email="a@e.com", name="A", password="pw", role=None):
    payload = {"email": email, "name": name, "password": password}
    if role:
        payload["role"] = role
    client.post("/users", json=payload, headers=H1)
    r = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _space(client):
    return client.post(
        "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
    ).json()["id"]


def _page(client, sid):
    return client.post(
        f"/spaces/{sid}/pages", json={"title": "Doc"}, headers=H1
    ).json()["id"]


def test_page_renders_thread_and_web_post(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        _login(client)  # first user → admin
        pid = _page(client, _space(client))

        page = client.get(f"/mentor/pages/{pid}")
        assert (
            page.status_code == 200
            and "Comments" in page.text
            and "Add a comment" in page.text
        )

        posted = client.post(
            f"/mentor/pages/{pid}/comments",
            data={"body": "first post"},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert "first post" in client.get(f"/mentor/pages/{pid}").text


def test_blank_comment_rejected(tmp_path):
    with TestClient(create_app(tmp_path / "blank.db")) as client:
        _login(client)
        pid = _page(client, _space(client))
        assert (
            client.post(
                f"/mentor/pages/{pid}/comments",
                data={"body": "  "},
                follow_redirects=False,
            ).status_code
            == 400
        )


def test_delete_is_author_only(tmp_path):
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        _login(client)  # user 1 (admin), the author
        pid = _page(client, _space(client))
        client.post(
            f"/mentor/pages/{pid}/comments",
            data={"body": "mine"},
            follow_redirects=False,
        )
        cid = client.get(f"/pages/{pid}/comments").json()[0]["id"]

        # A second user (separate session) cannot delete it.
        _login(client, "b@e.com", "B", "pw", role="member")
        denied = client.post(
            f"/mentor/pages/{pid}/comments/{cid}/delete", follow_redirects=False
        )
        assert denied.status_code == 403
        assert client.get(f"/pages/{pid}/comments") != []

        # Back to the author — delete succeeds and the thread empties.
        _login(client, "a@e.com", "A", "pw")
        ok = client.post(
            f"/mentor/pages/{pid}/comments/{cid}/delete", follow_redirects=False
        )
        assert ok.status_code == 303
        assert client.get(f"/pages/{pid}/comments").json() == []


def test_logged_out_reader_sees_signin_prompt(tmp_path):
    with TestClient(create_app(tmp_path / "anon.db")) as client:
        _login(client)
        pid = _page(client, _space(client))
        client.post(
            f"/mentor/pages/{pid}/comments",
            data={"body": "hello"},
            follow_redirects=False,
        )

        anon = TestClient(client.app)
        page = anon.get(f"/mentor/pages/{pid}")
        # Open read shows the comment, but the form is replaced by a sign-in prompt.
        assert "hello" in page.text
        assert (
            "Add a comment" not in page.text and "Sign in</a> to comment" in page.text
        )
