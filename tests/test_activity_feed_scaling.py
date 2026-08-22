"""The gated activity feed must not get more expensive as the trail grows.

WHY THIS FILE EXISTS
F-0.1 replaced a feed read that SQLite answered by evaluating a four-arm OR
against essentially every activity row and sorting the survivors through a temp
B-tree before LIMIT. The symptom recorded in `core/activity._paged_feed_sql` is
precise: cost scaled with the whole trail, and "the LIMIT is inert" — asking for
10 rows cost what asking for 50 cost. The fix asks each disjoint arm for its own
bounded page through `idx_activity_kind_id` and merges four short lists.

Nothing guarded that. The equivalence tests pin that the new shape returns the
SAME ROWS; none of them fail if the shape silently goes back to reading the whole
trail, because a slower query returns identical results.

WHY VDBE UNITS AND NOT MILLISECONDS
A wall-clock assertion on a 2-vCPU shared CI runner is a flake generator, and a
flaky perf gate gets deleted. `sqlite3_progress_handler` fires every N virtual
machine operations, so counting callbacks measures WORK rather than TIME:
deterministic, identical on a laptop and a runner, and immune to load. The
assertions below are ratios, which cancels the unit entirely.

WHY NO ANALYZE
`scripts/seed_benchmark.py` carries the warning at length: an earlier version ran
ANALYZE to be "fair" and produced numbers ~800x optimistic, nearly retiring a real
ceiling as a non-issue. No Athena database has sqlite_stat1 — the product never
runs ANALYZE or PRAGMA optimize. This fixture must not either. Do not "fix" that.

MEASURED HEADROOM (2026-08-21, so a future reader knows what normal looks like)
                       500 events   8000 events   limit=10 vs limit=50
    naive (pre-F-0.1)      256          4081         256 vs 255  (inert)
    current                 12            12          12 vs  52  (live)
The thresholds below sit far outside both, so they fail on a regression and never
on noise.
"""

from athena.core import activity, db

# One arm's worth of visible history, then eight times as much. The ratio between
# them is the whole assertion, so the absolute numbers only need to be far enough
# apart that linear growth is unmistakable.
_SMALL = 500
_LARGE = 4000

# A regression to the old shape multiplies work by the growth factor (8x here).
# Anything under 2x is flat for our purposes and leaves room for a constant-cost
# change that is not a scaling change.
_MAX_GROWTH_RATIO = 2.0

# The old shape spent the same work for limit=10 and limit=50 (ratio ~1.0). A live
# LIMIT costs visibly less for a smaller page. 0.8 is a wide margin below 1.0.
_MAX_SMALL_PAGE_RATIO = 0.8


def _seed(tmp_path, name, event_count):
    """A member, a public project, and `event_count` issue events they can see.

    The visibility gate matters here: events whose target does not resolve to
    something the actor may see force every arm to walk its entire kind partition
    looking for rows that qualify. That is a real worst case, but it is not the
    shape this test is about, and measuring it would hide the scaling property
    behind a constant. So the fixture makes the trail genuinely visible.
    """
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('member@example.com', 'M', 'member')"
    )
    conn.execute(
        "INSERT INTO projects (key, name, visibility, created_by, activity_scope_key)"
        " VALUES ('P', 'P', 'public', 1, '0123456789abcdef0123456789abcdef')"
    )
    conn.commit()
    member = dict(
        conn.execute(
            "SELECT * FROM users WHERE email = 'member@example.com'"
        ).fetchone()
    )
    conn.executemany(
        "INSERT INTO issues (title, status, project_id, created_by)"
        " VALUES (?, 'open', 1, 1)",
        [(f"issue {index}",) for index in range(event_count)],
    )
    conn.commit()
    issue_ids = [row["id"] for row in conn.execute("SELECT id FROM issues ORDER BY id")]
    conn.executemany(
        "INSERT INTO activity"
        " (actor_id, verb, target_kind, target_id, detail, created_at, visibility_restricted)"
        " VALUES (1, 'created', 'issue', ?, '', datetime('now'), 0)",
        [(issue_id,) for issue_id in issue_ids],
    )
    conn.commit()
    return conn, member


def _work_units(conn, call):
    """Virtual-machine work for one read, in units of 100 SQLite operations."""
    units = 0

    def tick():
        nonlocal units
        units += 1
        return 0

    conn.set_progress_handler(tick, 100)
    try:
        call()
    finally:
        conn.set_progress_handler(None, 0)
    return units


def _feed_cost(conn, member, limit):
    return _work_units(
        conn, lambda: activity.list_activity(conn, actor=member, limit=limit)
    )


def test_gated_feed_cost_does_not_scale_with_trail_size(tmp_path):
    # WHY: this is the F-0.1 regression itself. If the feed goes back to reading
    # the whole trail, an 8x longer trail costs ~8x more to read one page — and
    # every equivalence test still passes, because the rows returned are correct.
    small_conn, member = _seed(tmp_path, "small.db", _SMALL)
    large_conn, _ = _seed(tmp_path, "large.db", _LARGE)

    small_cost = _feed_cost(small_conn, member, 50)
    large_cost = _feed_cost(large_conn, member, 50)

    assert small_cost > 0, "progress handler recorded no work; the probe is broken"
    growth = large_cost / small_cost
    assert growth <= _MAX_GROWTH_RATIO, (
        f"reading one page got {growth:.1f}x more expensive when the trail grew "
        f"{_LARGE / _SMALL:.0f}x ({small_cost} -> {large_cost} work units). The "
        "feed is scaling with the trail again — see _paged_feed_sql."
    )


def test_gated_feed_limit_is_not_inert(tmp_path):
    # WHY: "the LIMIT is inert" was the tell that the page was being assembled
    # after the fact, from a full read. A page of 10 must cost visibly less than a
    # page of 50; if the two converge, the bounded per-arm read is gone.
    conn, member = _seed(tmp_path, "inert.db", _LARGE)

    small_page = _feed_cost(conn, member, 10)
    large_page = _feed_cost(conn, member, 50)

    assert large_page > 0, "progress handler recorded no work; the probe is broken"
    ratio = small_page / large_page
    assert ratio <= _MAX_SMALL_PAGE_RATIO, (
        f"a 10-row page cost {ratio:.2f} of a 50-row page "
        f"({small_page} vs {large_page} work units). The LIMIT is inert again, "
        "which means the page is being cut from a full read."
    )


def test_gated_feed_seeks_the_kind_index(tmp_path):
    # WHY: the bounded per-arm read only works because each arm pins target_kind to
    # a literal and can therefore walk idx_activity_kind_id in id order. If the
    # planner stops seeking that index, the two tests above are about to fail for
    # a reason this one names precisely.
    conn, member = _seed(tmp_path, "plan.db", _SMALL)
    built = activity._paged_feed_sql(
        conn,
        member,
        clauses=[],
        params=[],
        target_kind=None,
        direction="DESC",
        limit=50,
    )
    assert built is not None, "the gate admitted nothing; the fixture is wrong"
    sql, params = built
    plan = " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )
    assert "idx_activity_kind_id" in plan, plan
    assert "SCAN a" not in plan, plan
