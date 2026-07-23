"""Mutating selective import replay for Athena portability bundles."""

import json

from athena import ops
from athena.aegis import comments as issue_comments
from athena.aegis import issues, projects, sprints
from athena.core import access, activity, db, labels, portability, search
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


def _project_bundle(tmp_path):
    source = _connect(tmp_path / "source-project.db")
    owner = _user(source, "owner@example.com", "Owner", role="admin")
    member = _user(source, "member@example.com", "Member")
    bot = _user(source, "bot@example.com", "Bot", is_agent=True)
    ext_space = spaces.create_space(
        source, key="EXT", name="External", created_by=owner
    )
    ext_page = pages.create_page(
        source,
        space_id=ext_space["id"],
        title="External",
        body="external",
        created_by=owner,
    )
    project = projects.create_project(
        source,
        name="Replay",
        key="RPL",
        created_by=owner,
        description="import source",
    )
    access.add_project_member(source, project["id"], member, owner)
    sprint = sprints.create_sprint(source, project_id=project["id"], name="Launch")
    parent = issues.create_issue(
        source,
        title="Parent",
        body="placeholder",
        created_by=owner,
        project_id=project["id"],
    )
    child = issues.create_issue(
        source,
        title="Child",
        body="child body",
        created_by=member,
        project_id=project["id"],
    )
    issues.update_issue(
        source,
        parent["id"],
        body=f"See [[issue:{child['id']}]] and [[page:{ext_page['id']}]]",
    )
    issues.set_assignee(source, parent["id"], member)
    issues.set_parent(source, child["id"], parent["id"])
    issues.set_sprint(source, child["id"], sprint["id"])
    issue_comments.add_comment(
        source, issue_id=parent["id"], author_id=member, body="portable comment"
    )
    source.execute(
        "INSERT INTO issue_contributors (issue_id, user_id, added_by) VALUES (?, ?, ?)",
        (parent["id"], bot, owner),
    )
    source.execute(
        "INSERT INTO issue_links (from_id, to_id, kind, created_by) "
        "VALUES (?, ?, ?, ?)",
        (parent["id"], child["id"], "blocks", owner),
    )
    source.commit()
    bug = labels.create_label(source, name="bug", color="#ff0000")
    labels.add_label_to_issue(source, parent["id"], bug["id"])
    _attachment(
        source,
        target_kind="issue",
        target_id=parent["id"],
        uploaded_by=member,
        filename="evidence.txt",
    )
    root_event = activity.record(
        source,
        actor_id=owner,
        verb="created",
        target_kind="project",
        target_id=project["id"],
    )
    source.execute(
        "INSERT INTO activity "
        "(actor_id, verb, target_kind, target_id, detail, run_id, parent_run_id, "
        "forked_from_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bot,
            "triaged",
            "issue",
            parent["id"],
            "agent fork",
            "child-run",
            "parent-run",
            root_event["id"],
        ),
    )
    source.commit()
    bundle = portability.export_bundle(source, "project", project["id"])
    source.close()
    return bundle


def _prepare_project_target(path):
    target = _connect(path)
    extra = _user(target, "extra@example.com", "Extra")
    existing = projects.create_project(
        target, name="Existing", key="EX", created_by=extra
    )
    issues.create_issue(
        target,
        title="Existing issue",
        body="target id shifter",
        created_by=extra,
        project_id=existing["id"],
    )
    owner = _user(target, "owner@example.com", "Owner", role="admin")
    member = _user(target, "member@example.com", "Member")
    bot = _user(target, "bot@example.com", "Bot", is_agent=True)
    labels.create_label(target, name="bug", color="#ff0000")
    return target, owner, member, bot


