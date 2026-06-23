"""Tests for editing and deleting comments — the first place Athena enforces
per-row OWNERSHIP: only a comment's author may change or remove it.

These encode the contract, not just HTTP shapes: an author can edit/delete their
own comment, a different authenticated user is forbidden (403) from touching it,
a comment addressed under the wrong issue is a 404, an empty edit is a 422, and
every write needs an actor. The 403 tests are the load-bearing ones — they prove
ownership is actually enforced, not just advertised.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def _seed_two_users(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("ann@e.com", "Ann"))
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("bob@e.com", "Bob"))
    conn.commit()
    conn.close()


def _make_issue(client, actor="1") -> int:
    r = client.post("/issues", json={"title": "t"}, headers={"X-Athena-Actor": actor})
    assert r.status_code == 201
    return r.json()["id"]


def _make_comment(client, issue_id, body="hello", actor="1") -> int:
    r = client.post(
        f"/issues/{issue_id}/comments",
        json={"body": body},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201
    return r.json()["id"]


# --- REST API: edit -------------------------------------------------------


def test_author_can_edit_own_comment(tmp_path):
    # WHY: people fix typos. The author editing their own comment persists the
    # new body, and listing reflects it.
    db_file = tmp_path / "edit.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, body="orig", actor="1")
        r = client.patch(
            f"/issues/{issue_id}/comments/{comment_id}",
            json={"body": "edited"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["body"] == "edited"
        listed = client.get(f"/issues/{issue_id}/comments").json()
        assert listed[0]["body"] == "edited"


def test_non_author_cannot_edit_comment(tmp_path):
    # WHY (load-bearing): ownership must be enforced, not advertised. A different
    # authenticated user editing someone else's comment is a 403, and the
    # original body is untouched.
    db_file = tmp_path / "edit_forbidden.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, body="ann's words", actor="1")
        r = client.patch(
            f"/issues/{issue_id}/comments/{comment_id}",
            json={"body": "bob rewrote this"},
            headers={"X-Athena-Actor": "2"},  # Bob, not the author
        )
        assert r.status_code == 403
        listed = client.get(f"/issues/{issue_id}/comments").json()
        assert listed[0]["body"] == "ann's words"  # unchanged


def test_edit_rejects_empty_body(tmp_path):
    db_file = tmp_path / "edit_empty.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, actor="1")
        r = client.patch(
            f"/issues/{issue_id}/comments/{comment_id}",
            json={"body": "   "},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_edit_wrong_issue_is_404(tmp_path):
    # WHY: a comment id is only meaningful under its own issue. Addressing it
    # under a different issue must 404, not edit across issues.
    db_file = tmp_path / "edit_wrongissue.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_a = _make_issue(client, actor="1")
        issue_b = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_a, actor="1")
        r = client.patch(
            f"/issues/{issue_b}/comments/{comment_id}",
            json={"body": "x"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 404


def test_edit_requires_authentication(tmp_path):
    db_file = tmp_path / "edit_noauth.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, actor="1")
        r = client.patch(
            f"/issues/{issue_id}/comments/{comment_id}", json={"body": "x"}
        )
        assert r.status_code == 401


# --- REST API: delete -----------------------------------------------------


def test_author_can_delete_own_comment(tmp_path):
    db_file = tmp_path / "del.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, actor="1")
        r = client.delete(
            f"/issues/{issue_id}/comments/{comment_id}",
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 204
        assert client.get(f"/issues/{issue_id}/comments").json() == []


def test_non_author_cannot_delete_comment(tmp_path):
    # WHY (load-bearing): the delete path must enforce the same ownership as edit.
    db_file = tmp_path / "del_forbidden.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, actor="1")
        r = client.delete(
            f"/issues/{issue_id}/comments/{comment_id}",
            headers={"X-Athena-Actor": "2"},  # not the author
        )
        assert r.status_code == 403
        assert len(client.get(f"/issues/{issue_id}/comments").json()) == 1


def test_delete_missing_comment_is_404(tmp_path):
    db_file = tmp_path / "del_missing.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        r = client.delete(
            f"/issues/{issue_id}/comments/999", headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 404


def test_delete_requires_authentication(tmp_path):
    db_file = tmp_path / "del_noauth.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, actor="1")
        r = client.delete(f"/issues/{issue_id}/comments/{comment_id}")
        assert r.status_code == 401


# --- Web (browser session) ------------------------------------------------


def _login(client, email, name):
    # User creation is authenticated after the first (bootstrap) user; act as
    # user 1, who always exists once the first _login has run.
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})


def test_web_author_can_delete_then_non_author_cannot(tmp_path):
    # WHY: the web write paths must obey the same ownership rule as the API.
    # Ann's comment: Ann can delete it; Bob (logged in) gets 403.
    db_file = tmp_path / "web_del.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1
        issue_id = _make_issue(client, actor="1")
        c1 = _make_comment(client, issue_id, body="ann1", actor="1")
        c2 = _make_comment(client, issue_id, body="ann2", actor="1")

        # Bob logs in and is forbidden from deleting Ann's comment.
        _login(client, "bob@e.com", "Bob")  # user 2, replaces session cookie
        forbidden = client.post(f"/aegis/issues/{issue_id}/comments/{c1}/delete")
        assert forbidden.status_code == 403
        assert len(client.get(f"/issues/{issue_id}/comments").json()) == 2

        # Ann logs back in and deletes her own.
        _login(client, "ann@e.com", "Ann")
        ok = client.post(
            f"/aegis/issues/{issue_id}/comments/{c2}/delete", follow_redirects=False
        )
        assert ok.status_code == 303
        remaining = client.get(f"/issues/{issue_id}/comments").json()
        assert [r["id"] for r in remaining] == [c1]


def test_web_author_can_edit_own_comment(tmp_path):
    db_file = tmp_path / "web_edit.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1
        issue_id = _make_issue(client, actor="1")
        comment_id = _make_comment(client, issue_id, body="before", actor="1")
        ok = client.post(
            f"/aegis/issues/{issue_id}/comments/{comment_id}/edit",
            data={"body": "after"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert client.get(f"/issues/{issue_id}/comments").json()[0]["body"] == "after"
