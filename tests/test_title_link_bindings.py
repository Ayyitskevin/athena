"""Saved references retain the ID selected by the author across renames and edits."""

from contextlib import closing
import sqlite3

import pytest

from athena.aegis import issues
from athena.core import db, links
from athena.mentor import pages, spaces


def seed(conn):
    conn.execute(
        "INSERT INTO users(email,name) VALUES ('owner@example.invalid','Owner')"
    )
    conn.commit()
    return spaces.create_space(conn, key="OPS", name="Operations", created_by=1)["id"]


def page(conn, space_id, title, body=""):
    return pages.create_page(
        conn, space_id=space_id, title=title, body=body, created_by=1
    )["id"]


def edge_ids(conn, kind, source_id):
    return {
        row[0]
        for row in conn.execute(
            "SELECT target_id FROM links WHERE source_kind=? AND source_id=? AND target_kind='page'",
            (kind, source_id),
        )
    }


def test_native_identity_release_rebind_and_atomic_rollback(tmp_path):
    with closing(db.connect(tmp_path / "native.db")) as conn:
        db.migrate(conn)
        sp = seed(conn)
        original = page(conn, sp, "Runbook")
        source = issues.create_issue(
            conn, title="Deploy", body="[[Runbook]] [[ RUNBOOK ]]", created_by=1
        )["id"]
        pages.update_page(conn, original, title="New title", editor_id=1)
        namesake = page(conn, sp, "Runbook")
        issues.update_issue(conn, source, body="follow [[Runbook]]")
        assert links.source_title_targets(conn, "issue", source) == {
            "runbook": original
        }
        assert edge_ids(conn, "issue", source) == {original}

        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(conn, immediate=True):
                issues.update_issue(conn, source, body="", commit=False)
                issues.update_issue(conn, source, body="[[ Runbook ]]", commit=False)
                assert edge_ids(conn, "issue", source) == {namesake}
                conn.execute("INSERT INTO spaces(id) VALUES (?)", (sp,))
        assert issues.get_issue(conn, source)["body"] == "follow [[Runbook]]"
        assert links.stored_title_bindings(conn, "issue", source) == {
            "runbook": original
        }
        assert edge_ids(conn, "issue", source) == {original}

        issues.update_issue(conn, source, body="remove the token")
        issues.update_issue(conn, source, body="[[Runbook]]")
        assert edge_ids(conn, "issue", source) == {namesake}
        conn.execute("DELETE FROM issues WHERE id=?", (source,))
        assert not links.stored_title_bindings(conn, "issue", source)


def test_normalization_reserved_tokens_and_retryable_forward_links(tmp_path):
    with closing(db.connect(tmp_path / "forward.db")) as conn:
        db.migrate(conn)
        sp = seed(conn)
        source = page(conn, sp, "Source", "[[  ÄBC  ]] [[user:7]] [[KEY-7]]")
        assert links.source_title_targets(conn, "page", source) == {"Äbc": None}
        assert links.stored_title_bindings(conn, "page", source) == {}
        page(conn, sp, "äbc")
        good = page(conn, sp, "Äbc")
        pages.update_page(conn, source, body="new text [[ÄBC]]", editor_id=1)
        assert links.source_title_targets(conn, "page", source) == {"Äbc": good}
        pages.set_archived(conn, good, True)
        pages.update_page(conn, source, body="retained [[ Äbc ]]", editor_id=1)
        assert edge_ids(conn, "page", source) == {good}
        assert pages.delete_page(conn, good)
        page(conn, sp, "Äbc")
        pages.update_page(conn, source, body="dangling [[Äbc]]", editor_id=1)
        assert edge_ids(conn, "page", source) == {good}
        assert links.source_title_targets(conn, "page", source) == {"Äbc": good}
        assert pages.delete_page(conn, source)
        assert not links.stored_title_bindings(conn, "page", source)


def test_legacy_proof_distinguishes_numeric_edges_conflicting_history_and_unbound():
    assert links.legacy_title_bindings("[[A]] [[page:1]]", [1]) == {"a": None}
    assert links.legacy_title_bindings("[[A]] [[page:1]]", [1, 20]) == {"a": 20}
    assert links.legacy_title_bindings(
        "[[A]] [[B]]",
        [10, 20],
        titles_by_id={
            10: ["A", "B"],
            20: ["A", "B"],
        },
    ) == {"a": None, "b": None}
    assert links.legacy_title_bindings(
        "[[A]] [[B]]",
        [10, 20],
        titles_by_id={
            10: ["A", "new A"],
            20: ["B", "new B"],
        },
    ) == {"a": 10, "b": 20}


def test_upgrade_freezes_old_body_and_evidence_before_any_new_edits(
    tmp_path, monkeypatch, migration_inventory_through
):
    inventory = db.MIGRATIONS_DIR
    migration_inventory_through("0083_issue_review_requests.sql")
    with closing(db.connect(tmp_path / "upgrade.db")) as conn:
        db.migrate(conn)
        sp = seed(conn)
        original = page(conn, sp, "Runbook")
        other = page(conn, sp, "Auxiliary")
        source = issues.create_issue(conn, title="Old issue", body="", created_by=1)[
            "id"
        ]
        body = f"[[Runbook]] [[Auxiliary]] [[Unknown]] [[page:{other}]]"
        conn.execute("UPDATE issues SET body=? WHERE id=?", (body, source))
        conn.executemany(
            "INSERT INTO links VALUES ('issue', ?, 'page', ?)",
            [(source, original), (source, other)],
        )
        conn.commit()
        pages.update_page(conn, original, title="Renamed", editor_id=1)
        page(conn, sp, "Runbook")
        conn.commit()
        monkeypatch.setattr(db, "MIGRATIONS_DIR", inventory)
        assert db.migrate(conn) == ["0084_title_link_bindings.sql"]
        # New names must not change the interpretation of pre-upgrade evidence.
        pages.update_page(conn, original, title="Unknown", editor_id=1)
        before = conn.total_changes
        expected = {"runbook": original, "auxiliary": None, "unknown": None}
        assert links.source_title_targets(conn, "issue", source) == expected
        assert edge_ids(conn, "issue", source) == {original, other}
        assert conn.total_changes == before
        issues.update_issue(conn, source, body="after edit " + body)
        assert links.stored_title_bindings(conn, "issue", source) == expected
        assert edge_ids(conn, "issue", source) == {original, other}
        assert (
            conn.execute("SELECT COUNT(*) FROM legacy_title_link_sources").fetchone()[0]
            == 0
        )


def test_page_version_restore_keeps_retained_identity_and_resolves_reintroduced_token(
    tmp_path,
):
    with closing(db.connect(tmp_path / "restore.db")) as conn:
        db.migrate(conn)
        sp = seed(conn)
        original = page(conn, sp, "Runbook")
        source = page(conn, sp, "Consumer", "v1 [[Runbook]]")
        pages.update_page(conn, source, body="v2 [[Runbook]]", editor_id=1)
        pages.update_page(conn, original, title="Renamed", editor_id=1)
        namesake = page(conn, sp, "Runbook")
        pages.restore_version(conn, source, 1, editor_id=1)
        assert edge_ids(conn, "page", source) == {original}
        pages.update_page(conn, source, body="cleared", editor_id=1)
        pages.restore_version(conn, source, 1, editor_id=1)
        assert edge_ids(conn, "page", source) == {namesake}
