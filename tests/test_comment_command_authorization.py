"""Issue-comment commands authorize themselves.

`COMMAND_MIGRATION.md` named this debt: the comment commands took a bare actor id
and trusted the route's guards, so *the command owned the write and its audit,
not the gate*. A caller reaching the command directly — an MCP tool, a workflow,
a script — bypassed visibility and ownership entirely.

These tests call the commands with **no transport in front of them**. That is the
whole point: a route test cannot tell you whether the gate moved, because the
route would refuse first either way.

The wire contract is unchanged and pinned separately below: the same statuses come
out of the same requests. What changed is who enforces them, and when — the checks
now run inside the command's own write transaction, so an ownership test and the
write it guards can no longer straddle a boundary.
"""

from fastapi.testclient import TestClient
import pytest

from athena.aegis import comment_commands
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}

ANN = {"id": 1, "role": "admin"}
BOB = {"id": 2, "role": "member"}
CAT = {"id": 3, "role": "member"}


@pytest.fixture()
def app(tmp_path):
    return create_app(tmp_path / "cauth.db")


@pytest.fixture()
def world(app):
    """A public issue, a private one, and a comment on each — over the real API."""
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
        public = c.post(
            "/projects", json={"key": "PUB", "name": "Public"}, headers=H1
        ).json()
        private = c.post(
            "/projects", json={"key": "PRV", "name": "Private"}, headers=H1
        ).json()
        assert (
            c.put(
                f"/projects/{private['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )
        open_issue = c.post(
            "/issues", json={"title": "Open", "project_id": public["id"]}, headers=H1
        ).json()
        hidden_issue = c.post(
            "/issues", json={"title": "Hidden", "project_id": private["id"]}, headers=H1
        ).json()
        # Bob comments on the visible issue; Ann comments on the private one.
        bob_comment = c.post(
            f"/issues/{open_issue['id']}/comments",
            json={"body": "bob here"},
            headers=H2,
        ).json()
        return {
            "open": open_issue["id"],
            "hidden": hidden_issue["id"],
            "bob_comment": bob_comment["id"],
            "db": app.state.db_path,
        }


@pytest.fixture()
def conn(world):
    connection = db.connect(world["db"])
    yield connection
    connection.close()


# --- visibility now lives in the command ------------------------------------


def test_commenting_on_an_invisible_issue_is_refused_by_the_command(conn, world):
    # WHY: the debt, stated. Before, this call succeeded — the command trusted a
    # check that only the route performed, so anything else reaching it wrote a
    # comment onto an issue the actor could not see.
    with pytest.raises(comment_commands.CommentCommandError) as caught:
        comment_commands.create_comment(
            conn, actor=CAT, issue_id=world["hidden"], body="should not land"
        )
    assert caught.value.kind == "not_found"  # hidden reads as missing, never 403


def test_a_nonexistent_issue_is_refused_the_same_way(conn):
    with pytest.raises(comment_commands.CommentCommandError) as caught:
        comment_commands.create_comment(conn, actor=ANN, issue_id=9999, body="x")
    assert caught.value.kind == "not_found"


def test_the_visible_case_still_works(conn, world):
    comment = comment_commands.create_comment(
        conn, actor=CAT, issue_id=world["open"], body="cat here"
    )
    assert comment["body"] == "cat here" and comment["author_id"] == 3


# --- ownership now lives in the command -------------------------------------


def test_editing_someone_elses_comment_is_refused_by_the_command(conn, world):
    with pytest.raises(comment_commands.CommentCommandError) as caught:
        comment_commands.edit_comment(
            conn,
            actor=CAT,
            issue_id=world["open"],
            comment_id=world["bob_comment"],
            body="hijacked",
        )
    assert caught.value.kind == "forbidden"


def test_an_admin_may_not_rewrite_someone_elses_comment(conn, world):
    # WHY: the distinction the old route helper drew, preserved exactly. Removing
    # someone's words is moderation; rewriting them puts words in their mouth.
    with pytest.raises(comment_commands.CommentCommandError) as caught:
        comment_commands.edit_comment(
            conn,
            actor=ANN,
            issue_id=world["open"],
            comment_id=world["bob_comment"],
            body="reworded",
        )
    assert caught.value.kind == "forbidden"


def test_an_admin_may_delete_someone_elses_comment(conn, world):
    # ...but moderation IS allowed, and stays audited to the admin who did it.
    assert (
        comment_commands.delete_comment(
            conn, actor=ANN, issue_id=world["open"], comment_id=world["bob_comment"]
        )
        is True
    )


def test_the_author_may_edit_their_own(conn, world):
    updated = comment_commands.edit_comment(
        conn,
        actor=BOB,
        issue_id=world["open"],
        comment_id=world["bob_comment"],
        body="bob, revised",
    )
    assert updated["body"] == "bob, revised"


def test_a_comment_from_another_issue_is_not_found(conn, world):
    with pytest.raises(comment_commands.CommentCommandError) as caught:
        comment_commands.edit_comment(
            conn,
            actor=BOB,
            issue_id=world["hidden"],
            comment_id=world["bob_comment"],
            body="x",
        )
    # The issue check fires first: Bob cannot see the private issue at all.
    assert caught.value.kind == "not_found"


# --- the automation bypass stays narrow -------------------------------------


def test_only_the_automation_actor_may_use_the_bypass(conn, world):
    # WHY: automation acts on the target its rule selected rather than as an actor
    # who can see it, so it needs a bypass — and the bypass must not become a
    # general-purpose way around the gate.
    with pytest.raises(comment_commands.CommentCommandError) as caught:
        comment_commands.create_comment_as_automation(
            conn, actor_id=1, issue_id=world["hidden"], body="not automation"
        )
    assert caught.value.kind == "forbidden"


# --- the wire contract did not change ---------------------------------------


def test_the_http_statuses_are_unchanged(app, world):
    with TestClient(app) as c:
        # Author edits their own: 200.
        assert (
            c.patch(
                f"/issues/{world['open']}/comments/{world['bob_comment']}",
                json={"body": "revised"},
                headers=H2,
            ).status_code
            == 200
        )
        # Someone else edits it: 403.
        assert (
            c.patch(
                f"/issues/{world['open']}/comments/{world['bob_comment']}",
                json={"body": "nope"},
                headers={"X-Athena-Actor": "3"},
            ).status_code
            == 403
        )
        # Commenting on an issue you cannot see: 404, never 403.
        assert (
            c.post(
                f"/issues/{world['hidden']}/comments",
                json={"body": "x"},
                headers={"X-Athena-Actor": "3"},
            ).status_code
            == 404
        )
        # A comment that does not exist: 404.
        assert (
            c.delete(
                f"/issues/{world['open']}/comments/9999",
                headers=H2,
            ).status_code
            == 404
        )
