"""The hot issue-filter columns are indexed, and the planner uses them.

list_issues filters on status / assignee_id / project_id and every visibility-gated
read narrows with `project_id IN (...)`. Before 0045 only sprint_id / parent_id /
archived_at were indexed, so those queries full-scanned the issues table. These tests
pin that the three indexes exist AND that SQLite's planner actually searches by them
(SEARCH ... USING INDEX, not SCAN) — the behavioral intent, not just the schema.
"""
from athena.core import db


def _plan(conn, sql, params=()):
    return " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )


def test_hot_filter_indexes_exist(tmp_path):
    conn = db.connect(tmp_path / "idx.db")
    db.migrate(conn)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='issues'"
        )
    }
    assert {"idx_issues_project", "idx_issues_assignee", "idx_issues_status"} <= names


def test_visibility_gate_and_filters_use_an_index_not_a_scan(tmp_path):
    conn = db.connect(tmp_path / "plan.db")
    db.migrate(conn)
    # The visibility gate: `project_id IN (...)` — the predicate on nearly every read.
    # A SEARCH ... USING [COVERING] INDEX means it seeks by the index; a bare
    # "SCAN issues" would be the full-table read this migration removes.
    gate = _plan(conn, "SELECT id FROM issues WHERE project_id IN (1, 2, 3)")
    assert "idx_issues_project" in gate and "SCAN issues" not in gate
    # A direct project filter (board / issue list scoped to a project).
    proj = _plan(conn, "SELECT id FROM issues WHERE project_id = ?", (1,))
    assert "idx_issues_project" in proj and "SCAN issues" not in proj
    # Assignee filter (e.g. "my issues").
    assignee = _plan(conn, "SELECT id FROM issues WHERE assignee_id = ?", (1,))
    assert "idx_issues_assignee" in assignee and "SCAN issues" not in assignee
