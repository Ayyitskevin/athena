"""The verb-windowed activity scans are indexed, and the planner uses the index.

The admin attention rollup runs on every authenticated admin request, and two of
its inputs read activity by verb within a window: the security refusal counts
(verb IN (...) AND imported_at IS NULL AND created_at >= ?) and the
budget-exhaustion count (verb = ? AND created_at >= ? AND imported_at IS NULL).
Before 0075 no index led with verb, so both reads full-scanned the append-only
table — a per-request tax that grew with the trail itself. These tests pin that
the index exists AND that SQLite seeks by it (SEARCH ... USING INDEX, not
SCAN activity) — the behavioral intent, not just the schema.
"""

from athena.core import db


def _plan(conn, sql, params=()):
    return " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )


def test_activity_verb_window_index_exists(tmp_path):
    conn = db.connect(tmp_path / "idx.db")
    db.migrate(conn)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity'"
        )
    }
    assert "idx_activity_verb_window" in names


def test_refusal_counts_use_index_not_scan(tmp_path):
    conn = db.connect(tmp_path / "plan.db")
    db.migrate(conn)
    # The security_events.failure_counts shape: several verbs, native rows only,
    # optionally window-bounded. One indexed range per verb, no table scan.
    windowed = _plan(
        conn,
        "SELECT verb, COUNT(*) AS n FROM activity "
        "WHERE verb IN (?, ?, ?) AND imported_at IS NULL AND created_at >= ? "
        "GROUP BY verb",
        ("login_failed", "token_rejected", "scope_denied", "2026-01-01 00:00:00"),
    )
    assert "idx_activity_verb_window" in windowed and "SCAN activity" not in windowed
    # Unwindowed (the all-time counts on /admin/security) seeks the same way.
    unwindowed = _plan(
        conn,
        "SELECT verb, COUNT(*) AS n FROM activity "
        "WHERE verb IN (?, ?) AND imported_at IS NULL GROUP BY verb",
        ("login_failed", "token_rejected"),
    )
    assert (
        "idx_activity_verb_window" in unwindowed and "SCAN activity" not in unwindowed
    )


def test_budget_exhaustion_count_uses_index_not_scan(tmp_path):
    conn = db.connect(tmp_path / "plan.db")
    db.migrate(conn)
    # The fleet_attention budget-exhaustion shape: one verb, one window.
    plan = _plan(
        conn,
        "SELECT COUNT(*) AS n FROM activity "
        "WHERE verb = ? AND created_at >= ? AND imported_at IS NULL",
        ("agent_budget_exhausted", "2026-01-01 00:00:00"),
    )
    assert "idx_activity_verb_window" in plan and "SCAN activity" not in plan
