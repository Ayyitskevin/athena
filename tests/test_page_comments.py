"""The page-comments REST API — Mentor's Confluence-style discussion thread.

The page twin of the Aegis issue-comment contract (test_comments.py): a comment is
added by any signed-in actor onto a real page, listed oldest-first with the
author's name, edited/deleted only by its author, and its add/delete both land on
the page's audit trail. These pin all of that, plus the 404/403/422 boundaries.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # bootstrap admin
H2 = {"X-Athena-Actor": "2"}  # a second member


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A"})
    client.post("/users", json={"email": "b@e.com", "name": "B", "role": "member"}, headers=H1)


def _space(client, key="ENG", name="Eng"):
    r = client.post("/spaces", json={"key": key, "name": name}, headers=H1)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _page(client, space_id, title="Doc"):
    r = client.post(f"/spaces/{space_id}/pages", json={"title": title}, headers=H1)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_add_list_oldest_first_with_author(tmp_path):
    with TestClient(create_app(tmp_path / "add.db")) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))

        first = client.post(f"/pages/{pid}/comments", json={"body": "first"}, headers=H1)
        assert first.status_code == 201, first.text
        assert first.json()["author_name"] == "A" and first.json()["page_id"] == pid
        client.post(f"/pages/{pid}/comments", json={"body": "second"}, headers=H2)

        listed = client.get(f"/pages/{pid}/comments").json()
        # Oldest first (id order), each carrying the author's display name.
        assert [(c["body"], c["author_name"]) for c in listed] == [
            ("first", "A"),
            ("second", "B"),
        ]


def test_blank_body_is_422(tmp_path):
    with TestClient(create_app(tmp_path / "blank.db")) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        assert client.post(f"/pages/{pid}/comments", json={"body": "   "}, headers=H1).status_code == 422


def test_unknown_page_is_404(tmp_path):
    with TestClient(create_app(tmp_path / "missing.db")) as client:
        _bootstrap(client)
        assert client.get("/pages/9999/comments").status_code == 404
        assert client.post("/pages/9999/comments", json={"body": "x"}, headers=H1).status_code == 404


def test_author_only_edit_and_delete(tmp_path):
    with TestClient(create_app(tmp_path / "author.db")) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        cid = client.post(f"/pages/{pid}/comments", json={"body": "mine"}, headers=H1).json()["id"]

        # A different user can neither edit nor delete it.
        assert client.patch(
            f"/pages/{pid}/comments/{cid}", json={"body": "hijack"}, headers=H2
        ).status_code == 403
        assert client.delete(f"/pages/{pid}/comments/{cid}", headers=H2).status_code == 403
        # The author can edit, then delete.
        edited = client.patch(f"/pages/{pid}/comments/{cid}", json={"body": "mine v2"}, headers=H1)
        assert edited.status_code == 200 and edited.json()["body"] == "mine v2"
        assert client.delete(f"/pages/{pid}/comments/{cid}", headers=H1).status_code == 204
        assert client.get(f"/pages/{pid}/comments").json() == []


def test_admin_can_delete_but_not_edit_another_users_comment(tmp_path):
    # WHY: the moderation override. H1 is the bootstrap ADMIN, H2 a member. An admin may
    # DELETE any comment (spam/abuse) but still may NOT EDIT another's words (that would put
    # words in their mouth). So H2 writes, the admin is refused the edit but allowed the
    # delete, and the removal lands on the audit trail stamped to the admin.
    db_file = tmp_path / "admin_mod.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        cid = client.post(
            f"/pages/{pid}/comments", json={"body": "b's words"}, headers=H2
        ).json()["id"]

        # Admin cannot rewrite another user's comment...
        assert client.patch(
            f"/pages/{pid}/comments/{cid}", json={"body": "admin rewrite"}, headers=H1
        ).status_code == 403
        # ...but CAN delete it (moderation).
        assert client.delete(f"/pages/{pid}/comments/{cid}", headers=H1).status_code == 204
        assert client.get(f"/pages/{pid}/comments").json() == []

    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id FROM activity WHERE target_kind = 'page' AND target_id = ? "
        "AND verb = 'page_comment_deleted' ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
    conn.close()
    assert row["actor_id"] == 1  # audited to the admin moderator, not the author


def test_comment_must_belong_to_the_named_page(tmp_path):
    with TestClient(create_app(tmp_path / "wrongpage.db")) as client:
        _bootstrap(client)
        sid = _space(client)
        p1, p2 = _page(client, sid, "One"), _page(client, sid, "Two")
        cid = client.post(f"/pages/{p1}/comments", json={"body": "on one"}, headers=H1).json()["id"]
        # Addressing p1's comment under p2's path is a 404, not a cross-page edit.
        assert client.patch(
            f"/pages/{p2}/comments/{cid}", json={"body": "x"}, headers=H1
        ).status_code == 404
        assert client.delete(f"/pages/{p2}/comments/{cid}", headers=H1).status_code == 404


def test_comment_and_delete_land_on_the_activity_trail(tmp_path):
    db_file = tmp_path / "trail.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        cid = client.post(f"/pages/{pid}/comments", json={"body": "hi"}, headers=H1).json()["id"]
        client.delete(f"/pages/{pid}/comments/{cid}", headers=H1)
    conn = db.connect(db_file)
    verbs = [
        r["verb"]
        for r in conn.execute(
            "SELECT verb FROM activity WHERE target_kind = 'page' AND target_id = ? ORDER BY id",
            (pid,),
        ).fetchall()
    ]
    conn.close()
    # page_created from the page, then the comment add + delete facts.
    assert "page_commented" in verbs and "page_comment_deleted" in verbs
