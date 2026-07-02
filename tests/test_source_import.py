"""Source export mappers for Athena portability bundles."""

import json

from athena import ops
from athena.core import db, portability, source_import


def _connect(path):
    conn = db.connect(path)
    db.migrate(conn)
    return conn


def _user(conn, email, name, *, role="member"):
    cur = conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
        (email, name, role),
    )
    conn.commit()
    return cur.lastrowid


def _jira_payload():
    return {
        "issues": [
            {
                "id": "10001",
                "key": "MIG-1",
                "fields": {
                    "project": {"key": "MIG", "name": "Migration"},
                    "summary": "Parent issue",
                    "description": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Blocks MIG-2"}
                                ],
                            }
                        ],
                    },
                    "status": {
                        "name": "To Do",
                        "statusCategory": {"key": "new"},
                    },
                    "priority": {"name": "High"},
                    "reporter": {
                        "emailAddress": "owner@example.com",
                        "displayName": "Owner",
                    },
                    "assignee": {
                        "emailAddress": "member@example.com",
                        "displayName": "Member",
                    },
                    "created": "2025-01-01T00:00:00.000+0000",
                    "labels": ["bug", "backend"],
                    "comment": {
                        "comments": [
                            {
                                "author": {
                                    "emailAddress": "member@example.com",
                                    "displayName": "Member",
                                },
                                "body": "Portable comment",
                                "created": "2025-01-02T00:00:00.000+0000",
                            }
                        ]
                    },
                    "issuelinks": [
                        {
                            "type": {
                                "name": "Blocks",
                                "outward": "blocks",
                                "inward": "is blocked by",
                            },
                            "outwardIssue": {"key": "MIG-2"},
                        }
                    ],
                },
            },
            {
                "id": "10002",
                "key": "MIG-2",
                "fields": {
                    "project": {"key": "MIG", "name": "Migration"},
                    "summary": "Child issue",
                    "description": "Child body",
                    "status": {
                        "name": "In Progress",
                        "statusCategory": {"key": "indeterminate"},
                    },
                    "priority": {"name": "Low"},
                    "reporter": {
                        "emailAddress": "member@example.com",
                        "displayName": "Member",
                    },
                    "assignee": None,
                    "parent": {"key": "MIG-1"},
                    "created": "2025-01-03T00:00:00.000+0000",
                    "labels": ["bug"],
                },
            },
        ]
    }


def _confluence_payload():
    return {
        "results": [
            {
                "id": "100",
                "type": "page",
                "title": "Home",
                "space": {"key": "DOC", "name": "Docs"},
                "history": {
                    "createdBy": {
                        "email": "owner@example.com",
                        "displayName": "Owner",
                    },
                    "createdDate": "2025-02-01T00:00:00Z",
                },
                "version": {
                    "by": {
                        "email": "owner@example.com",
                        "displayName": "Owner",
                    },
                    "when": "2025-02-02T00:00:00Z",
                },
                "body": {"storage": {"value": "<p>See [[page:200]]</p>"}},
                "metadata": {"labels": {"results": [{"name": "runbook"}]}},
                "comments": [
                    {
                        "author": {
                            "email": "member@example.com",
                            "displayName": "Member",
                        },
                        "body": "<p>Looks good</p>",
                        "created_at": "2025-02-03T00:00:00Z",
                    }
                ],
            },
            {
                "id": "200",
                "type": "page",
                "title": "Child",
                "space": {"key": "DOC", "name": "Docs"},
                "ancestors": [{"id": "100"}],
                "history": {
                    "createdBy": {
                        "email": "member@example.com",
                        "displayName": "Member",
                    },
                    "createdDate": "2025-02-04T00:00:00Z",
                },
                "version": {
                    "by": {
                        "email": "member@example.com",
                        "displayName": "Member",
                    },
                    "when": "2025-02-05T00:00:00Z",
                },
                "body": {"storage": {"value": "<p>Child body</p>"}},
                "versions": [
                    {
                        "number": 1,
                        "title": "Child draft",
                        "body": "<p>Draft body</p>",
                        "by": {
                            "email": "member@example.com",
                            "displayName": "Member",
                        },
                        "when": "2025-02-04T12:00:00Z",
                    }
                ],
            },
        ]
    }


