"""Inbox rows say when their target is already handled.

Each inbox row carries a read-only ``target_state``, computed at read time from the
target issue and the reader's own completion events:

    archived          the target issue is archived (wins over the others)
    completed_by_you  the reader completed the target (a claim_completed event)
    closed            the target's status is in the done category
    null              none of the above

The annotation is a read. It never acknowledges, never writes a notification, and a
hidden target's state is never shown: a row the reader cannot see is not returned at
all. Positive controls sit beside each case, and every test runs on the REST, web or
MCP surface that a reader would actually use.
"""

from html import escape

from athena.core import activity, db, notifications
from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}  # the admin / creator
H2 = {"X-Athena-Actor": "2"}  # the watcher whose inbox is read


def _app(tmp_path, name):
    db_file = tmp_path / name
    return create_app(db_file), db_file


def _setup_users(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)  # id 2


def _inbox(client, headers=H2):
    return client.get("/notifications", headers=headers).json()


def _row_for(rows, issue_id, verb):
    matches = [r for r in rows if r["target_id"] == issue_id and r["verb"] == verb]
    assert len(matches) == 1, rows
    return matches[0]


def _watch_and_change(client, issue_id, status):
    client.post(
        "/watches", json={"target_kind": "issue", "target_id": issue_id}, headers=H2
    )
    r = client.patch(f"/issues/{issue_id}", json={"status": status}, headers=H1)
    assert r.status_code == 200, r.text


# --- one annotation each -----------------------------------------------------