def test_project_import_allocates_past_deleted_reserved_ids(tmp_path):
    # A bare SQLite insert would recycle the deleted highest id and trip the
    # reservation trigger. Import must allocate from the same monotonic sequence as
    # native creates so portability remains usable after deletions.
    bundle = _project_bundle(tmp_path)
    target = _connect(tmp_path / "target-after-delete.db")
    extra = _user(target, "extra@example.com", "Extra")
    deleted = projects.create_project(
        target, name="Deleted", key="DEL", created_by=extra
    )
    deleted_issue = issues.create_issue(
        target,
        title="Deleted highest issue",
        body="",
        created_by=extra,
    )
    target.execute("DELETE FROM issues WHERE id = ?", (deleted_issue["id"],))
    target.commit()
    assert projects.delete_project(target, deleted["id"])

    owner = _user(target, "owner@example.com", "Owner", role="admin")
    member = _user(target, "member@example.com", "Member")
    bot = _user(target, "bot@example.com", "Bot", is_agent=True)
    labels.create_label(target, name="bug", color="#ff0000")
    manifest = portability.build_import_manifest(target, bundle)

    result = portability.replay_import_manifest(target, bundle, manifest)

    imported = target.execute("SELECT id FROM projects WHERE key = 'RPL'").fetchone()
    assert result["status"] == "imported"
    imported_issue_ids = {row["target_id"] for row in result["id_map"]["issues"]}
    assert min(imported_issue_ids) > deleted_issue["id"]
    assert deleted_issue["id"] not in imported_issue_ids
    assert imported["id"] > deleted["id"]
    assert result["reused"]["users"] == 3
    assert {owner, member, bot} == {
        row["target_id"] for row in result["id_map"]["users"]
    }
    target.close()


def test_project_import_replays_manifest_and_rewrites_internal_refs(tmp_path):
    bundle = _project_bundle(tmp_path)
    target, owner, member, bot = _prepare_project_target(tmp_path / "target.db")
    manifest = portability.build_import_manifest(target, bundle)

    result = portability.replay_import_manifest(target, bundle, manifest)

    imported_project = target.execute(
        "SELECT * FROM projects WHERE key = 'RPL'"
    ).fetchone()
    imported_issues = target.execute(
        "SELECT * FROM issues WHERE project_id = ? ORDER BY project_seq",
        (imported_project["id"],),
    ).fetchall()
    parent, child = imported_issues
    target.close()

    assert result["schema"] == portability.IMPORT_RESULT_SCHEMA
    assert result["status"] == "imported"
    assert result["created"]["projects"] == 1
    assert result["created"]["issues"] == 2
    assert result["created"]["issue_links"] == 1
    assert result["created"]["labels"] == 0
    assert result["reused"] == {"users": 3, "labels": 1}
    assert result["skipped"]["attachments"] == 1
    assert result["skipped"]["external_cross_links"] == 1
    assert result["id_map"]["users"] == [
        {"source_id": bundle["users"][0]["id"], "target_id": owner},
        {"source_id": bundle["users"][1]["id"], "target_id": member},
        {"source_id": bundle["users"][2]["id"], "target_id": bot},
    ]
    external_page_id = next(
        row["target_id"]
        for row in bundle["cross_links"]
        if row["target_kind"] == "page"
    )
    assert parent["id"] != bundle["issues"][0]["id"]
    assert f"[[issue:{child['id']}]]" in parent["body"]
    assert f"[[issue:{bundle['issues'][1]['id']}]]" not in parent["body"]
    assert f"[[page:{external_page_id}]]" in parent["body"]
    assert child["parent_id"] == parent["id"]
    assert child["sprint_id"] is not None


def test_project_import_rebuilds_links_activity_and_search(tmp_path):
    bundle = _project_bundle(tmp_path)
    target, *_ = _prepare_project_target(tmp_path / "target-search.db")
    manifest = portability.build_import_manifest(target, bundle)
    result = portability.replay_import_manifest(target, bundle, manifest)
    issue_map = {
        row["source_id"]: row["target_id"] for row in result["id_map"]["issues"]
    }
    activity_map = {
        row["source_id"]: row["target_id"] for row in result["id_map"]["activity"]
    }
    parent_id = issue_map[bundle["issues"][0]["id"]]
    child_id = issue_map[bundle["issues"][1]["id"]]
    source_triaged = next(row for row in bundle["activity"] if row["verb"] == "triaged")
    # The bundle's triaged event carries run_id/parent_run_id/forked_from_event_id (see the
    # raw INSERT in _project_bundle) — a forged fork that import must NOT honor.
    assert source_triaged["run_id"] and source_triaged["forked_from_event_id"]

    links_rows = target.execute(
        "SELECT source_kind, source_id, target_kind, target_id FROM links "
        "WHERE source_id = ?",
        (parent_id,),
    ).fetchall()
    dependency = target.execute(
        "SELECT * FROM issue_links WHERE from_id = ? AND to_id = ?",
        (parent_id, child_id),
    ).fetchone()
    activity_rows = target.execute(
        "SELECT id, verb, target_kind, target_id, forked_from_event_id FROM activity "
        "WHERE target_kind IN ('project', 'issue') ORDER BY id DESC LIMIT 2"
    ).fetchall()
    imported_triaged = target.execute(
        "SELECT run_id, parent_run_id, forked_from_event_id, imported_at "
        "FROM activity WHERE id = ?",
        (activity_map[source_triaged["id"]],),
    ).fetchone()
    hits = search.search(target, "Parent")
    target.close()

    assert [dict(row) for row in links_rows] == [
        {
            "source_kind": "issue",
            "source_id": parent_id,
            "target_kind": "issue",
            "target_id": child_id,
        }
    ]
    assert dependency["kind"] == "blocks"
    assert {row["verb"] for row in activity_rows} >= {"created", "triaged"}
    # On import the forged run fields are dropped (foreign history can't tag itself into a
    # native run) and the row is stamped imported_at instead — its provenance is auditable.
    assert imported_triaged["run_id"] is None
    assert imported_triaged["parent_run_id"] is None
    assert imported_triaged["forked_from_event_id"] is None
    assert imported_triaged["imported_at"] is not None
    assert any(hit["kind"] == "issue" and hit["source_id"] == parent_id for hit in hits)


