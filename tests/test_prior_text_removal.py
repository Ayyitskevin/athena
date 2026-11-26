"""An administrator can remove a kept prior text, and the trail says so.

WHY: F10 keeps the title and body each issue edit replaced, so editing a secret
out of an issue no longer removed it: anyone who could see the edit could read
it back. Decision 6 of docs/decisions/2026-11-26-owner-decisions.md: an
administrator may remove one edit's kept text, audited.

The contract pinned here:

* ``DELETE /issues/{id}/prior-texts/{event_id}`` by an administrator with an
  admin-scope credential removes that edit's kept text and records
  ``issue_prior_text_removed`` naming the edit (``edit <id>``), never the text;
  the read then answers 404, and the issue's current text and the edit's own
  event are untouched;
* anyone else is refused 403 and nothing changes; a missing issue, another
  issue's edit, an edit with no kept text and a text already removed all read
  404;
* the edit's undo then refuses as it does for an edit with no kept text;
* the database itself refuses a delete without its removal event.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, bootstrap, client_as, make_app, onboard_agent

from athena.core import db

SECRET = "token: ath_live_do_not_keep"


def _issue(client: TestClient, body: str = SECRET) -> int:
    made = client.post(
        "/issues", json={"title": "Leaky", "body": body}, headers=ADMIN
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _edit_out(client: TestClient, issue_id: int) -> int:
    edited = client.patch(
        f"/issues/{issue_id}", json={"body": "redacted"}, headers=ADMIN
    )
    assert edited.status_code == 200, edited.text
    feed = client.get(
        "/activity",
        params={"target_kind": "issue", "target_id": issue_id, "verb": "issue_edited"},
        headers=ADMIN,
    ).json()
    return feed[0]["id"]


def _removals(db_file: Path) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT actor_id, target_kind, target_id, detail FROM activity "
                "WHERE verb = 'issue_prior_text_removed' ORDER BY id"
            )
        ]
    finally:
        conn.close()


def _dump(db_file: Path) -> str:
    conn = db.connect(db_file)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def test_an_administrator_removes_a_kept_text_with_its_event(tmp_path):
    app, db_file = make_app(tmp_path, "removal.db")
    with TestClient(app) as client:
        bootstrap(client)
        issue_id = _issue(client)
        edit_id = _edit_out(client, issue_id)
        assert client.get(
            f"/issues/{issue_id}/prior-texts/{edit_id}", headers=ADMIN
        ).json()["prior_body"] == SECRET

        removed = client.delete(
            f"/issues/{issue_id}/prior-texts/{edit_id}", headers=ADMIN
        )
        assert removed.status_code == 200, removed.text
        assert removed.json() == {"event_id": edit_id, "removed": ["body"]}

        gone = client.get(f"/issues/{issue_id}/prior-texts/{edit_id}", headers=ADMIN)
        assert gone.status_code == 404
        issue = client.get(f"/issues/{issue_id}", headers=ADMIN).json()
        assert issue["body"] == "redacted"
        history = client.get(f"/issues/{issue_id}/history", headers=ADMIN).json()
        edit = next(i for i in history["items"] if i["signal"] == "edit")
        assert edit["prior_text"] is None

        # Its undo now refuses, as for an edit that never kept text.
        refused = client.post(f"/activity/{edit_id}/undo", headers=ADMIN)
        assert refused.status_code == 422
        assert refused.json()["code"] == "undo_not_reversible"

        again = client.delete(
            f"/issues/{issue_id}/prior-texts/{edit_id}", headers=ADMIN
        )
        assert again.status_code == 404

    assert _removals(db_file) == [
        {
            "actor_id": 1,
            "target_kind": "issue",
            "target_id": issue_id,
            "detail": f"edit {edit_id}",
        }
    ]
    # The secret is nowhere in the database any more: not in the kept text,
    # not in the removal event, not in the edit's own event.
    assert SECRET not in _dump(db_file)


def test_only_an_administrator_may_remove(tmp_path):
    app, db_file = make_app(tmp_path, "removal-gate.db")
    with TestClient(app) as client:
        bootstrap(client)
        issue_id = _issue(client)
        edit_id = _edit_out(client, issue_id)
        seat = onboard_agent(client, email="sol@e.com", name="Sol")
        seat_http = client_as(app, token=seat)
        refused = seat_http.delete(f"/issues/{issue_id}/prior-texts/{edit_id}")
        assert refused.status_code == 403
        assert refused.json()["code"] == "forbidden"
        assert client.get(
            f"/issues/{issue_id}/prior-texts/{edit_id}", headers=ADMIN
        ).status_code == 200
    assert _removals(db_file) == []


def test_what_is_not_a_kept_text_of_this_issue_reads_as_missing(tmp_path):
    app, db_file = make_app(tmp_path, "removal-missing.db")
    with TestClient(app) as client:
        bootstrap(client)
        issue_id = _issue(client)
        other_id = _issue(client, body="other")
        edit_id = _edit_out(client, issue_id)
        created_id = client.get(
            "/activity",
            params={"target_kind": "issue", "target_id": issue_id, "verb": "created"},
            headers=ADMIN,
        ).json()[0]["id"]
        for path in (
            f"/issues/{other_id}/prior-texts/{edit_id}",  # another issue's edit
            f"/issues/{issue_id}/prior-texts/{created_id}",  # not an edit
            f"/issues/{10**6}/prior-texts/{edit_id}",  # no such issue
        ):
            answer = client.delete(path, headers=ADMIN)
            assert answer.status_code == 404, (path, answer.text)
            assert answer.json()["code"] == "not_found"
    assert _removals(db_file) == []


def test_the_database_refuses_a_delete_without_its_removal_event(tmp_path):
    app, db_file = make_app(tmp_path, "removal-trigger.db")
    with TestClient(app) as client:
        bootstrap(client)
        issue_id = _issue(client)
        edit_id = _edit_out(client, issue_id)
        other_id = _issue(client, body="other")
        _edit_out(client, other_id)
    conn = db.connect(db_file)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="removal event"):
            conn.execute(
                "DELETE FROM issue_prior_texts WHERE event_id = ?", (edit_id,)
            )
        # An event naming another edit, or another issue, does not open it.
        conn.execute(
            "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail) "
            "VALUES (1, 'issue_prior_text_removed', 'issue', ?, ?)",
            (other_id, f"edit {edit_id}"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="removal event"):
            conn.execute(
                "DELETE FROM issue_prior_texts WHERE event_id = ?", (edit_id,)
            )
    finally:
        conn.rollback()
        conn.close()


def test_the_command_owns_its_gate(tmp_path):
    """A caller reaching the command directly is refused, not trusted: a member,
    and an administrator paused after the caller looked them up."""
    from athena.aegis import prior_text_commands
    from athena.core import users

    app, db_file = make_app(tmp_path, "removal-command.db")
    with TestClient(app) as client:
        bootstrap(client)
        issue_id = _issue(client)
        edit_id = _edit_out(client, issue_id)
        seat = onboard_agent(client, email="sol@e.com", name="Sol")
    conn = db.connect(db_file)
    try:
        admin = users.get_user(conn, 1)
        member = users.get_user(conn, seat["user"]["id"])
        users.set_paused(conn, 1, True, commit=True)
        for actor in (member, admin):
            with pytest.raises(prior_text_commands.PriorTextCommandError) as refused:
                prior_text_commands.remove_prior_text(
                    conn, actor=actor, issue_id=issue_id, event_id=edit_id
                )
            assert refused.value.kind == "forbidden"
        users.set_paused(conn, 1, False, commit=True)
        assert prior_text_commands.remove_prior_text(
            conn, actor=admin, issue_id=issue_id, event_id=edit_id
        ) == {"event_id": edit_id, "removed": ["body"]}
    finally:
        conn.close()
    assert len(_removals(db_file)) == 1
