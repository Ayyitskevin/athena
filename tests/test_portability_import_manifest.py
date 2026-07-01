"""Replay manifest generation for Athena portability imports."""

import json

from athena import ops
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
            32,
            f"sha-{target_kind}-{target_id}",
            f"blob-{target_kind}-{target_id}",
            uploaded_by,
        ),
    )
    conn.commit()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _codes(items):
    return {item["code"] for item in items}


def _project_bundle(tmp_path):
    conn = _connect(tmp_path / "source-project.db")
    owner = _user(conn, "owner@example.com", "Owner", role="admin")
    member = _user(conn, "member@example.com", "Member")
    project = projects.create_project(
        conn,
        name="Replay",
        key="RPL",
        created_by=owner,
        description="manifest source",
    )
    access.add_project_member(conn, project["id"], member, owner)
    issue = issues.create_issue(
        conn,
        title="Replay me",
        body="body",
        created_by=owner,
        project_id=project["id"],
    )
    issues.set_assignee(conn, issue["id"], member)
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


def test_project_import_manifest_maps_reused_ids_and_create_refs(tmp_path):
    bundle = _project_bundle(tmp_path)
    target = _connect(tmp_path / "target-project.db")
    owner = _user(target, "owner@example.com", "Owner", role="admin")
    member = _user(target, "member@example.com", "Member")
    bug = labels.create_label(target, name="bug", color="#ff0000")

    before = _count(target, "projects")
    manifest = portability.build_import_manifest(target, bundle)
    after = _count(target, "projects")
    target.close()

    assert manifest["schema"] == portability.MANIFEST_SCHEMA
    assert manifest["ok"] is True
    assert manifest["status"] == "ready"
    assert manifest["attachment_policy"] == {
        "mode": "skip",
        "status": "ready",
        "attachment_count": 1,
        "replay_action": "skip",
    }
    assert manifest["id_map"]["project"] == {
        "source_id": bundle["project"]["id"],
        "action": "create",
        "target_ref": f"project:{bundle['project']['id']}",
    }
    assert manifest["id_map"]["users"] == [
        {
            "source_id": bundle["users"][0]["id"],
            "source_email": "owner@example.com",
            "action": "reuse",
            "target_id": owner,
        },
        {
            "source_id": bundle["users"][1]["id"],
            "source_email": "member@example.com",
            "action": "reuse",
            "target_id": member,
        },
    ]
    assert manifest["id_map"]["labels"][0]["target_id"] == bug["id"]
    assert [op["op"] for op in manifest["operations"]][:4] == [
        "map_users",
        "map_labels",
        "create_labels",
        "create_project",
    ]
    assert manifest["operations"][-1] == {
        "op": "apply_attachment_policy",
        "policy": "skip",
        "action": "skip",
        "count": 1,
    }
    assert before == after == 0


def test_import_manifest_require_blobs_blocks_attachment_bundles(tmp_path):
    bundle = _project_bundle(tmp_path)
    target = _connect(tmp_path / "target-require-blobs.db")
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")

    manifest = portability.build_import_manifest(
        target,
        bundle,
        attachment_policy="require-blobs",
    )
    target.close()

    assert manifest["ok"] is False
    assert manifest["status"] == "blocked"
    assert manifest["attachment_policy"]["status"] == "blocked"
    assert _codes(manifest["conflicts"]) == {"attachment_blobs_required"}


def test_import_manifest_cli_writes_json_and_refuses_overwrite(tmp_path, capsys):
    bundle = _project_bundle(tmp_path)
    bundle_path = tmp_path / "project.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    manifest_path = tmp_path / "project.manifest.json"
    target_path = tmp_path / "target-cli.db"
    target = _connect(target_path)
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    target.close()

    result = ops.import_manifest_main(
        [str(target_path), str(bundle_path), str(manifest_path)]
    )
    out = capsys.readouterr()
    manifest = json.loads(manifest_path.read_text())

    assert result == 0
    assert "ready" in out.out
    assert manifest["schema"] == portability.MANIFEST_SCHEMA
    assert manifest["operations"][-1]["action"] == "skip"

    result = ops.import_manifest_main(
        [str(target_path), str(bundle_path), str(manifest_path)]
    )
    out = capsys.readouterr()
    assert result == 1
    assert "manifest path already exists" in out.err


def test_space_import_manifest_carries_page_refs_and_space_operation(tmp_path):
    source = _connect(tmp_path / "source-space.db")
    owner = _user(source, "owner@example.com", "Owner", role="admin")
    member = _user(source, "member@example.com", "Member")
    space = spaces.create_space(source, key="DOC", name="Docs", created_by=owner)
    access.add_space_member(source, space["id"], member, owner)
    page = pages.create_page(
        source,
        space_id=space["id"],
        title="Home",
        body="body",
        created_by=owner,
    )
    bundle = portability.export_bundle(source, "space", space["id"])
    source.close()

    target = _connect(tmp_path / "target-space.db")
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    manifest = portability.build_import_manifest(target, bundle)
    target.close()

    assert manifest["ok"] is True
    assert manifest["id_map"]["space"]["target_ref"] == f"space:{space['id']}"
    assert manifest["id_map"]["pages"] == [
        {
            "source_id": page["id"],
            "action": "create",
            "target_ref": f"page:{page['id']}",
        }
    ]
    assert [op["op"] for op in manifest["operations"]][:4] == [
        "map_users",
        "map_labels",
        "create_labels",
        "create_space",
    ]
