"""Archived issues drop out of full-text search — the soft-delete invariant held there too.

An archived (soft-deleted) issue disappears from the issue list, board, dashboard, and
label counts. The FTS index is derived and carries no archived flag, so a plain search
used to leak archived issues back as live hits (while the same query WITH a structured
filter, routed through list_issues, correctly hid them). search now excludes archived
issues by source, consistently on both the no-filter and filtered paths, with an explicit
opt-in to search the archive.
"""
from fastapi.testclient import TestClient

from athena.aegis import issue_search, issues
from athena.core import db, search
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _has(hits, iid):
    return any(h["source_id"] == iid for h in hits)


def test_archived_issue_is_hidden_from_search_but_findable_on_opt_in(tmp_path):
    db_file = tmp_path / "s.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        iid = client.post(
            "/issues", json={"title": "Payment webhook bug"}, headers=H1
        ).json()["id"]

    conn = db.connect(db_file)
    # Indexed on creation → a plain search finds it on both paths.
    assert _has(issue_search.search_issues(conn, "Payment"), iid)  # aegis no-filter branch
    assert _has(search.search(conn, "Payment", kind="issue"), iid)  # core search

    # Archive it — it must drop out of search, like every other list.
    issues.set_archived(conn, iid, True)
    assert not _has(issue_search.search_issues(conn, "Payment"), iid)
    assert not _has(search.search(conn, "Payment", kind="issue"), iid)
    # The filtered branch already hid it; assert it still does (consistency).
    assert not _has(issue_search.search_issues(conn, "Payment", status="open"), iid)

    # The archive is still searchable on an explicit opt-in.
    assert _has(search.search(conn, "Payment", kind="issue", include_archived=True), iid)

    # Restoring it brings it back to ordinary search.
    issues.set_archived(conn, iid, False)
    assert _has(issue_search.search_issues(conn, "Payment"), iid)
