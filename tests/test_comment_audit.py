"""Issue-comment lifecycle changes (create, edit, delete) are now audited atomically.

A comment is issue content. Creating and deleting one recorded an event, but in a
SEPARATE commit from the row change (a crash between the two lost the event); EDITING a
comment recorded NOTHING at all, so a body could be silently rewritten over the API and
the web. These tests pin that create/edit/delete each record a commented / comment_edited
/ comment_deleted event in the SAME transaction as the change, once per real change; that
the edit — previously silent — is now on the record; and that the author-ownership and
admin-moderation authz is preserved.
"""

from fastapi.testclient import TestClient

from athena.aegis import comment_commands
from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # user 1 — first user, admin
H2 = {"X-Athena-Actor": "2"}  # user 2 — a non-admin member


def _app(tmp_path, name="comments.db"):
    return create_app(tmp_path / name), tmp_path / name


def _two_users(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "Bob", "password": "pw"}, headers=H1
    )


def _issue(client, actor="1") -> int:
    return client.post(
        "/issues", json={"title": "t"}, headers={"X-Athena-Actor": actor}
    ).json()["id"]


def _comment(client, issue_id, body="hello", actor="1") -> int:
    r = client.post(
        f"/issues/{issue_id}/comments",
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
        _two_users(c)
        iid = _issue(c)
        _comment(c, iid, body="first", actor="2")

    ev = _events(db_file, "commented")
    assert len(ev) == 1
    assert ev[0]["target_kind"] == "issue" and ev[0]["target_id"] == iid
    assert ev[0]["actor_id"] == 2
    # Commenting is participation: the auto-watch landed in the same transaction.
    conn = db.connect(db_file)
    watched = conn.execute(
        "SELECT 1 FROM watches WHERE user_id = 2 AND target_kind = 'issue' AND target_id = ?",
        (iid,),
    ).fetchone()
    conn.close()
    assert watched is not None


def test_rest_edit_is_audited(tmp_path):
    # The headline gap: editing a comment was a completely silent content rewrite.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        iid = _issue(c)
        cid = _comment(c, iid, body="orig", actor="1")
        r = c.patch(
            f"/issues/{iid}/comments/{cid}", json={"body": "edited"}, headers=H1
        )
        assert r.status_code == 200 and r.json()["body"] == "edited"

    ev = _events(db_file, "comment_edited")
    assert len(ev) == 1
    assert (
        ev[0]["target_kind"] == "issue"
        and ev[0]["target_id"] == iid
        and ev[0]["actor_id"] == 1
    )


def test_rest_delete_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        iid = _issue(c)
        cid = _comment(c, iid, actor="1")
        assert c.delete(f"/issues/{iid}/comments/{cid}", headers=H1).status_code == 204
        # Deleting again is a 404 and records nothing the second time.
        assert c.delete(f"/issues/{iid}/comments/{cid}", headers=H1).status_code == 404

    assert len(_events(db_file, "comment_deleted")) == 1


def test_edit_is_author_only_even_for_admin(tmp_path):
    # Author-ownership holds even against an admin: rewriting someone's words would put
    # words in their mouth. The forbidden edit records nothing.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        iid = _issue(c)
        cid = _comment(c, iid, body="bob's words", actor="2")  # authored by user 2
        r = c.patch(
            f"/issues/{iid}/comments/{cid}", json={"body": "hijacked"}, headers=H1
        )  # admin
        assert r.status_code == 403
    assert _events(db_file, "comment_edited") == []


def test_admin_can_moderate_delete_and_it_is_audited_to_the_admin(tmp_path):
    # Delete lifts author-ownership for admin moderation (spam/abuse) — and the removal
    # is audited TO the admin, so the moderation is on the record.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        iid = _issue(c)
        cid = _comment(c, iid, body="spam", actor="2")  # authored by user 2
        assert (
            c.delete(f"/issues/{iid}/comments/{cid}", headers=H1).status_code == 204
        )  # admin

    ev = _events(db_file, "comment_deleted")
    assert len(ev) == 1 and ev[0]["actor_id"] == 1  # the moderating admin


# --- web -------------------------------------------------------------------


def test_web_edit_is_audited(tmp_path):
    # The web edit path ALSO recorded nothing before this slice.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)  # a@e.com / pw is admin (id 1)
        iid = _issue(c)
        cid = _comment(c, iid, body="orig", actor="1")
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        r = c.post(
            f"/aegis/issues/{iid}/comments/{cid}/edit",
            data={"body": "edited-via-web"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

    assert len(_events(db_file, "comment_edited")) == 1


# --- command atomicity -----------------------------------------------------


def test_command_edit_vanished_comment_rejects_and_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        iid = _issue(c)
    conn = db.connect(db_file)
    try:
        comment_commands.edit_comment(
            conn, actor_id=1, issue_id=iid, comment_id=999, body="x"
        )
        raise AssertionError("expected CommentCommandError")
    except comment_commands.CommentCommandError as exc:
        assert exc.kind == "not_found"
    assert [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "comment_edited"
    ] == []