def test_import_neutralizes_forged_activity_provenance(tmp_path):
    # WHY: activity travels in the bundle, but a bundle is untrusted input. A hostile bundle
    # must not be able to (a) splice forged events into a NATIVE run by reusing its run_id,
    # (b) back/forward-date the trail, or (c) pack the log with a giant/garbage verb. Import
    # NULLs the run fields, clamps the timestamp to the import instant, bounds the verb, and
    # stamps imported_at so the row is auditable as foreign history.
    bundle = _project_bundle(tmp_path)
    target, owner, member, bot = _prepare_project_target(tmp_path / "forge.db")
    # The target already has a NATIVE run "ops-run" recorded on its own trail.
    native = target.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, run_id) "
        "VALUES (?, 'reviewed', 'user', ?, 'ops-run')",
        (owner, owner),
    )
    target.commit()
    native_id = native.lastrowid

    # Forge an event onto a bundled issue: collide with the native run, jump to the year
    # 3000, and carry a verb that is BOTH oversized AND laced with control chars in its
    # first bytes — so the assertion pins the printable-stripping, not just truncation.
    forged_verb = "a\x07b\tc" + "z" * 100
    bundle["activity"].append(
        {
            "id": 999999,
            "actor_id": bundle["users"][0]["id"],
            "verb": forged_verb,
            "target_kind": "issue",
            "target_id": bundle["issues"][0]["id"],
            "detail": "forged",
            "created_at": "3000-01-01 00:00:00",
            "run_id": "ops-run",
            "parent_run_id": "ops-run",
            "forked_from_event_id": None,
        }
    )

    manifest = portability.build_import_manifest(target, bundle)
    result = portability.replay_import_manifest(target, bundle, manifest)

    forged_id = {
        row["source_id"]: row["target_id"] for row in result["id_map"]["activity"]
    }[999999]
    forged = target.execute(
        "SELECT verb, created_at, run_id, parent_run_id, imported_at "
        "FROM activity WHERE id = ?",
        (forged_id,),
    ).fetchone()
    ops_run_ids = [
        row["id"]
        for row in target.execute(
            "SELECT id FROM activity WHERE run_id = 'ops-run' ORDER BY id"
        ).fetchall()
    ]
    native_row = target.execute(
        "SELECT imported_at FROM activity WHERE id = ?", (native_id,)
    ).fetchone()
    target.close()

    # Run fields dropped: the forged event can never appear in the native run's replay.
    assert forged["run_id"] is None and forged["parent_run_id"] is None
    assert ops_run_ids == [native_id]
    # Future timestamp clamped to the import instant (== the row's own imported_at stamp).
    assert forged["created_at"] != "3000-01-01 00:00:00"
    assert forged["created_at"] == forged["imported_at"]
    # Verb: control chars stripped (a\x07b\tc -> abc) AND truncated to the 80-char cap.
    # Exact-equality pins the stripping — deleting the isprintable filter would fail here.
    assert forged["verb"] == "abc" + "z" * 77
    assert forged["imported_at"] is not None
    # A natively recorded row is never marked imported.
    assert native_row["imported_at"] is None


