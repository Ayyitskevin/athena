"""Mentor's page and page-comment commands authorize themselves.

`COMMAND_MIGRATION.md` named this debt: the page and page-comment commands took a
bare actor id and trusted the route's guards (`access.can_see_space` and friends),
so *the command owned the write and its audit, not the gate*. A caller reaching a
command directly — undo, a workflow, a script — bypassed visibility and ownership
entirely.

These tests call the commands with **no transport in front of them**, exactly as
`test_comment_command_authorization.py` does for the aegis twin: a route test
cannot tell you whether the gate moved, because the route would refuse first
either way. The wire contract is pinned separately at the bottom — the same
statuses come out of the same requests; what changed is who enforces them, and
that the check and the write now share one transaction.
"""

from fastapi.testclient import TestClient
import pytest

from athena.core import db, labels
from athena.main import create_app
from athena.mentor import page_comment_commands, page_commands

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}

ANN = {"id": 1, "role": "admin"}
BOB = {"id": 2, "role": "member"}
CAT = {"id": 3, "role": "member"}


@pytest.fixture()
def app(tmp_path):
    return create_app(tmp_path / "pauth.db")


@pytest.fixture()
def world(app):
    """A public space and a private one, a page in each, and Bob's comment on the
    public page — all built over the real API."""
    with TestClient(app) as c:
        for email, name in (
            ("ann@e.com", "Ann"),
            ("bob@e.com", "Bob"),
            ("cat@e.com", "Cat"),
        ):
            c.post(
                "/users",
                json={"email": email, "name": name, "password": "secret"},
                headers=H1,
            )
        public = c.post("/spaces", json={"key": "PUB", "name": "Open"}, headers=H1)
        private = c.post("/spaces", json={"key": "PRV", "name": "Hidden"}, headers=H1)
        public, private = public.json(), private.json()
        assert (
            c.put(
                f"/spaces/{private['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )
        open_page = c.post(
            f"/spaces/{public['id']}/pages",
            json={"title": "Open page", "body": "text"},
            headers=H1,
        ).json()
        hidden_page = c.post(
            f"/spaces/{private['id']}/pages",
            json={"title": "Hidden page", "body": "secret"},
            headers=H1,
        ).json()
        label = c.post("/labels", json={"name": "docs"}, headers=H1).json()
        bob_comment = c.post(
            f"/pages/{open_page['id']}/comments",
            json={"body": "bob here"},
            headers=H2,
        ).json()
        return {
            "public_space": public["id"],
            "private_space": private["id"],
            "open": open_page["id"],
            "hidden": hidden_page["id"],
            "label": label["id"],
            "bob_comment": bob_comment["id"],
            "db": app.state.db_path,
        }


@pytest.fixture()
def conn(world):
    connection = db.connect(world["db"])
    yield connection
    connection.close()


# --- visibility now lives in the page commands -------------------------------


def test_every_page_write_refuses_a_hidden_page_inside_the_command(conn, world):
    # WHY: the debt, stated once for the whole surface. Before, every one of
    # these calls succeeded for an actor who could not see the page — the
    # commands trusted checks only the routes performed. One test, all entry
    # points, so a new page write cannot forget the rule without failing here.
    hidden = world["hidden"]
    refusals = [
        lambda: page_commands.edit_page(conn, actor=CAT, page_id=hidden, body="x"),
        lambda: page_commands.move_page(
            conn, actor=CAT, page_id=hidden, new_parent_id=None
        ),
        lambda: page_commands.restore_page_version(
            conn, actor=CAT, page_id=hidden, version=1
        ),
        lambda: page_commands.delete_page(conn, actor=CAT, page_id=hidden),
        lambda: page_commands.attach_page_label(
            conn, actor=CAT, page_id=hidden, label_id=world["label"]
        ),
        lambda: page_commands.attach_page_label_by_name(
            conn, actor=CAT, page_id=hidden, name="sneak"
        ),
        lambda: page_commands.detach_page_label(
            conn, actor=CAT, page_id=hidden, label_id=world["label"]
        ),
        lambda: page_commands.set_page_archived(
            conn, actor=CAT, page_id=hidden, archived=True
        ),
    ]
    for refusal in refusals:
        with pytest.raises(page_commands.PageCommandError) as caught:
            refusal()
        # Hidden reads as missing, never 403 — a write path proves no existence.
        assert caught.value.kind == "not_found"


def test_every_space_scoped_create_refuses_a_hidden_space(conn, world):
    private = world["private_space"]
    refusals = [
        lambda: page_commands.create_page(
            conn, actor=CAT, space_id=private, title="Sneak", body=""
        ),
        lambda: page_commands.create_page_from_template(
            conn, actor=CAT, space_id=private, template_id=world["hidden"], title="T"
        ),
        lambda: page_commands.ensure_daily_page(conn, actor=CAT, space_id=private),
    ]
    for refusal in refusals:
        with pytest.raises(page_commands.PageCommandError) as caught:
            refusal()
        assert caught.value.kind == "not_found"


def test_a_refused_attach_by_name_grows_no_vocabulary(conn, world):
    # WHY: the find-or-create shares the attach's transaction, so the refusal
    # rolls the vocabulary write back too. Under the old transport-side gate this
    # path was unreachable; now that the command IS the gate, the property has to
    # hold here, not in a route.
    with pytest.raises(page_commands.PageCommandError):
        page_commands.attach_page_label_by_name(
            conn, actor=CAT, page_id=world["hidden"], name="sneaky-label"
        )
    assert labels.get_label_by_name(conn, "sneaky-label") is None


def test_the_visible_case_still_works(conn, world):
    page = page_commands.edit_page(
        conn, actor=CAT, page_id=world["open"], body="cat edited this"
    )
    assert page["body"] == "cat edited this"


def test_delete_refuses_children_inside_the_command(conn, world):
    # The no-cascade rule moved in with the visibility check: both now share the
    # write transaction, so a child created in a race can no longer slip past a
    # boundary-side count.
    child = page_commands.create_page(
        conn,
        actor=ANN,
        space_id=world["public_space"],
        title="Child",
        parent_id=world["open"],
    )
    with pytest.raises(page_commands.PageCommandError) as caught:
        page_commands.delete_page(conn, actor=ANN, page_id=world["open"])
    assert caught.value.kind == "conflict"
    deleted = page_commands.delete_page(conn, actor=ANN, page_id=child["id"])
    assert deleted["title"] == "Child"  # the row as it was, for redirects/audit


# --- ownership now lives in the page-comment commands ------------------------


def test_commenting_on_an_invisible_page_is_refused_by_the_command(conn, world):
    with pytest.raises(page_comment_commands.PageCommentCommandError) as caught:
        page_comment_commands.create_page_comment(
            conn, actor=CAT, page_id=world["hidden"], body="should not land"
        )
    assert caught.value.kind == "not_found"


def test_editing_someone_elses_page_comment_is_refused_by_the_command(conn, world):
    with pytest.raises(page_comment_commands.PageCommentCommandError) as caught:
        page_comment_commands.edit_page_comment(
            conn,
            actor=CAT,
            page_id=world["open"],
            comment_id=world["bob_comment"],
            body="hijacked",
        )
    assert caught.value.kind == "forbidden"


def test_an_admin_may_not_rewrite_but_may_delete(conn, world):
    # Removing someone's words is moderation; rewriting them puts words in their
    # mouth — the same line the aegis twin draws, preserved exactly.
    with pytest.raises(page_comment_commands.PageCommentCommandError) as caught:
        page_comment_commands.edit_page_comment(
            conn,
            actor=ANN,
            page_id=world["open"],
            comment_id=world["bob_comment"],
            body="reworded",
        )
    assert caught.value.kind == "forbidden"
    assert (
        page_comment_commands.delete_page_comment(
            conn, actor=ANN, page_id=world["open"], comment_id=world["bob_comment"]
        )
        is True
    )


def test_a_comment_from_another_page_is_not_found(conn, world):
    with pytest.raises(page_comment_commands.PageCommentCommandError) as caught:
        page_comment_commands.edit_page_comment(
            conn,
            actor=BOB,
            page_id=world["hidden"],
            comment_id=world["bob_comment"],
            body="x",
        )
    # The page check fires first: Bob cannot see the private page at all.
    assert caught.value.kind == "not_found"


# --- the wire contract did not change ---------------------------------------


def test_the_http_statuses_are_unchanged(app, world):
    with TestClient(app) as c:
        h3 = {"X-Athena-Actor": "3"}
        # Editing a page in a private space you cannot see: 404, never 403.
        assert (
            c.patch(
                f"/pages/{world['hidden']}", json={"body": "x"}, headers=h3
            ).status_code
            == 404
        )
        # Someone else edits Bob's comment: 403.
        assert (
            c.patch(
                f"/pages/{world['open']}/comments/{world['bob_comment']}",
                json={"body": "nope"},
                headers=h3,
            ).status_code
            == 403
        )
        # Deleting a page that still has children: 409.
        child = c.post(
            f"/spaces/{world['public_space']}/pages",
            json={"title": "Child2", "parent_id": world["open"]},
            headers=H1,
        ).json()
        assert c.delete(f"/pages/{world['open']}", headers=H1).status_code == 409
        assert c.delete(f"/pages/{child['id']}", headers=H1).status_code == 204
        # The daily note of a hidden space: 404.
        assert (
            c.post(f"/spaces/{world['private_space']}/daily", headers=h3).status_code
            == 404
        )
