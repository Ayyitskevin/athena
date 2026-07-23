"""Selective JSON export bundles for Athena portability."""

import json

from athena import ops
from athena.aegis import comments as issue_comments
from athena.aegis import issues, projects
from athena.core import access, activity, db, labels, portability
from athena.mentor import page_comments, pages, spaces


def _connect(path):
    conn = db.connect(path)
    db.migrate(conn)
    return conn


def _user(conn, email, name, *, role="member", is_agent=False):
    cur = conn.execute(
        "INSERT INTO users (email, name, role, is_agent) VALUES (?, ?, ?, ?)",
        (email, name, role, 1 if is_agent else 0),
    )
    conn.commit()
    return cur.lastrowid


def _attachment(conn, *, target_kind, target_id, uploaded_by, filename="spec.txt"):
    conn.execute(
        "INSERT INTO attachments "
        "(target_kind, target_id, filename, content_type, byte_size, sha256, "
        "stored_name, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target_kind,
            target_id,
            filename,
            "text/plain",
            12,
            f"sha-{target_kind}-{target_id}",
            f"random-{target_kind}-{target_id}",
            uploaded_by,
        ),
    )
    conn.commit()


def _ids(rows):
    return [row["id"] for row in rows]


def test_project_export_bundle_captures_project_content_without_secrets(tmp_path):
    conn = _connect(tmp_path / "project.db")
    owner = _user(conn, "owner@example.com", "Owner", role="admin")
    member = _user(conn, "member@example.com", "Member")
    bot = _user(conn, "bot@example.com", "Bot", is_agent=True)

    ext_space = spaces.create_space(conn, key="EXT", name="External", created_by=owner)
    ext_page = pages.create_page(
        conn,
        space_id=ext_space["id"],
        title="Outside Page",
        body="outside",
        created_by=owner,
    )
    project = projects.create_project(
        conn,
        name="Portability",
        key="PORT",
        description="Export me",
        created_by=owner,
    )
    access.add_project_member(conn, project["id"], member, owner)
    conn.execute(
        "INSERT INTO sprints (project_id, name, goal, state) VALUES (?, ?, ?, ?)",
        (project["id"], "Sprint 1", "ship export", "planned"),
    )
    conn.commit()

    issue_a = issues.create_issue(
        conn,
        title="Parent",
        body=f"See [[page:{ext_page['id']}]]",
        created_by=owner,
        project_id=project["id"],
    )
    issue_b = issues.create_issue(
        conn,
        title="Child",
        body="child body",
        created_by=member,
        project_id=project["id"],
    )
    issues.set_assignee(conn, issue_a["id"], member)
    issues.set_parent(conn, issue_b["id"], issue_a["id"])
    issue_comments.add_comment(
        conn, issue_id=issue_a["id"], author_id=member, body="comment"
    )
    conn.execute(
        "INSERT INTO issue_contributors (issue_id, user_id, added_by) VALUES (?, ?, ?)",
        (issue_a["id"], bot, owner),
    )
    conn.execute(
        "INSERT INTO issue_links (from_id, to_id, kind, created_by) "
        "VALUES (?, ?, ?, ?)",
        (issue_a["id"], issue_b["id"], "blocks", owner),
    )
    conn.commit()
    bug = labels.create_label(conn, name="bug", color="#ff0000")
    labels.add_label_to_issue(conn, issue_a["id"], bug["id"])
    _attachment(
        conn,
        target_kind="issue",
        target_id=issue_a["id"],
        uploaded_by=member,
        filename="project-spec.txt",
    )
    activity.record(
        conn,
        actor_id=owner,
        verb="created",
        target_kind="project",
        target_id=project["id"],
    )
    activity.record(
        conn,
        actor_id=bot,
        verb="triaged",
        target_kind="issue",
        target_id=issue_a["id"],
    )

    bundle = portability.export_bundle(conn, "project", project["id"])
    conn.close()

    assert bundle["schema"] == portability.SCHEMA
    assert bundle["schema_version"] == 1
    assert bundle["kind"] == "project"
    assert bundle["project"]["key"] == "PORT"
    assert _ids(bundle["issues"]) == [issue_a["id"], issue_b["id"]]
    assert bundle["comments"][0]["body"] == "comment"
    assert bundle["contributors"] == [
        {
            "issue_id": issue_a["id"],
            "user_id": bot,
            "added_by": owner,
            "added_at": bundle["contributors"][0]["added_at"],
        }
    ]
    assert bundle["issue_links"][0]["kind"] == "blocks"
    assert bundle["labels"] == [{"id": bug["id"], "name": "bug", "color": "#ff0000"}]
    assert bundle["label_links"] == [{"issue_id": issue_a["id"], "label_id": bug["id"]}]
    assert bundle["attachments"][0]["filename"] == "project-spec.txt"
    assert "stored_name" not in bundle["attachments"][0]
    assert bundle["cross_links"] == [
        {
            "source_kind": "issue",
            "source_id": issue_a["id"],
            "target_kind": "page",
            "target_id": ext_page["id"],
        }
    ]
    assert [row["verb"] for row in bundle["activity"]] == ["created", "triaged"]
    users = {row["email"]: row for row in bundle["users"]}
    assert users["bot@example.com"]["is_agent"] is True
    assert "password_hash" not in users["owner@example.com"]


