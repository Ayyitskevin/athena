"""Cost and paging contracts for the agent desk's expired-lease reader."""

from contextlib import closing
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from athena.aegis import leases, projects
from athena.core import db


NOW = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn():
    with closing(db.connect(":memory:")) as conn:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO users (id, email, name) VALUES "
            "(1, 'holder@example.test', 'Holder'), (2, 'other@example.test', 'Other')"
        )
        yield conn


def seed_leases(conn, count):
    ids = range(1, count + 1)
    conn.executemany(
        "INSERT INTO issues (id, title, created_by) VALUES (?, 'work', 1)",
        ((i,) for i in ids),
    )
    conn.executemany(
        "INSERT INTO issue_leases (issue_id, generation, holder_id, claimed_at, expires_at) "
        "VALUES (?, ?, 1, '1999-01-01 00:00:00', '2000-01-01 00:00:00')",
        ((i, f"{i:032x}") for i in ids),
    )


@pytest.mark.parametrize("count", [20, 2_000])
def test_lapsed_lookup_only_decodes_the_visible_page(conn, count):
    seed_leases(conn, count)
    # The smallest ID has the newest claim. All others tie, including at the cutoff.
    conn.execute(
        "UPDATE issue_leases SET claimed_at = '1999-12-01 00:00:00' WHERE issue_id = 1"
    )
    statements = []
    with patch.object(leases, "_row_to_lease", wraps=leases._row_to_lease) as decode:
        conn.set_trace_callback(statements.append)
        try:
            page, total = leases.lapsed_leases_held_by(conn, holder_id=1, now=NOW)
        finally:
            conn.set_trace_callback(None)

    assert total == count
    assert [row["issue_id"] for row in page] == [1, *range(count, count - 19, -1)]
    assert (len(statements), decode.call_count) == (1, 20)


def test_lapsed_page_clamps_the_limit_and_preserves_the_full_count(conn):
    seed_leases(conn, 101)
    for limit, length in [(0, 1), (101, 100)]:
        page, total = leases.lapsed_leases_held_by(
            conn, holder_id=1, limit=limit, now=NOW
        )
        assert len(page) == length
        assert total == 101
    assert leases.lapsed_leases_held_by(conn, holder_id=2, now=NOW) == ([], 0)


def test_lapsed_lookup_filters_before_paging_and_keeps_status_owner_semantics(conn):
    seed_leases(conn, 8)
    configured = projects.create_project(
        conn, name="Configured", key="CFG", created_by=1, commit=True
    )["id"]
    empty = projects.create_project(
        conn, name="Empty menu", key="EMP", created_by=1, commit=True
    )["id"]
    conn.execute("DELETE FROM project_statuses")
    conn.execute(
        "INSERT INTO project_statuses (project_id, name, category, position) "
        "VALUES (?, 'Closed', 'done', 0)",
        (configured,),
    )
    conn.executemany(
        "UPDATE issues SET status = ?, project_id = ? WHERE id = ?",
        [
            ("Closed", configured, 1),
            ("closed", configured, 2),
            ("done", configured, 3),
            ("done", empty, 4),
            ("done", None, 5),
        ],
    )
    conn.execute("UPDATE issues SET archived_at = '1999-12-01 00:00:00' WHERE id = 1")
    conn.execute(
        "UPDATE issue_leases SET expires_at = '2000-01-01 00:00:01' WHERE issue_id = 6"
    )
    conn.execute(
        "UPDATE issue_leases SET holder_id = 2, generation = ? WHERE issue_id = 7",
        ("a" * 32,),
    )
    # ID 8 would win the tied claim order if an orphan consumed a page slot.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM issues WHERE id = 8")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    page, total = leases.lapsed_leases_held_by(conn, holder_id=1, limit=5, now=NOW)
    assert total == 5
    assert [row["issue_id"] for row in page] == [5, 4, 3, 2, 1]
    expected_done = [True, True, False, False, True]
    for row, done in zip(page, expected_done, strict=True):
        assert row["issue_done"] is done
        assert row["active"] is False
        assert row.keys() == leases.get_lease(conn, row["issue_id"]).keys() | {
            "issue_done"
        }
