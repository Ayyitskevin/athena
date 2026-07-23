"""Source export mappers for Athena portability bundles."""

import json
from pathlib import Path

from athena import ops
from athena.core import db, portability, source_import

FIXTURES = Path(__file__).parent / "fixtures" / "source_import"


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


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
                                "content": [{"type": "text", "text": "Blocks MIG-2"}],
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


def test_jira_mapper_handles_cloud_export_and_reports_unmapped_fields(tmp_path):
    bundle = source_import.map_jira_project(_fixture("jira_cloud_search.json"))
    report = source_import.source_mapping_report(bundle)

    assert bundle["project"]["key"] == "OPS"
    assert [issue["title"] for issue in bundle["issues"]] == [
        "Prepare migration runbook",
        "Verify deployment checklist",
    ]
    assert bundle["issues"][0]["body"].startswith(
        "Coordinate with [[issue:2]] before cutover."
    )
    assert "Back up SQLite database" in bundle["issues"][0]["body"]
    assert bundle["issues"][0]["assignee_id"] == 2
    assert bundle["users"][1]["email"] == "557058-agent@import.local"
    assert bundle["comments"][0]["body"] == "Looks ready."
    assert bundle["issue_links"] == [
        {
            "from_id": 1,
            "to_id": 2,
            "kind": "blocks",
            "created_by": 1,
            "created_at": "2026-01-01T12:00:00.000+0000",
        }
    ]
    assert bundle["issues"][1]["parent_id"] is None

    assert report["schema"] == source_import.SOURCE_MAPPING_REPORT_SCHEMA
    assert report["status"] == "mapped_with_gaps"
    assert report["counts"]["issues"] == 2
    unmapped = {row["path"]: row for row in report["unmapped_fields"]}
    assert unmapped["issues[].fields.customfield_10020"]["count"] == 1
    assert unmapped["issues[].fields.attachment"]["reason"].startswith("raw attachment")
    assert unmapped["issues[].fields.issuelinks"]["count"] == 1
    assert unmapped["issues[].fields.parent"]["count"] == 1

    target = _connect(tmp_path / "target-jira-cloud.db")
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "557058-agent@import.local", "Migration Agent")
    _user(target, "reviewer@example.com", "Reviewer")
    manifest = portability.build_import_manifest(target, bundle)
    result = portability.replay_import_manifest(target, bundle, manifest)
    target.close()

    assert manifest["ok"] is True
    assert result["status"] == "imported"


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

    imported = target.execute(
        "SELECT title, body, parent_id FROM pages ORDER BY id"
    ).fetchall()
    target.close()

    assert manifest["ok"] is True
    assert result["status"] == "imported"
    assert [row["title"] for row in imported] == ["Home", "Child"]
    assert imported[0]["body"] == "See [[page:2]]"
    assert imported[1]["parent_id"] == 1


def test_confluence_mapper_handles_cloud_export_and_reports_unmapped_fields(tmp_path):
    bundle = source_import.map_confluence_space(
        _fixture("confluence_cloud_content.json")
    )
    report = source_import.source_mapping_report(bundle)

    assert bundle["space"]["key"] == "OPS"
    assert bundle["pages"][0]["title"] == "Migration Home"
    assert bundle["pages"][0]["body"] == "See .\n[[page:2]]"
    assert bundle["pages"][1]["parent_id"] == 1
    assert bundle["versions"][0]["body"] == "Initial checklist."
    assert bundle["comments"][0]["body"] == "Good operating note."
    assert bundle["cross_links"] == [
        {
            "source_kind": "page",
            "source_id": 1,
            "target_kind": "page",
            "target_id": 2,
        }
    ]

    assert report["status"] == "mapped_with_gaps"
    assert report["counts"]["pages"] == 2
    unmapped = {row["path"]: row for row in report["unmapped_fields"]}
    assert unmapped["pages[].restrictions"]["count"] == 1
    assert unmapped["pages[].extensions"]["count"] == 1
    assert unmapped["pages[].body.storage.links"]["count"] == 1

    target = _connect(tmp_path / "target-confluence-cloud.db")
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "editor@example.com", "Editor")
    _user(target, "reviewer@example.com", "Reviewer")
    manifest = portability.build_import_manifest(target, bundle)
    result = portability.replay_import_manifest(target, bundle, manifest)
    target.close()

    assert manifest["ok"] is True
    assert result["status"] == "imported"


def _confluence_page(page_id, title, **extra):
    return {
        "id": page_id,
        "type": "page",
        "title": title,
        "space": {"key": "OPS", "name": "Ops"},
        "body": {"storage": {"value": "<p>body</p>"}},
        **extra,
    }


def test_confluence_mapper_preserves_code_macro_cdata_content():
    # WHY: Confluence wraps code/noformat macro bodies in <![CDATA[...]]>; HTMLParser used
    # to drop CDATA entirely, so runbook code blocks vanished silently while the report
    # still said "mapped". The content must survive import.
    macro = (
        "<p>Run this:</p>"
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
        "<![CDATA[systemctl restart athena]]>"
        "</ac:plain-text-body></ac:structured-macro>"
    )
    bundle = source_import.map_confluence_space(
        {
            "results": [
                _confluence_page("1", "Runbook", body={"storage": {"value": macro}})
            ]
        }
    )
    assert "systemctl restart athena" in bundle["pages"][0]["body"]