def test_jira_mapper_outputs_valid_project_bundle_and_imports(tmp_path):
    bundle = source_import.map_jira_project(_jira_payload())

    assert bundle["schema"] == portability.SCHEMA
    assert bundle["source"]["kind"] == "jira-project"
    assert bundle["project"]["key"] == "MIG"
    assert [issue["project_seq"] for issue in bundle["issues"]] == [1, 2]
    assert bundle["issues"][0]["body"] == "Blocks [[issue:2]]"
    assert bundle["issues"][1]["parent_id"] == 1
    assert {label["name"] for label in bundle["labels"]} == {"bug", "backend"}
    assert bundle["issue_links"] == [
        {
            "from_id": 1,
            "to_id": 2,
            "kind": "blocks",
            "created_by": 1,
            "created_at": "2025-01-01T00:00:00.000+0000",
        }
    ]
    assert bundle["cross_links"] == [
        {
            "source_kind": "issue",
            "source_id": 1,
            "target_kind": "issue",
            "target_id": 2,
        }
    ]

    target = _connect(tmp_path / "target-jira.db")
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    manifest = portability.build_import_manifest(target, bundle)
    result = portability.replay_import_manifest(target, bundle, manifest)

    imported = target.execute(
        "SELECT title, body, parent_id FROM issues ORDER BY project_seq"
    ).fetchall()
    target.close()

    assert manifest["ok"] is True
    assert result["status"] == "imported"
    assert [row["title"] for row in imported] == ["Parent issue", "Child issue"]
    assert imported[0]["body"] == "Blocks [[issue:2]]"
    assert imported[1]["parent_id"] == 1


def test_confluence_mapper_outputs_valid_space_bundle_and_imports(tmp_path):
    bundle = source_import.map_confluence_space(_confluence_payload())

    assert bundle["schema"] == portability.SCHEMA
    assert bundle["source"]["kind"] == "confluence-space"
    assert bundle["space"]["key"] == "DOC"
    assert bundle["pages"][0]["body"] == "See [[page:2]]"
    assert bundle["pages"][1]["parent_id"] == 1
    assert bundle["versions"][0]["body"] == "Draft body"
    assert bundle["comments"][0]["body"] == "Looks good"
    assert bundle["labels"] == [{"id": 1, "name": "runbook", "color": "#6b7280"}]
    assert bundle["cross_links"] == [
        {
            "source_kind": "page",
            "source_id": 1,
            "target_kind": "page",
            "target_id": 2,
        }
    ]

    target = _connect(tmp_path / "target-confluence.db")
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    manifest = portability.build_import_manifest(target, bundle)
    result = portability.replay_import_manifest(target, bundle, manifest)

    imported = target.execute("SELECT title, body, parent_id FROM pages ORDER BY id").fetchall()
    target.close()

    assert manifest["ok"] is True
    assert result["status"] == "imported"
    assert [row["title"] for row in imported] == ["Home", "Child"]
    assert imported[0]["body"] == "See [[page:2]]"
    assert imported[1]["parent_id"] == 1


def test_map_source_cli_writes_bundle_and_refuses_overwrite(tmp_path, capsys):
    source_path = tmp_path / "jira.json"
    bundle_path = tmp_path / "bundle.json"
    source_path.write_text(json.dumps(_jira_payload()), encoding="utf-8")

    result = ops.map_source_main(
        ["jira-project", str(source_path), str(bundle_path), "--project-name", "Import"]
    )
    out = capsys.readouterr()
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

    assert result == 0
    assert "Mapped jira-project export" in out.out
    assert bundle["project"]["name"] == "Import"

    result = ops.map_source_main(["jira-project", str(source_path), str(bundle_path)])
    out = capsys.readouterr()
    assert result == 1
    assert "bundle path already exists" in out.err
