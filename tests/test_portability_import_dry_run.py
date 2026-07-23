"""Dry-run validation for Athena portability imports."""

import json

from athena import ops
from athena.aegis import comments as issue_comments
from athena.aegis import issues, projects
from athena.core import access, activity, db, labels, portability
from athena.mentor import pages, spaces


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


def _attachment(conn, *, target_kind, target_id, uploaded_by, filename):
    conn.execute(
        "INSERT INTO attachments "
        "(target_kind, target_id, filename, content_type, byte_size, sha256, "
        "stored_name, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target_kind,
            target_id,
            filename,
            "text/plain",
            20,
            f"sha-{target_kind}-{target_id}",
            f"stored-{target_kind}-{target_id}",
            uploaded_by,
        ),
    )
    conn.commit()


def _codes(items):
    return {item["code"] for item in items}


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _project_bundle(tmp_path):
    conn = _connect(tmp_path / "source-project.db")
    owner = _user(conn, "owner@example.com", "Owner", role="admin")
    member = _user(conn, "member@example.com", "Member")
    ext_space = spaces.create_space(conn, key="EXT", name="External", created_by=owner)
    ext_page = pages.create_page(
        conn,
        space_id=ext_space["id"],
        title="External Page",
        body="outside",
        created_by=owner,
    )
    project = projects.create_project(
        conn,
        name="Portability",
        key="PORT",
        created_by=owner,
        description="selective export",
    )
    access.add_project_member(conn, project["id"], member, owner)
    issue = issues.create_issue(
        conn,
        title="Import me",
        body=f"See [[page:{ext_page['id']}]]",
        created_by=owner,
        project_id=project["id"],
    )
    issues.set_assignee(conn, issue["id"], member)
    issue_comments.add_comment(
        conn, issue_id=issue["id"], author_id=member, body="portable comment"
    )
    bug = labels.create_label(conn, name="bug", color="#ff0000")
    labels.add_label_to_issue(conn, issue["id"], bug["id"])
    _attachment(
        conn,
        target_kind="issue",
        target_id=issue["id"],
        uploaded_by=member,
        filename="evidence.txt",
    )
    activity.record(
        conn,
        actor_id=owner,
        verb="created",
        target_kind="project",
        target_id=project["id"],
    )
    bundle = portability.export_bundle(conn, "project", project["id"])
    conn.close()
    return bundle


def test_project_import_dry_run_ready_reuses_users_and_labels_without_writes(tmp_path):
    bundle = _project_bundle(tmp_path)
    target = _connect(tmp_path / "target-ready.db")
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    labels.create_label(target, name="bug", color="#ff0000")

    before = _count(target, "projects")
    report = portability.dry_run_import_bundle(target, bundle)
    after = _count(target, "projects")
    target.close()

    assert report["ok"] is True
    assert report["status"] == "ready"
    assert report["would_create"]["projects"] == 1
    assert report["would_create"]["issues"] == 1
    assert report["would_create"]["labels"] == 0
    assert report["would_reuse"] == {"users": 2, "labels": 1}
    assert _codes(report["conflicts"]) == set()
    assert _codes(report["warnings"]) == {
        "attachment_blobs_not_included",
        "external_cross_links_require_mapping",
    }
    assert before == after == 0


def test_project_import_dry_run_blocks_conflicts_and_missing_users(tmp_path):
    bundle = _project_bundle(tmp_path)
    target = _connect(tmp_path / "target-blocked.db")
    owner = _user(target, "owner@example.com", "Owner", role="admin")
    projects.create_project(
        target,
        name=bundle["project"]["name"],
        key=bundle["project"]["key"],
        created_by=owner,
    )

    report = portability.dry_run_import_bundle(target, bundle)
    target.close()

    assert report["ok"] is False
    assert report["status"] == "blocked"
    assert _codes(report["conflicts"]) == {
        "missing_user",
        "project_key_exists",
        "project_name_exists",
    }
    assert report["would_reuse"]["users"] == 1


def test_import_dry_run_cli_emits_json_report(tmp_path, capsys):
    bundle = _project_bundle(tmp_path)
    bundle_path = tmp_path / "project.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    target_path = tmp_path / "target-cli.db"
    target = _connect(target_path)
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    target.close()

    result = ops.import_dry_run_main([str(target_path), str(bundle_path), "--json"])
    out = capsys.readouterr()
    report = json.loads(out.out)

    assert result == 0
    assert report["ok"] is True
    assert report["kind"] == "project"
    assert report["would_create"]["projects"] == 1


def test_space_import_dry_run_reports_counts_and_key_conflict(tmp_path):
    source = _connect(tmp_path / "source-space.db")
    owner = _user(source, "owner@example.com", "Owner", role="admin")
    member = _user(source, "member@example.com", "Member")
    project = projects.create_project(source, name="Aegis", key="AEG", created_by=owner)
    issue = issues.create_issue(
        source,
        title="Linked issue",
        body="outside",
        created_by=owner,
        project_id=project["id"],
    )
    space = spaces.create_space(source, key="DOC", name="Docs", created_by=owner)
    access.add_space_member(source, space["id"], member, owner)
    page = pages.create_page(
        source,
        space_id=space["id"],
        title="Home",
        body=f"See [[issue:{issue['id']}]]",
        created_by=owner,
    )
    _attachment(
        source,
        target_kind="page",
        target_id=page["id"],
        uploaded_by=owner,
        filename="diagram.txt",
    )
    bundle = portability.export_bundle(source, "space", space["id"])
    source.close()

    target = _connect(tmp_path / "target-space.db")
    target_owner = _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    spaces.create_space(
        target, key="DOC", name="Existing Docs", created_by=target_owner
    )

    report = portability.dry_run_import_bundle(target, bundle)
    target.close()

    assert report["ok"] is False
    assert report["would_create"]["spaces"] == 1
    assert report["would_create"]["pages"] == 1
    assert report["would_reuse"]["users"] == 2
    assert _codes(report["conflicts"]) == {"space_key_exists"}
    assert _codes(report["warnings"]) == {
        "attachment_blobs_not_included",
        "external_cross_links_require_mapping",
    }
