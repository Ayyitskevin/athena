"""Page-comment lifecycle changes (create, edit, delete) are now audited atomically.

The Mentor twin of test_comment_audit.py. Creating and deleting a page comment recorded
an event but in a SEPARATE commit from the row change (a crash between the two lost the
event); EDITING recorded NOTHING at all, so a page-comment body could be silently
rewritten over the API and the web. These tests pin that create/edit/delete each record
a page_commented / page_comment_edited / page_comment_deleted event in the SAME
transaction as the change, once per real change; that the edit — previously silent — is
now on the record; and that author-ownership / admin-moderation authz is preserved.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.mentor import page_comment_commands
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # bootstrap admin
H2 = {"X-Athena-Actor": "2"}  # a second member


def _app(tmp_path, name="page_comments.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "B", "role": "member"}, headers=H1
    )


def _space(client, key="ENG", name="Eng"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()[
        "id"
    ]


def _page(client, space_id, title="Doc"):
    return client.post(
        f"/spaces/{space_id}/pages", json={"title": title}, headers=H1
    ).json()["id"]


def _comment(client, page_id, body="hello", actor="1") -> int:
    r = client.post(
        f"/pages/{page_id}/comments",
        json={"body": body},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _events(db_file, *verbs):
    conn = db.connect(db_file)
    return [e for e in activity.list_activity(conn, limit=200) if e["verb"] in verbs]


# --- REST ------------------------------------------------------------------


def test_rest_create_is_audited_and_author_watches(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c, _space(c))
        _comment(c, pid, body="first", actor="2")

    ev = _events(db_file, "page_commented")
    assert len(ev) == 1
    assert (
        ev[0]["target_kind"] == "page"
        and ev[0]["target_id"] == pid
        and ev[0]["actor_id"] == 2
    )
    # Commenting is participation: the auto-watch landed in the same transaction.
    conn = db.connect(db_file)
    watched = conn.execute(
        "SELECT 1 FROM watches WHERE user_id = 2 AND target_kind = 'page' AND target_id = ?",
        (pid,),
    ).fetchone()
    conn.close()
    assert watched is not None


def test_rest_edit_is_audited(tmp_path):
    # The headline gap: editing a page comment was a completely silent content rewrite.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c, _space(c))
        cid = _comment(c, pid, body="orig", actor="1")
        r = c.patch(f"/pages/{pid}/comments/{cid}", json={"body": "edited"}, headers=H1)
        assert r.status_code == 200 and r.json()["body"] == "edited"

    ev = _events(db_file, "page_comment_edited")
    assert len(ev) == 1
    assert (
        ev[0]["target_kind"] == "page"
        and ev[0]["target_id"] == pid
        and ev[0]["actor_id"] == 1
    )


def test_rest_delete_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c, _space(c))
        cid = _comment(c, pid, actor="1")
        assert c.delete(f"/pages/{pid}/comments/{cid}", headers=H1).status_code == 204
        # Deleting again is a 404 and records nothing the second time.
        assert c.delete(f"/pages/{pid}/comments/{cid}", headers=H1).status_code == 404

    assert len(_events(db_file, "page_comment_deleted")) == 1


def test_edit_is_author_only_even_for_admin(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c, _space(c))
        cid = _comment(c, pid, body="bob's words", actor="2")  # authored by user 2
        r = c.patch(
            f"/pages/{pid}/comments/{cid}", json={"body": "hijacked"}, headers=H1
        )  # admin
        assert r.status_code == 403
    assert _events(db_file, "page_comment_edited") == []


def test_admin_can_moderate_delete_and_it_is_audited_to_the_admin(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c, _space(c))
        cid = _comment(c, pid, body="spam", actor="2")  # authored by user 2
        assert (
            c.delete(f"/pages/{pid}/comments/{cid}", headers=H1).status_code == 204
        )  # admin

    ev = _events(db_file, "page_comment_deleted")
    assert len(ev) == 1 and ev[0]["actor_id"] == 1  # the moderating admin


# --- web -------------------------------------------------------------------


def test_web_edit_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)  # a@e.com / pw is admin (id 1)
        pid = _page(c, _space(c))
        cid = _comment(c, pid, body="orig", actor="1")
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        r = c.post(
            f"/mentor/pages/{pid}/comments/{cid}/edit",
            data={"body": "edited-via-web"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

    assert len(_events(db_file, "page_comment_edited")) == 1


# --- command atomicity -----------------------------------------------------


def test_command_edit_vanished_comment_rejects_and_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c, _space(c))
    conn = db.connect(db_file)
    try:
        page_comment_commands.edit_page_comment(
            conn, actor={"id": 1}, page_id=pid, comment_id=999, body="x"
        )
        raise AssertionError("expected PageCommentCommandError")
    except page_comment_commands.PageCommentCommandError as exc:
        assert exc.kind == "not_found"
    assert [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "page_comment_edited"
    ] == []
