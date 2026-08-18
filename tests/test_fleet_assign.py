"""Fleet assign: desk first, radio optional."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import buzz_radio, db, users
from athena.main import create_app
from athena.workflows import fleet_assign_commands


def _radio(**kwargs):
    return {"status": "sent", "detail": "fake"}


def test_assignment_message_is_a_new_job_not_a_steer():
    text = buzz_radio.assignment_message(
        seat_name="Grok",
        issue_key="MWS-2",
        title="Hunt",
        url="http://example/aegis/issues/2",
        note="please",
    )
    assert text.startswith("ATHENA_ASSIGN MWS-2")
    assert "not a steer" in text
    assert "please" in text


def test_radio_never_parses_assignment_text_through_a_shell(
    monkeypatch, tmp_path: Path
):
    key_file = tmp_path / "buzz.env"
    key_file.write_text("SECKEY=synthetic-test-key\n", encoding="utf-8")
    monkeypatch.setenv("ATHENA_BUZZ_CLI", "/opt/buzz/bin/buzz")
    monkeypatch.setenv("ATHENA_BUZZ_KEY_FILE", str(key_file))
    monkeypatch.setenv("ATHENA_BUZZ_RELAY_URL", "ws://127.0.0.1:3000")

    observed = {}

    def runner(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    title = '$(touch /tmp/not-a-command) "quoted"'
    note = "`id` && false; --broadcast"
    result = buzz_radio.send_assignment(
        seat_name="Codex",
        buzz_pubkey="npub-test",
        issue_key="ATH-1",
        title=title,
        url="http://athena.test/aegis/issues/1",
        note=note,
        runner=runner,
    )

    assert result["status"] == "sent"
    assert observed["kwargs"]["shell"] is False
    argv = observed["argv"]
    assert argv[:3] == ["/opt/buzz/bin/buzz", "messages", "send"]
    assert argv[-2] == "--content"
    assert title in argv[-1]
    assert note in argv[-1]
    assert "synthetic-test-key" not in argv
    assert (
        observed["kwargs"]["env"]["BUZZ_PRIVATE_KEY"] == "synthetic-test-key"
    )


def test_assign_sets_assignee_and_delegates(tmp_path):
    conn = db.connect(tmp_path / "assign.db")
    db.migrate(conn)
    admin = users.create_user(
        conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE
    )
    users.create_user(conn, email="grok@agents.local", name="Grok", is_agent=True)
    issue = issues.create_issue(
        conn, title="slice", body="do it", created_by=admin["id"]
    )
    result = fleet_assign_commands.assign_issue_to_seat(
        conn,
        actor=admin,
        issue_id=issue["id"],
        seat_slug="grok",
        note="go",
        radio=_radio,
    )
    assert result["seat"] == "grok"
    assert result["radio"]["status"] == "sent"
    fresh = issues.get_issue(conn, issue["id"])
    assert fresh["assignee_id"] == result["agent_id"]
    conn.close()


def test_assign_unknown_seat_and_operator(tmp_path):
    conn = db.connect(tmp_path / "bad-seat.db")
    db.migrate(conn)
    admin = users.create_user(
        conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE
    )
    issue = issues.create_issue(conn, title="x", body="", created_by=admin["id"])
    try:
        fleet_assign_commands.assign_issue_to_seat(
            conn, actor=admin, issue_id=issue["id"], seat_slug="nope", radio=_radio
        )
        raise AssertionError("unknown seat")
    except fleet_assign_commands.AssignError as exc:
        assert "unknown seat" in exc.detail
    try:
        fleet_assign_commands.assign_issue_to_seat(
            conn, actor=admin, issue_id=issue["id"], seat_slug="kevin", radio=_radio
        )
        raise AssertionError("operator")
    except fleet_assign_commands.AssignError as exc:
        assert "operator" in exc.detail
    conn.close()


def test_admin_form_assigns(tmp_path):
    app = create_app(tmp_path / "assign-web.db")
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "admin@e.com", "name": "Admin", "password": "secret"},
        )
        client.post(
            "/users/onboard_agent",
            json={"name": "Grok", "scopes": ["read", "issue:write"]},
            headers={"X-Athena-Actor": "1"},
        )
        issue = client.post(
            "/issues",
            json={"title": "desk work"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        client.post(
            "/login",
            data={"email": "admin@e.com", "password": "secret"},
            follow_redirects=False,
        )
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        page = client.get("/admin/fleet")
        assert page.status_code == 200
        assert "Assign to a seat" in page.text
        posted = client.post(
            "/admin/fleet/assign",
            data={"issue_id": issue["id"], "seat_slug": "grok", "note": "now"},
            follow_redirects=False,
        )
        assert posted.status_code == 303, posted.text
        assert "assigned=" in posted.headers["location"]
        assert "seat=grok" in posted.headers["location"]
        assert "radio=skipped" in posted.headers["location"]