def test_closed_target_is_annotated_closed_for_the_watcher(tmp_path):
    app, _ = _app(tmp_path, "closed.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "ship it"}, headers=H1).json()
        _watch_and_change(client, issue["id"], "done")

        row = _row_for(_inbox(client), issue["id"], "changed_status")
        assert row["target_state"] == "closed"


def test_open_target_has_no_annotation(tmp_path):
    # Positive control: the same flow to an in-progress status annotates nothing.
    app, _ = _app(tmp_path, "open.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "keep going"}, headers=H1).json()
        _watch_and_change(client, issue["id"], "in_progress")

        row = _row_for(_inbox(client), issue["id"], "changed_status")
        assert row["target_state"] is None


def test_archived_target_is_annotated_archived_even_when_done(tmp_path):
    app, _ = _app(tmp_path, "archived.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "old"}, headers=H1).json()
        _watch_and_change(client, issue["id"], "done")
        r = client.post(f"/issues/{issue['id']}/archive", headers=H1)
        assert r.status_code in (200, 204), r.text

        row = _row_for(_inbox(client), issue["id"], "archived")
        assert row["target_state"] == "archived"


def _complete_as_reader(db_file, issue_id):
    conn = db.connect(db_file)
    activity.record(
        conn,
        actor_id=2,
        verb="claim_completed",
        target_kind="issue",
        target_id=issue_id,
        detail="generation 1",
    )
    conn.commit()


def test_a_notification_older_than_the_completion_is_labelled_completed_by_you(
    tmp_path,
):
    # Positive control: the row describes an event the reader had already handled.
    app, db_file = _app(tmp_path, "completed.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "claimed"}, headers=H1).json()
        client.post(
            "/watches",
            json={"target_kind": "issue", "target_id": issue["id"]},
            headers=H2,
        )
        client.post(
            f"/issues/{issue['id']}/comments", json={"body": "thanks"}, headers=H1
        )
        _complete_as_reader(db_file, issue["id"])

        row = _row_for(_inbox(client), issue["id"], "commented")
        assert row["target_state"] == "completed_by_you"


def test_a_notification_after_the_completion_is_not_labelled_completed_by_you(tmp_path):
    # The reader finished, then the operator asks for more. The new row describes an
    # unhandled request, so it must not read as already handled.
    app, db_file = _app(tmp_path, "later.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "claimed"}, headers=H1).json()
        client.post(
            "/watches",
            json={"target_kind": "issue", "target_id": issue["id"]},
            headers=H2,
        )
        client.post(
            f"/issues/{issue['id']}/comments", json={"body": "first"}, headers=H1
        )
        _complete_as_reader(db_file, issue["id"])
        client.post(
            f"/issues/{issue['id']}/comments",
            json={"body": "please also add the PR link"},
            headers=H1,
        )

        rows = _inbox(client)
        comments = sorted(
            [
                r
                for r in rows
                if r["verb"] == "commented" and r["target_id"] == issue["id"]
            ],
            key=lambda r: r["event_id"],
        )
        assert [r["target_state"] for r in comments] == ["completed_by_you", None]


def test_the_notification_schema_keeps_its_name_and_gains_the_field(tmp_path):
    # Contract: the inbox response stays documented as NotificationOut. target_state is
    # an additive field on that schema, not a renamed type.
    app, _ = _app(tmp_path, "schema.json.db")
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "NotificationStateOut" not in schemas
    assert "target_state" in schemas["NotificationOut"]["properties"]
    response = spec["paths"]["/notifications"]["get"]["responses"]["200"]
    items = response["content"]["application/json"]["schema"]["items"]
    assert items == {"$ref": "#/components/schemas/NotificationOut"}


def test_completion_is_per_reader_not_global(tmp_path):
    # The completion belongs to user 2. Their claim_completed notifies the creator
    # (user 1), whose row must not say "completed_by_you".
    app, db_file = _app(tmp_path, "perreader.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "claimed"}, headers=H1).json()
        conn = db.connect(db_file)
        activity.record(
            conn,
            actor_id=2,
            verb="claim_completed",
            target_kind="issue",
            target_id=issue["id"],
            detail="generation 1",
        )
        conn.commit()

        creator_rows = _inbox(client, headers=H1)
        row = _row_for(creator_rows, issue["id"], "claim_completed")
        assert row["target_state"] is None


def test_imported_completion_does_not_mark_the_reader_as_completing(tmp_path):
    # An imported claim_completed is foreign history: it must not count as the
    # reader's own completion. The reader's row must stay unannotated.
    app, db_file = _app(tmp_path, "imported.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "imported"}, headers=H1).json()
        client.post(
            "/watches",
            json={"target_kind": "issue", "target_id": issue["id"]},
            headers=H2,
        )
        client.post(
            f"/issues/{issue['id']}/comments", json={"body": "again"}, headers=H1
        )
        # The imported completion is recorded AFTER the row, so it would be labelled
        # if imported history counted. It must not be.
        conn = db.connect(db_file)
        activity.record(
            conn,
            actor_id=2,
            verb="claim_completed",
            target_kind="issue",
            target_id=issue["id"],
            detail="imported generation 1",
            imported_at="2026-01-01 00:00:00",
        )
        conn.commit()

        row = _row_for(_inbox(client), issue["id"], "commented")
        assert row["target_state"] is None


# --- hidden targets ----------------------------------------------------------


def test_hidden_target_row_and_its_state_are_never_returned(tmp_path):
    app, _ = _app(tmp_path, "hidden.db")
    with TestClient(app) as client:
        _setup_users(client)
        pid = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H1
        ).json()["id"]
        issue = client.post(
            "/issues", json={"title": "secret", "project_id": pid}, headers=H1
        ).json()
        _watch_and_change(client, issue["id"], "done")

        # Positive control, while the project is public: the reader sees the row.
        visible = _row_for(_inbox(client), issue["id"], "changed_status")
        assert visible["target_state"] == "closed"

        # The admin flips the project private without adding the reader.
        r = client.put(
            f"/projects/{pid}/visibility", json={"visibility": "private"}, headers=H1
        )
        assert r.status_code == 200, r.text

        rows = _inbox(client)
        assert not any(r["target_id"] == issue["id"] for r in rows)
        body = client.get("/notifications", headers=H2).text
        assert "closed" not in body and "secret" not in body
        assert (
            client.get("/notifications/unread_count", headers=H2).json()["count"] == 0
        )


# --- reads never write -------------------------------------------------------


def _notification_snapshot(db_file):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT id, user_id, event_id, read_at FROM notifications ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in rows]


def test_reads_leave_notifications_untouched_on_every_surface(tmp_path):
    app, db_file = _app(tmp_path, "reads.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        _watch_and_change(client, issue["id"], "done")
        before = _notification_snapshot(db_file)
        assert before, "need notifications for the check to mean anything"

        client.get("/notifications", headers=H2)
        client.get("/notifications?unread=true", headers=H2)
        client.get("/notifications/unread_count", headers=H2)

        assert _notification_snapshot(db_file) == before


def test_annotation_query_does_not_change_total_changes(tmp_path):
    app, db_file = _app(tmp_path, "totals.db")
    with TestClient(app) as client:
        _setup_users(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        _watch_and_change(client, issue["id"], "done")

    conn = db.connect(db_file)
    before = conn.total_changes
    rows = notifications.list_notifications(conn, 2)
    assert rows, "need rows for the check to mean anything"
    from athena.aegis import notification_state  # the module under test

    annotated = notification_state.annotate_inbox(conn, 2, rows)
    assert conn.total_changes == before
    assert any(r["target_state"] == "closed" for r in annotated)


# --- web and MCP surfaces ----------------------------------------------------


def test_web_inbox_shows_the_annotation_label(tmp_path):
    app, _ = _app(tmp_path, "web.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post(
            "/users",
            json={"email": "b@e.com", "name": "B", "password": "pw2"},
            headers=H1,
        )
        closed = client.post("/issues", json={"title": "web"}, headers=H1).json()
        _watch_and_change(client, closed["id"], "done")
        # Positive control: an in-progress target on the same page gets no label.
        open_issue = client.post("/issues", json={"title": "open"}, headers=H1).json()
        _watch_and_change(client, open_issue["id"], "in_progress")

    with TestClient(app) as reader:
        r = reader.post(
            "/login",
            data={"email": "b@e.com", "password": "pw2"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        page = reader.get("/inbox")
        assert page.status_code == 200
        assert page.text.count(escape("target closed")) == 1
        assert "target archived" not in page.text
        assert "you completed this" not in page.text


def test_mcp_list_notifications_carries_target_state(tmp_path):
    from athena.mcp.client import AthenaClient

    app, _ = _app(tmp_path, "mcp.db")
    with TestClient(app) as client:
        _setup_users(client)
        raw = client.post(
            "/tokens",
            json={"name": "mcp", "scopes": ["admin"]},
            headers=H2,
        ).json()["token"]
        issue = client.post("/issues", json={"title": "m"}, headers=H1).json()
        _watch_and_change(client, issue["id"], "done")

        client.headers.update({"Authorization": f"Bearer {raw}"})
        rows = AthenaClient(client=client).list_notifications()
        assert [r["target_state"] for r in rows if r["target_id"] == issue["id"]] == [
            "closed"
        ]
