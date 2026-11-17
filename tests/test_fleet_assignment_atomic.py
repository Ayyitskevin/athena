"""A floor transfer succeeds or rolls back as one desk action, before radio."""

from contextlib import closing
import sqlite3
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from athena.aegis import issue_commands, issues, projects
from athena.core import budget_commands, buzz_radio, db, users
from athena.main import create_app
from athena.workflows import fleet_assign_commands


@pytest.fixture
def transfer(tmp_path):
    path = tmp_path / "transfer.db"
    with closing(db.connect(path)) as conn:
        db.migrate(conn)
        owner = users.create_user(
            conn, email="owner@example.invalid", name="Owner", role="admin"
        )
        member = users.create_user(
            conn,
            email="member@example.invalid",
            name="Member",
            password="local-test-password",
            role="member",
        )
        recipient = users.create_user(
            conn, email="grok@agents.local", name="Grok", is_agent=True
        )
        project = projects.create_project(
            conn, key="LAB", name="Lab", created_by=owner["id"]
        )
        issue = issue_commands.create_issue(
            conn, actor=owner, project_id=project["id"], title="Transfer"
        )
        issue_commands.update_issue(
            conn, actor=owner, issue_id=issue["id"], assignee_id=member["id"]
        )
        budget_commands.set_budget(
            conn,
            actor_id=owner["id"],
            target_user_id=member["id"],
            window="day",
            action_limit=10,
        )
        yield conn, path, member, recipient, issue


def test_member_floor_transfer_reaches_desk_before_radio(transfer, monkeypatch):
    conn, path, member, recipient, issue = transfer
    observed = []

    def radio(**kwargs):
        # A separate connection must see the committed transfer when the radio runs.
        with closing(db.connect(path)) as reader:
            assignee = issues.get_issue(reader, issue["id"])["assignee_id"]
            delegated = reader.execute(
                "SELECT user_id FROM issue_contributors WHERE issue_id=?",
                (issue["id"],),
            ).fetchone()
            observed.append((assignee, delegated["user_id"]))
        return {"status": "skipped", "detail": "local fixture"}

    monkeypatch.setattr(buzz_radio, "send_assignment", radio)
    start = conn.execute("SELECT MAX(id) FROM activity").fetchone()[0]
    with TestClient(create_app(path)) as client:
        login = client.post(
            "/login",
            data={"email": member["email"], "password": "local-test-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        client.headers["X-CSRF-Token"] = client.cookies["athena_csrf"]
        floor = f"/aegis/projects/{issue['project_id']}/floor"
        assert floor + "/assign" in client.get(floor).text
        response = client.post(
            floor + "/assign",
            data={"issue_id": issue["id"], "seat_slug": "grok"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "radio=skipped" in response.headers["location"]
        inbox = client.get(
            "/delegations/me", headers={"X-Athena-Actor": str(recipient["id"])}
        )
        assert inbox.status_code == 200
        assert [item["issue"]["id"] for item in inbox.json()["items"]] == [issue["id"]]
    assert observed == [(recipient["id"], recipient["id"])]
    events = conn.execute(
        "SELECT actor_id,verb FROM activity WHERE target_kind='issue' AND target_id=? AND id>? ORDER BY verb",
        (issue["id"], start),
    ).fetchall()
    assert [tuple(r) for r in events] == [
        (member["id"], "assigned"),
        (member["id"], "delegated"),
    ]
    assert (
        conn.execute(
            "SELECT action_used FROM agent_budgets WHERE user_id=?", (member["id"],)
        ).fetchone()[0]
        == 1
    )
    notices = conn.execute(
        "SELECT a.verb FROM notifications n JOIN activity a ON a.id=n.event_id "
        "WHERE n.user_id=? AND a.id>? ORDER BY a.verb",
        (recipient["id"], start),
    )
    assert [r["verb"] for r in notices] == ["assigned", "delegated"]


def test_failed_assignment_rolls_back_native_side_effects(transfer):
    conn, _, member, _, issue = transfer

    def snapshot():
        return {
            table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "issues",
                "issue_contributors",
                "activity",
                "activity_chain",
                "issue_assignee_facts",
                "notifications",
                "watches",
                "agent_budgets",
            )
        }

    before = snapshot()
    conn.execute(
        "CREATE TRIGGER refuse_assignment BEFORE INSERT ON activity "
        "WHEN NEW.verb='assigned' BEGIN SELECT RAISE(ABORT,'assignment audit refused'); END"
    )
    radio = Mock()
    with pytest.raises(sqlite3.IntegrityError, match="assignment audit refused"):
        fleet_assign_commands.assign_issue_to_seat(
            conn, actor=member, issue_id=issue["id"], seat_slug="grok", radio=radio
        )
    assert not conn.in_transaction
    assert snapshot() == before
    radio.assert_not_called()


def test_caller_owned_transaction_never_sends_or_commits(transfer):
    conn, path, _, recipient, issue = transfer
    owner = users.get_user(conn, issue["created_by"])
    radio = Mock(return_value={"status": "sent"})
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE issues SET title='uncommitted caller edit' WHERE id=?", (issue["id"],)
    )
    with pytest.raises(fleet_assign_commands.AssignError, match="transaction"):
        fleet_assign_commands.assign_issue_to_seat(
            conn, actor=owner, issue_id=issue["id"], seat_slug="grok", radio=radio
        )
    assert conn.in_transaction
    assert issues.get_issue(conn, issue["id"])["title"] == "uncommitted caller edit"
    assert issues.get_issue(conn, issue["id"])["assignee_id"] != recipient["id"]
    radio.assert_not_called()
    conn.rollback()
    with closing(db.connect(path)) as reader:
        assert issues.get_issue(reader, issue["id"])["title"] == "Transfer"


def test_repeat_by_owner_preserves_events_and_former_assignee_loses_authority(transfer):
    conn, _, member, recipient, issue = transfer
    radio = Mock(return_value={"status": "skipped"})
    args = {"issue_id": issue["id"], "seat_slug": "grok", "radio": radio}
    start = conn.execute("SELECT MAX(id) FROM activity").fetchone()[0]
    fleet_assign_commands.assign_issue_to_seat(conn, actor=member, **args)
    with pytest.raises(issue_commands.IssueCommandError) as refusal:
        fleet_assign_commands.assign_issue_to_seat(conn, actor=member, **args)
    assert refusal.value.kind == "forbidden"
    owner = users.get_user(conn, issue["created_by"])
    fleet_assign_commands.assign_issue_to_seat(conn, actor=owner, **args)
    assert radio.call_count == 2
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM issue_contributors WHERE issue_id=? AND user_id=?",
            (issue["id"], recipient["id"]),
        ).fetchone()[0]
        == 1
    )
    assert [
        r["verb"]
        for r in conn.execute(
            "SELECT verb FROM activity WHERE target_kind='issue' AND target_id=? AND id>? ORDER BY verb",
            (issue["id"], start),
        )
    ] == ["assigned", "delegated"]