def test_confluence_mapper_skips_non_object_rows_without_crashing():
    # WHY: a JSON null / non-object in results used to crash the whole map (AttributeError
    # in the id comprehension / space derivation). It must skip-and-report instead —
    # including when the bad row is first.
    bundle = source_import.map_confluence_space(
        {"results": [None, _confluence_page("1", "Home")]}
    )
    report = source_import.source_mapping_report(bundle)
    assert [p["title"] for p in bundle["pages"]] == ["Home"]
    assert any(
        u["path"] == "pages[]" and "non-object" in u["reason"]
        for u in report["unmapped_fields"]
    )


def test_confluence_mapper_breaks_parent_cycles():
    # WHY: a buggy export can list two pages as each other's parent; imported verbatim,
    # mentor's validate_move walks the parent chain forever and hangs the worker. The
    # mapper must emit an acyclic tree.
    bundle = source_import.map_confluence_space(
        {
            "results": [
                _confluence_page("1", "A", ancestors=[{"id": "2"}]),
                _confluence_page("2", "B", ancestors=[{"id": "1"}]),
            ]
        }
    )
    parents = {p["id"]: p["parent_id"] for p in bundle["pages"]}
    assert not (parents[1] == 2 and parents[2] == 1)  # the mutual edge is broken
    for start in parents:  # every chain terminates (acyclic)
        seen, node = set(), start
        while node is not None:
            assert node not in seen
            seen.add(node)
            node = parents.get(node)
    report = source_import.source_mapping_report(bundle)
    assert any("cyclic" in u["reason"] for u in report["unmapped_fields"])


def test_jira_mapper_scopes_to_one_project_and_skips_foreign_issues():
    # WHY: a multi-project Jira export (WEB-3, API-3) used to lump both into one project
    # reusing the raw key number as project_seq → two issues both keyed WEB-3, so [[WEB-3]]
    # resolved nondeterministically. The mapper must scope to one project.
    payload = {
        "issues": [
            {
                "id": "1",
                "key": "WEB-3",
                "fields": {
                    "project": {"key": "WEB", "name": "Web"},
                    "summary": "web issue",
                    "status": {"name": "To Do"},
                    "reporter": {"emailAddress": "o@e.com", "displayName": "O"},
                },
            },
            {
                "id": "2",
                "key": "API-3",
                "fields": {
                    "project": {"key": "API", "name": "Api"},
                    "summary": "api issue",
                    "status": {"name": "To Do"},
                    "reporter": {"emailAddress": "o@e.com", "displayName": "O"},
                },
            },
        ]
    }
    bundle = source_import.map_jira_project(payload)
    report = source_import.source_mapping_report(bundle)
    assert bundle["project"]["key"] == "WEB"
    assert [i["title"] for i in bundle["issues"]] == ["web issue"]
    assert [i["project_seq"] for i in bundle["issues"]] == [3]  # no collision
    assert any(
        u["path"] == "issues[].fields.project" and "different project" in u["reason"]
        for u in report["unmapped_fields"]
    )


def test_jira_mapper_canonicalizes_issue_status_casing(tmp_path):
    # WHY: two issues with the same status in different casing ("Done"/"done") used to
    # store their raw casing while the deduped status row kept the first — so the second
    # issue's status didn't match any row under Athena's case-sensitive category lookup,
    # and a closed issue read as open (is_done False).
    from athena.aegis import statuses as aegis_statuses

    def _issue(iid, key, casing):
        return {
            "id": iid,
            "key": key,
            "fields": {
                "project": {"key": "OPS", "name": "Ops"},
                "summary": key,
                "status": {"name": casing, "statusCategory": {"key": "done"}},
                "reporter": {"emailAddress": "o@e.com", "displayName": "O"},
                "created": "2025-01-01T00:00:00.000+0000",
            },
        }

    bundle = source_import.map_jira_project(
        {"issues": [_issue("1", "OPS-1", "Done"), _issue("2", "OPS-2", "done")]}
    )
    assert [s["name"] for s in bundle["statuses"]] == ["Done"]  # one row, deduped
    assert [i["status"] for i in bundle["issues"]] == ["Done", "Done"]  # both canonical

    target = _connect(tmp_path / "casing.db")
    _user(target, "o@e.com", "O", role="admin")
    manifest = portability.build_import_manifest(target, bundle)
    portability.replay_import_manifest(target, bundle, manifest)
    rows = target.execute("SELECT status, project_id FROM issues").fetchall()
    # The runtime consequence: BOTH imported issues resolve as done.
    assert all(
        aegis_statuses.is_done(target, r["project_id"], r["status"]) for r in rows
    )
    target.close()


def test_map_source_cli_writes_bundle_and_refuses_overwrite(tmp_path, capsys):
    source_path = tmp_path / "jira.json"
    bundle_path = tmp_path / "bundle.json"
    report_path = tmp_path / "report.json"
    source_path.write_text(json.dumps(_jira_payload()), encoding="utf-8")

    result = ops.map_source_main(
        [
            "jira-project",
            str(source_path),
            str(bundle_path),
            "--project-name",
            "Import",
            "--report-path",
            str(report_path),
        ]
    )
    out = capsys.readouterr()
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert result == 0
    assert "Mapped jira-project export" in out.out
    assert "Wrote source mapping report" in out.out
    assert bundle["project"]["name"] == "Import"
    assert report["schema"] == source_import.SOURCE_MAPPING_REPORT_SCHEMA

    result = ops.map_source_main(["jira-project", str(source_path), str(bundle_path)])
    out = capsys.readouterr()
    assert result == 1
    assert "bundle path already exists" in out.err
