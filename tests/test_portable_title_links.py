"""Selective import must carry runbook identity across a change of page IDs."""

import pytest

from athena.aegis import issues, projects
from athena.core import db, links, portability
from athena.mentor import pages, spaces


def _database(path):
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name) VALUES ('owner@example.test', 'Owner')"
    )
    conn.commit()
    return conn


def _space(conn, key):
    return spaces.create_space(conn, key=key, name=key, created_by=1)["id"]


def _page(conn, space_id, title, body=""):
    return pages.create_page(
        conn, space_id=space_id, title=title, body=body, created_by=1
    )["id"]


def _import(conn, bundle):
    manifest = portability.build_import_manifest(conn, bundle)
    assert manifest["status"] == "ready"
    portability.replay_import_manifest(conn, bundle, manifest)


def _assert_edges(conn, kind, source_id, expected):
    assert {
        edge["id"] for edge in links.outgoing_links(conn, kind, source_id)
    } == expected


@pytest.mark.parametrize("legacy", [False, True])
def test_import_renamed_runbook_and_external_namesake(tmp_path, legacy):
    origin = _database(tmp_path / "origin.db")
    docs = _space(origin, "DOC")
    other = _space(origin, "EXT")
    runbook = _page(origin, docs, "Handbook")
    _page(origin, other, "External")
    local = _page(origin, docs, "Local source", "[[Handbook]]")
    external = _page(origin, docs, "External source", "[[External]]")
    pages.update_page(origin, runbook, title="Renamed handbook", editor_id=1)
    _page(origin, docs, "Handbook")
    bundle = portability.export_bundle(origin, "space", docs)
    if legacy:
        bundle.pop("title_bindings")
    else:
        assert bundle["title_bindings"] == [
            {"source_id": local, "token": "handbook", "target_page_id": runbook},
            {"source_id": external, "token": "external", "target_page_id": None},
        ]

    target = _database(tmp_path / "destination.db")
    _page(target, _space(target, "DST"), "External")
    _import(target, bundle)
    ids = {r["title"]: r["id"] for r in target.execute("SELECT id, title FROM pages")}
    new_runbook = ids["Renamed handbook"]
    assert new_runbook != runbook
    for title, token, expected_id in [
        ("Local source", "Handbook", new_runbook),
        ("External source", "External", None),
    ]:
        source_id = ids[title]
        body = pages.get_page(target, source_id)["body"]
        assert body == f"[[{token}]]"
        assert links.source_title_targets(target, "page", source_id) == {
            token.lower(): expected_id
        }
        pages.update_page(target, source_id, editor_id=1, body=body + " retained")
        _assert_edges(
            target, "page", source_id, {expected_id} if expected_id else set()
        )
    origin.close()
    target.close()


def test_legacy_space_import_recovers_distinct_titles_from_page_history(tmp_path):
    origin = _database(tmp_path / "origin.db")
    docs = _space(origin, "DOC")
    first = _page(origin, docs, "Alpha")
    second = _page(origin, docs, "Bravo")
    _page(origin, docs, "Procedure", "[[Alpha]] and [[Bravo]]")
    pages.update_page(origin, first, title="New alpha", editor_id=1)
    pages.update_page(origin, second, title="New bravo", editor_id=1)
    bundle = portability.export_bundle(origin, "space", docs)
    del bundle["title_bindings"]

    target = _database(tmp_path / "target.db")
    _page(target, _space(target, "DST"), "Alpha")
    _import(target, bundle)
    ids = {r["title"]: r["id"] for r in target.execute("SELECT id, title FROM pages")}
    assert links.source_title_targets(target, "page", ids["Procedure"]) == {
        "alpha": ids["New alpha"],
        "bravo": ids["New bravo"],
    }
    origin.close()
    target.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_project_import_does_not_acquire_destination_runbook_on_next_edit(
    tmp_path, legacy
):
    origin = _database(tmp_path / "project.db")
    _page(origin, _space(origin, "OPS"), "On call")
    project = projects.create_project(origin, name="Handoff", key="OPS", created_by=1)
    issues.create_issue(
        origin,
        title="Investigate",
        body="[[On call]]",
        project_id=project["id"],
        created_by=1,
    )
    bundle = portability.export_bundle(origin, "project", project["id"])
    if legacy:
        bundle.pop("title_bindings")
    target = _database(tmp_path / "destination.db")
    _page(target, _space(target, "OPS"), "On call")
    _import(target, bundle)
    issue_id = target.execute(
        "SELECT id FROM issues WHERE title = 'Investigate'"
    ).fetchone()[0]
    assert links.source_title_targets(target, "issue", issue_id) == {"on call": None}
    issues.update_issue(target, issue_id, body="[[On call]] kept")
    _assert_edges(target, "issue", issue_id, set())
    origin.close()
    target.close()


def test_import_distinguishes_missing_binding_from_retryable_forward_reference(
    tmp_path,
):
    origin = _database(tmp_path / "origin.db")
    docs = _space(origin, "DOC")
    _page(origin, _space(origin, "EXT"), "Existing outside")
    source = _page(origin, docs, "Guide", "[[Existing outside]] [[Future]]")
    bundle = portability.export_bundle(origin, "space", docs)
    held, retry = bundle["title_bindings"]
    assert held == {
        "source_id": source,
        "token": "existing outside",
        "target_page_id": None,
    }
    assert retry == {
        "source_id": source,
        "token": "future",
        "target_page_id": None,
        "retry_on_save": True,
    }
    target = _database(tmp_path / "target.db")
    dst = _space(target, "DST")
    _page(target, dst, "Existing outside")
    future = _page(target, dst, "Future")
    for incomplete in ([], [retry], [held]):
        with pytest.raises(ValueError, match="every authored bare title"):
            _import(target, bundle | {"title_bindings": incomplete})
    with pytest.raises(ValueError, match="contradicts a bundled page edge"):
        _import(
            target, bundle | {"title_bindings": [held | {"retry_on_save": True}, retry]}
        )
    _import(target, bundle)
    guide = target.execute(
        "SELECT id, body FROM pages WHERE title = 'Guide'"
    ).fetchone()
    pages.update_page(target, guide["id"], editor_id=1, body=guide["body"] + " edited")
    _assert_edges(target, "page", guide["id"], {future})
    origin.close()
    target.close()


def test_import_refuses_inconsistent_title_binding_metadata(tmp_path):
    origin = _database(tmp_path / "source.db")
    docs = _space(origin, "DOC")
    runbook = _page(origin, docs, "Runbook")
    source = _page(origin, docs, "Reader", "[[Runbook]]")
    unrelated = _page(origin, docs, "Unrelated")
    bundle = portability.export_bundle(origin, "space", docs)
    target = _database(tmp_path / "target.db")
    good = {"source_id": source, "token": "runbook", "target_page_id": runbook}
    for invalid in (
        {},
        [],
        [good | {"retry_on_save": True}],
        [good | {"target_page_id": None, "retry_on_save": "yes"}],
        [{"source_id": 2000, "token": "runbook", "target_page_id": None}],
        [good | {"token": "absent"}],
        [good | {"target_page_id": 2000}],
        [good | {"target_page_id": True}],
        [good | {"target_page_id": unrelated}],
        [good, good],
    ):
        with pytest.raises(ValueError, match="title_bindings"):
            portability.build_import_manifest(
                target, bundle | {"title_bindings": invalid}
            )
    assert target.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
    origin.close()
    target.close()