def test_space_import_replays_manifest_and_rewrites_page_refs(tmp_path):
    source = _connect(tmp_path / "source-space.db")
    owner = _user(source, "owner@example.com", "Owner", role="admin")
    member = _user(source, "member@example.com", "Member")
    project = projects.create_project(source, name="Aegis", key="AEG", created_by=owner)
    issue = issues.create_issue(
        source,
        title="External issue",
        body="outside",
        created_by=owner,
        project_id=project["id"],
    )
    space = spaces.create_space(source, key="DOC", name="Docs", created_by=owner)
    access.add_space_member(source, space["id"], member, owner)
    parent = pages.create_page(
        source,
        space_id=space["id"],
        title="Parent",
        body="parent",
        created_by=owner,
    )
    child = pages.create_page(
        source,
        space_id=space["id"],
        title="Child",
        parent_id=parent["id"],
        body=f"See [[page:{parent['id']}]] and [[issue:{issue['id']}]]",
        created_by=member,
    )
    pages.update_page(
        source,
        parent["id"],
        editor_id=member,
        body=f"Parent references [[page:{child['id']}]]",
    )
    page_comments.add_comment(
        source, page_id=child["id"], author_id=owner, body="page comment"
    )
    kb = labels.create_label(source, name="kb", color="#00ff00")
    labels.add_label_to_page(source, child["id"], kb["id"])
    _attachment(
        source,
        target_kind="page",
        target_id=child["id"],
        uploaded_by=owner,
        filename="diagram.txt",
    )
    activity.record(
        source,
        actor_id=owner,
        verb="created",
        target_kind="space",
        target_id=space["id"],
    )
    bundle = portability.export_bundle(source, "space", space["id"])
    source.close()

    target = _connect(tmp_path / "target-space.db")
    extra = _user(target, "extra@example.com", "Extra")
    spaces.create_space(target, key="OLD", name="Old", created_by=extra)
    _user(target, "owner@example.com", "Owner", role="admin")
    _user(target, "member@example.com", "Member")
    manifest = portability.build_import_manifest(target, bundle)
    result = portability.replay_import_manifest(target, bundle, manifest)
    page_map = {row["source_id"]: row["target_id"] for row in result["id_map"]["pages"]}
    imported_child = target.execute(
        "SELECT * FROM pages WHERE id = ?", (page_map[child["id"]],)
    ).fetchone()
    link_rows = target.execute(
        "SELECT source_kind, source_id, target_kind, target_id FROM links "
        "WHERE source_id = ?",
        (page_map[child["id"]],),
    ).fetchall()
    hits = search.search(target, "Child")
    target.close()

    assert result["created"]["spaces"] == 1
    assert result["created"]["pages"] == 2
    assert result["created"]["page_versions"] == 1
    assert result["created"]["labels"] == 1
    assert result["skipped"] == {"attachments": 1, "external_cross_links": 1}
    assert imported_child["parent_id"] == page_map[parent["id"]]
    assert f"[[page:{page_map[parent['id']]}]]" in imported_child["body"]
    assert f"[[issue:{issue['id']}]]" in imported_child["body"]
    assert [dict(row) for row in link_rows] == [
        {
            "source_kind": "page",
            "source_id": page_map[child["id"]],
            "target_kind": "page",
            "target_id": page_map[parent["id"]],
        }
    ]
    assert any(
        hit["kind"] == "page" and hit["source_id"] == page_map[child["id"]]
        for hit in hits
    )


def test_import_cli_replays_json_result_and_refuses_stale_manifest(tmp_path, capsys):
    bundle = _project_bundle(tmp_path)
    bundle_path = tmp_path / "project.json"
    manifest_path = tmp_path / "project.manifest.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    target_path = tmp_path / "target-cli.db"
    target, *_ = _prepare_project_target(target_path)
    manifest = portability.build_import_manifest(target, bundle)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    target.close()

    result = ops.import_main(
        [str(target_path), str(bundle_path), str(manifest_path), "--json"]
    )
    out = capsys.readouterr()
    imported = json.loads(out.out)

    assert result == 0
    assert imported["status"] == "imported"
    assert imported["created"]["projects"] == 1

    result = ops.import_main([str(target_path), str(bundle_path), str(manifest_path)])
    out = capsys.readouterr()
    assert result == 1
    assert "import manifest is no longer ready" in out.err


def test_import_refuses_blocked_manifest(tmp_path):
    bundle = _project_bundle(tmp_path)
    target, *_ = _prepare_project_target(tmp_path / "target-blocked.db")
    manifest = portability.build_import_manifest(
        target,
        bundle,
        attachment_policy="require-blobs",
    )

    try:
        portability.replay_import_manifest(target, bundle, manifest)
    except ValueError as exc:
        assert "not ready" in str(exc)
    else:
        raise AssertionError("blocked manifest should not import")
    finally:
        assert (
            target.execute("SELECT COUNT(*) FROM projects WHERE key='RPL'").fetchone()[
                0
            ]
            == 0
        )
        target.close()
