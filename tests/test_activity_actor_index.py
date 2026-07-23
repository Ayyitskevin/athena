"""The actor-filtered activity feed is indexed, and the planner uses it.

list_activity / list_events / the per-agent run rollups all filter `actor_id = ?`
and order by id -- the shape of every "what did this actor do" read. Before 0047 only
(target_kind, target_id, id) was indexed, so an actor-scoped read full-scanned the
unbounded activity table and then sorted. These tests pin that the index exists AND
that SQLite seeks by it (SEARCH ... USING INDEX, not SCAN activity) -- the behavioral
intent, not just the schema.
"""

from athena.core import db


def _plan(conn, sql, params=()):
    return " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )


def test_activity_actor_index_exists(tmp_path):
    conn = db.connect(tmp_path / "idx.db")
    db.migrate(conn)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity'"
        )
    }
    assert "idx_activity_actor" in names


def test_actor_feed_uses_index_not_scan(tmp_path):
    conn = db.connect(tmp_path / "plan.db")
    db.migrate(conn)
    # Newest-first, bounded -- the list_activity feed / CSV export shape.
    desc = _plan(
        conn,
        "SELECT id FROM activity WHERE actor_id = ? ORDER BY id DESC LIMIT ?",
        (1, 50),
    )
    assert "idx_activity_actor" in desc and "SCAN activity" not in desc
    # Oldest-first, bounded -- the list_events feed shape.
    asc = _plan(
        conn,
        "SELECT id FROM activity WHERE actor_id = ? ORDER BY id ASC LIMIT ?",
        (1, 50),
    )
    assert "idx_activity_actor" in asc and "SCAN activity" not in asc