def test_space_export_bundle_captures_pages_versions_and_page_metadata(tmp_path):
    conn = _connect(tmp_path / "space.db")
    owner = _user(conn, "owner@example.com", "Owner", role="admin")
    member = _user(conn, "member@example.com", "Member")

    project = projects.create_project(conn, name="Aegis", key="AEG", created_by=owner)
    issue = issues.create_issue(
        conn,
        title="Linked issue",
        body="issue",
        created_by=owner,
        project_id=project["id"],
    )
    space = spaces.create_space(
        conn,
        key="DOC",
        name="Docs",
        description="Export docs",
        created_by=owner,
    )
    access.add_space_member(conn, space["id"], member, owner)
    parent = pages.create_page(
        conn,
        space_id=space["id"],
        title="Parent",
        body="first body",
        created_by=owner,
    )
    child = pages.create_page(
        conn,
        space_id=space["id"],
        parent_id=parent["id"],
        title="Child",
        body=f"See [[issue:{issue['id']}]]",
        created_by=member,
    )
    pages.update_page(
        conn,
        parent["id"],
        editor_id=member,
        title="Parent v2",
        body="second body",
    )
    page_comments.add_comment(
        conn, page_id=child["id"], author_id=owner, body="page comment"
    )
    kb = labels.create_label(conn, name="kb", color="#00ff00")
    labels.add_label_to_page(conn, child["id"], kb["id"])
    _attachment(
        conn,
        target_kind="page",
        target_id=child["id"],
        uploaded_by=owner,
        filename="diagram.txt",
    )
    activity.record(
        conn,
        actor_id=owner,
        verb="created",
        target_kind="space",
        target_id=space["id"],
    )
    activity.record(
        conn,
        actor_id=member,
        verb="edited",
        target_kind="page",
        target_id=parent["id"],
    )

    bundle = portability.export_bundle(conn, "space", space["id"])
    conn.close()

    assert bundle["kind"] == "space"
    assert bundle["space"]["key"] == "DOC"
    assert _ids(bundle["pages"]) == [parent["id"], child["id"]]
    assert bundle["versions"][0]["page_id"] == parent["id"]
    assert bundle["versions"][0]["body"] == "first body"
    assert bundle["comments"][0]["body"] == "page comment"
    assert bundle["labels"] == [{"id": kb["id"], "name": "kb", "color": "#00ff00"}]
    assert bundle["label_links"] == [{"page_id": child["id"], "label_id": kb["id"]}]
    assert bundle["attachments"][0]["filename"] == "diagram.txt"
    assert "stored_name" not in bundle["attachments"][0]
    assert bundle["cross_links"] == [
        {
            "source_kind": "page",
            "source_id": child["id"],
            "target_kind": "issue",
            "target_id": issue["id"],
        }
    ]
    assert [row["verb"] for row in bundle["activity"]] == ["created", "edited"]
    assert {row["email"] for row in bundle["users"]} == {
        "owner@example.com",
        "member@example.com",
    }


def test_export_cli_writes_json_and_refuses_overwrite(tmp_path, capsys):
    db_file = tmp_path / "cli.db"
    conn = _connect(db_file)
    owner = _user(conn, "owner@example.com", "Owner")
    project = projects.create_project(conn, name="CLI", key="CLI", created_by=owner)
    conn.close()
    bundle_path = tmp_path / "exports" / "project.json"

    assert (
        ops.export_main([str(db_file), "project", str(project["id"]), str(bundle_path)])
        == 0
    )
    out = capsys.readouterr()
    assert "Exported project" in out.out
    bundle = json.loads(bundle_path.read_text())
    assert bundle["project"]["name"] == "CLI"

    assert (
        ops.export_main([str(db_file), "project", str(project["id"]), str(bundle_path)])
        == 1
    )
    out = capsys.readouterr()
    assert "export path already exists" in out.err

    assert (
        ops.export_main(
            [
                str(db_file),
                "project",
                str(project["id"]),
                str(bundle_path),
                "--overwrite",
            ]
        )
        == 0
    )


def test_export_rejects_missing_container(tmp_path):
    conn = _connect(tmp_path / "missing.db")
    try:
        try:
            portability.export_bundle(conn, "project", 999)
        except ValueError as exc:
            assert "no such project: 999" in str(exc)
        else:
            raise AssertionError("expected missing project to fail")
    finally:
        conn.close()


def test_export_bundle_uses_one_read_snapshot(tmp_path, monkeypatch):
    db_file = tmp_path / "snapshot.db"
    reader = _connect(db_file)
    owner = _user(reader, "snapshot@example.com", "Snapshot")
    project = projects.create_project(
        reader, name="Snapshot", key="SNP", created_by=owner
    )
    before = issues.create_issue(
        reader,
        title="Before snapshot",
        body="before",
        created_by=owner,
        project_id=project["id"],
    )
    writer = db.connect(db_file)
    real_rows = portability._rows
    inserted = False

    def _interleave(conn, sql, params=()):
        nonlocal inserted
        if not inserted and "FROM issues WHERE project_id" in sql:
            inserted = True
            issues.create_issue(
                writer,
                title="After snapshot",
                body="after",
                created_by=owner,
                project_id=project["id"],
            )
        return real_rows(conn, sql, params)

    monkeypatch.setattr(portability, "_rows", _interleave)
    try:
        bundle = portability.export_bundle(reader, "project", project["id"])
    finally:
        writer.close()
        reader.close()

    assert inserted is True
    assert [row["id"] for row in bundle["issues"]] == [before["id"]]
    assert bundle["project"]["issue_counter"] == 1
