"""An inbox read touches what it returns, not every notification ever received (H4).

Notifications are never deleted (retention is H20), so an inbox read that walks a
user's rows grows with history: the reviewer measured the operator's priority
inbox at 33, 357 and 1,875 ms at 1,000, 10,000 and 50,000 issues (PERF-7). The
newest-first list sorted every row the user ever received in a temporary B-tree,
and the priority inbox read the whole visible inbox before applying its limit.

These tests pin:

  * the plans (principle 12: plans, not timings): the unread count and every
    unread read go through the partial index over unread rows; the newest-first
    list walks the user's index in id order and stops at its limit, with no sort
    of the user's whole history; the priority inbox reads its rows a bounded batch
    at a time, never with an unbounded LIMIT;
  * the answers: every inbox read, for every caller, returns exactly the rows,
    order and counts it returned before the change (recorded on the code before
    it), on a workspace with read and unread rows, private and public containers,
    a member removed from a private project, priority, mute and digest preferences;
  * the migration: building the indexes on a database with history changes no
    answer, and the plans use them.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from athena.aegis import desk, issues, notification_priority, projects
from athena.core import activity, db, notifications, users
from athena.main import create_app

from support import ADMIN, add_user, as_user, bootstrap

CLEO = as_user(2)  # creates and changes everything; owns the private containers
OTTO = as_user(3)  # watches; was a member of the private project, then removed
NELL = as_user(4)  # an agent-like seat that never acknowledges


def _index_migration() -> Path:
    """This change's migration, found by name so no test depends on its number."""
    (found,) = db.MIGRATIONS_DIR.glob("*_inbox_indexes.sql")
    return found


class Recording:
    """A connection that remembers every statement it is asked to run."""

    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.statements: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return self._inner.execute(sql, params)


def _inbox_plans(conn, statements) -> list[tuple[str, tuple, list[str]]]:
    found = []
    for sql, params in statements:
        if "FROM notifications" not in sql:
            continue
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
        found.append((" ".join(sql.split()), params, plan))
    return found


def _history(db_file: Path) -> None:
    """Three people and a seat, public and private containers, and enough events
    that every read has more history than it returns."""
    with TestClient(create_app(db_file)) as c:
        bootstrap(c)
        add_user(c, "cleo@e.com", name="Cleo")
        add_user(c, "otto@e.com", name="Otto")
        add_user(c, "nell@e.com", name="Nell")

        def call(method, path, body=None, headers=ADMIN, ok=(200, 201, 204)):
            response = c.request(method, path, json=body, headers=headers)
            assert response.status_code in ok, (path, response.text)
            return response.json() if response.content else None

        opn = call("POST", "/projects", {"name": "Open", "key": "OPN"}, CLEO)["id"]
        sec = call("POST", "/projects", {"name": "Secret", "key": "SEC"}, CLEO)["id"]
        call("PUT", f"/projects/{sec}/visibility", {"visibility": "private"}, CLEO)
        call("POST", f"/projects/{sec}/members", {"user_id": 3}, CLEO)
        found = []
        for n, (project, priority) in enumerate(
            [
                (opn, "urgent"),
                (opn, "high"),
                (opn, "low"),
                (sec, "high"),
                (sec, "medium"),
                (None, "medium"),
            ]
        ):
            body = {"title": f"Work {n}", "priority": priority}
            if project is not None:
                body["project_id"] = project
            found.append(call("POST", "/issues", body, CLEO)["id"])
        for issue_id in found:
            for watcher in (ADMIN, OTTO):
                call(
                    "POST",
                    "/watches",
                    {"target_kind": "issue", "target_id": issue_id},
                    watcher,
                )
        for issue_id in found[:2]:
            call(
                "POST",
                "/watches",
                {"target_kind": "issue", "target_id": issue_id},
                NELL,
            )
        doc = call("POST", "/spaces", {"key": "DOC", "name": "Docs"}, CLEO)["id"]
        call("POST", "/watches", {"target_kind": "space", "target_id": doc}, OTTO)
        page = call("POST", f"/spaces/{doc}/pages", {"title": "Runbook"}, CLEO)["id"]

        for round_ in range(4):
            for issue_id in found:
                call(
                    "POST",
                    f"/issues/{issue_id}/comments",
                    {"body": f"note {round_}"},
                    CLEO,
                )
            status = ("in_progress", "open")[round_ % 2]
            call("PATCH", f"/issues/{found[round_]}", {"status": status}, CLEO)
            call("PATCH", f"/pages/{page}", {"body": f"step {round_}"}, CLEO)

        otto_rows = c.get("/notifications", params={"limit": 200}, headers=OTTO).json()
        oldest_first = sorted(row["id"] for row in otto_rows)
        for notification_id in oldest_first[:5]:
            call("POST", f"/notifications/{notification_id}/read", headers=OTTO)
        call("POST", "/notifications/read_all", {"through_id": oldest_first[12]}, OTTO)
        ann_rows = c.get("/notifications", params={"limit": 200}, headers=ADMIN).json()
        call(
            "POST",
            "/notifications/read_all",
            {"ids": sorted(row["id"] for row in ann_rows)[:7]},
        )

        far = "2999-01-01T00:00:00Z"
        prefs = [
            (OTTO, "issue", found[0], {"priority": "low"}),
            (OTTO, "issue", found[1], {"mute_until": far}),
            (OTTO, "issue", found[2], {"priority": "urgent"}),
            (OTTO, "space", doc, {"digest_window_minutes": 60}),
            (ADMIN, "issue", found[5], {"priority": "urgent", "mute_until": far}),
        ]
        for who, kind, target, body in prefs:
            call("PUT", f"/watches/{kind}/{target}/preference", body, who)

        # Otto leaves the private project: what he was told about it stays in his
        # inbox, hidden at read.
        call("DELETE", f"/projects/{sec}/members/3", headers=CLEO, ok=(200, 204))
        # One more event after he left, on a public issue.
        call("POST", f"/issues/{found[0]}/comments", {"body": "last"}, CLEO)


_LIST_QUERIES: tuple[dict, ...] = (
    {},
    {"unread": "true"},
    {"limit": 3},
    {"unread": "true", "limit": 2},
    {"limit": 200},
)
_PRIORITY_QUERIES: tuple[dict, ...] = (
    {},
    {"unread": "true"},
    {"min_priority": "high"},
    {"min_priority": "urgent", "limit": 1},
    {"include_muted": "true"},
    {"digest": "true"},
    {"limit": 3},
    {"unread": "true", "include_muted": "true", "limit": 5},
    {"min_priority": "medium", "include_muted": "true", "limit": 200},
)


def _row(row: dict) -> tuple:
    return (
        row["id"],
        row["event_id"],
        row["read_at"] is not None,
        row["verb"],
        row["target_kind"],
        row["target_id"],
        row.get("target_state"),
    )


def _item(item: dict) -> tuple:
    return (
        item["id"],
        item["read_at"] is not None,
        item["priority"],
        item["muted"],
        item["delivery_state"],
        item["digest_bucket"] is not None,
        item["source"]["priority_source"],
        item["source"]["preference_target_kind"],
    )


def _answers(db_file: Path) -> dict[str, object]:
    """Every inbox read, as every caller makes it."""
    answers: dict[str, object] = {}
    with TestClient(create_app(db_file)) as c:
        for name, headers in (
            ("ann", ADMIN),
            ("cleo", CLEO),
            ("otto", OTTO),
            ("nell", NELL),
        ):
            for params in _LIST_QUERIES:
                rows = c.get("/notifications", params=params, headers=headers).json()
                answers[f"{name} GET /notifications {params}"] = [_row(r) for r in rows]
            answers[f"{name} unread_count"] = c.get(
                "/notifications/unread_count", headers=headers
            ).json()["count"]
            for params in _PRIORITY_QUERIES:
                body = c.get(
                    "/notifications/priority", params=params, headers=headers
                ).json()
                answers[f"{name} priority {params}"] = [_item(i) for i in body["items"]]
            for unread in ("false", "true"):
                answers[f"{name} summary unread={unread}"] = c.get(
                    "/notifications/priority/summary",
                    params={"unread": unread},
                    headers=headers,
                ).json()["by_priority"]
    with closing(db.connect(db_file)) as conn:
        for user_id in (1, 2, 3, 4):
            user = users.get_user(conn, user_id)
            # The cockpit's inbox page and the desk's preview and count.
            answers[f"{user_id} cockpit inbox"] = [
                _row(r)
                for r in notifications.list_notifications(
                    conn, user_id, limit=100, actor=user
                )
            ]
            answers[f"{user_id} desk preview"] = [
                _row(r)
                for r in notifications.list_notifications(
                    conn,
                    user_id,
                    unread_only=True,
                    limit=desk.NOTIFICATIONS_LIMIT,
                    actor=user,
                )
            ]
            answers[f"{user_id} desk unread"] = notifications.unread_count(
                conn, user_id, actor=user
            )
            answers[f"{user_id} ungated unread"] = notifications.unread_count(
                conn, user_id
            )
    return answers


#: Every answer of ``_answers`` on ``_history``, recorded on cdf2c49, before the
#: change. Rows are ``_row`` tuples and items ``_item`` tuples, as JSON lists.
EXPECTED_ANSWERS = json.loads(
    (Path(__file__).parent / "fixtures" / "inbox_answers.json").read_text(
        encoding="utf-8"
    )
)


def _as_json(answers: dict[str, object]) -> object:
    return json.loads(json.dumps(answers))


def test_every_inbox_read_answers_as_before(tmp_path):
    db_file = tmp_path / "inbox.db"
    _history(db_file)
    assert _as_json(_answers(db_file)) == EXPECTED_ANSWERS


# --- the plans -------------------------------------------------------------------


@pytest.fixture
def long_inbox(tmp_path):
    """One reader with far more history than any read returns: 300 notifications
    on a public issue and a private one, the oldest 250 read."""
    db_file = tmp_path / "long.db"
    with closing(db.connect(db_file)) as conn:
        db.migrate(conn)
        conn.executemany(
            "INSERT INTO users (id, email, name, role) VALUES (?, ?, ?, ?)",
            [(1, "a@e.com", "A", "admin"), (2, "b@e.com", "B", "member")],
        )
        conn.commit()
        for key, visibility in (("OPN", "public"), ("SHT", "private")):
            project = projects.create_project(
                conn, name=key, key=key, created_by=1, commit=True
            )
            conn.execute(
                "UPDATE projects SET visibility = ? WHERE id = ?",
                (visibility, project["id"]),
            )
            issues.create_issue(
                conn,
                title=key,
                body="",
                created_by=1,
                project_id=project["id"],
                commit=True,
            )
        for n in range(300):
            event = activity.record(
                conn,
                actor_id=1,
                verb="commented",
                target_kind="issue",
                target_id=1 + n % 2,
                commit=False,
            )
            conn.execute(
                "INSERT INTO notifications (user_id, event_id) VALUES (2, ?)",
                (event["id"],),
            )
        conn.execute(
            "UPDATE notifications SET read_at = datetime('now') WHERE id <= 250"
        )
        conn.commit()
    return db_file


def _recorded(db_file: Path, read) -> tuple[object, list[tuple[str, tuple, list[str]]]]:
    with closing(db.connect(db_file)) as conn:
        recording = Recording(conn)
        result = read(recording, users.get_user(conn, 2))
        return result, _inbox_plans(conn, recording.statements)


def _no_walk(plans) -> None:
    """No step sorts the user's history (a temp B-tree over every row), scans the
    table, or reaches rows through an index that is not the read's own: the
    unique (user_id, event_id) index and 0023's (user_id, read_at, id) index walk
    every notification of the user for a newest-first read."""
    for _, _, plan in plans:
        walks = [
            step
            for step in plan
            if "TEMP B-TREE" in step
            or step.startswith(("SCAN n", "SCAN notifications"))
            or "idx_notifications_user " in step
            or "sqlite_autoindex_notifications_1" in step
        ]
        assert walks == [], plan


def _uses(plans, prefix: str) -> None:
    assert plans, "the read did not touch notifications"
    for sql, _, plan in plans:
        steps = [
            step for step in plan if step.split()[1:2] in (["n"], ["notifications"])
        ]
        assert steps and steps[0].startswith(prefix), (sql, steps)


def test_the_unread_count_reads_only_unread_rows_from_their_index(long_inbox):
    count, plans = _recorded(
        long_inbox, lambda conn, user: notifications.unread_count(conn, 2, actor=user)
    )
    assert count == 25  # half of the 50 unread are on the private issue
    _no_walk(plans)
    # The gate reads each unread row's event, so the row is fetched: the index
    # holds no event id (with one, an acknowledgement by id was planned as a
    # probe of every visible event for every id).
    _uses(
        plans,
        "SEARCH n USING INDEX idx_notifications_unread (user_id=? AND read_at=?)",
    )
    ungated, plans = _recorded(
        long_inbox, lambda conn, user: notifications.unread_count(conn, 2)
    )
    assert ungated == 50
    _no_walk(plans)
    _uses(plans, "SEARCH notifications USING COVERING INDEX idx_notifications_unread")
    # An admin sees every event, so the count needs no event at all: the index
    # answers it alone, without a join per unread row.
    admin, plans = _recorded(
        long_inbox,
        lambda conn, user: notifications.unread_count(
            conn, 2, actor=users.get_user(conn, 1)
        ),
    )
    assert admin == 50
    _no_walk(plans)
    assert [plan for _, _, plan in plans] == [
        [
            "SEARCH notifications USING COVERING INDEX idx_notifications_unread "
            "(user_id=? AND read_at=?)"
        ]
    ]


def test_the_unread_list_reads_newest_unread_rows_from_their_index(long_inbox):
    rows, plans = _recorded(
        long_inbox,
        lambda conn, user: notifications.list_notifications(
            conn, 2, unread_only=True, limit=10, actor=user
        ),
    )
    assert [r["id"] for r in rows] == list(range(299, 279, -2))
    _no_walk(plans)
    _uses(
        plans, "SEARCH n USING INDEX idx_notifications_unread (user_id=? AND read_at=?)"
    )


def test_the_newest_first_list_stops_at_its_limit_instead_of_sorting_history(
    long_inbox,
):
    rows, plans = _recorded(
        long_inbox,
        lambda conn, user: notifications.list_notifications(
            conn, 2, limit=10, actor=user
        ),
    )
    assert [r["id"] for r in rows] == list(range(299, 279, -2))
    _no_walk(plans)
    _uses(plans, "SEARCH n USING INDEX idx_notifications_inbox (user_id=?")


def test_the_priority_inbox_reads_a_bounded_batch_not_the_whole_inbox(long_inbox):
    result, plans = _recorded(
        long_inbox,
        lambda conn, user: notification_priority.list_priority_notifications(
            conn, 2, limit=5, actor=user
        ),
    )
    assert [i["id"] for i in result["items"]] == [299, 297, 295, 293, 291]
    _no_walk(plans)
    _uses(plans, "SEARCH n USING INDEX idx_notifications_inbox (user_id=?")
    # Each statement is bounded, and together they read a few rows, not 300.
    limits = [params[-1] for _, params, _ in plans]
    assert all(0 < limit <= 50 for limit in limits), limits
    assert sum(limits) < 50, limits


# --- the migration --------------------------------------------------------------


def test_the_index_migration_changes_no_answer_on_a_database_with_history(
    tmp_path, monkeypatch
):
    source = db.MIGRATIONS_DIR
    mine = _index_migration()
    before = tmp_path / "before"
    through = tmp_path / "through"
    before.mkdir()
    through.mkdir()
    for migration in sorted(source.glob("*.sql")):
        if migration.name < mine.name:
            (before / migration.name).write_bytes(migration.read_bytes())
        if migration.name <= mine.name:
            (through / migration.name).write_bytes(migration.read_bytes())
    db_file = tmp_path / "inbox.db"
    monkeypatch.setattr(db, "MIGRATIONS_DIR", before)
    _history(db_file)
    without = _answers(db_file)

    monkeypatch.setattr(db, "MIGRATIONS_DIR", through)
    with closing(db.connect(db_file)) as conn:
        assert db.migrate(conn) == [mine.name]
        indexes = {
            row["name"]: row["sql"]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'notifications'"
            )
        }
        assert "idx_notifications_unread" in indexes
        assert "WHERE read_at IS NULL" in indexes["idx_notifications_unread"]
        assert "idx_notifications_inbox" in indexes
        # Running its work again changes nothing.
        schema = sorted(map(tuple, conn.execute("SELECT name, sql FROM sqlite_master")))
        conn.executescript(mine.read_text(encoding="utf-8"))
        assert (
            sorted(map(tuple, conn.execute("SELECT name, sql FROM sqlite_master")))
            == schema
        )
    assert _answers(db_file) == without
    assert _as_json(without) == EXPECTED_ANSWERS


def test_an_acknowledgement_by_id_asks_the_gate_of_each_rows_own_event(long_inbox):
    """Acknowledging a few ids must cost those ids. Written as `event_id IN
    (every visible event)` and planned through an unread index that held the
    event id, it probed every visible event for every id: 22 s for 40 ids at
    50,000 issues, inside the writer lock. Each id is found by its key and its
    one event by its key."""
    with closing(db.connect(long_inbox)) as conn:
        unread = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM notifications WHERE user_id = 2 AND read_at IS NULL "
                "ORDER BY id"
            )
        ]
    some = unread[:6]

    def acknowledge(conn, user):
        visible = notifications.visible_own_ids(conn, 2, some, actor=user)
        changed = notifications.mark_read_bounded(
            conn, 2, ids=some, through_id=None, actor=user, commit=False
        )
        return visible, changed

    with closing(db.connect(long_inbox)) as conn:
        recording = Recording(conn)
        visible, changed = acknowledge(recording, users.get_user(conn, 2))
        plans = [
            (
                " ".join(sql.split()),
                params,
                [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)],
            )
            for sql, params in recording.statements
            if "notifications" in sql
        ]
    # Half the reader's notifications are on the private issue it cannot see.
    assert 0 < len(visible) < len(some) and changed == len(visible)
    assert [sql.split()[0] for sql, _, _ in plans] == ["SELECT", "UPDATE"]
    for sql, _, plan in plans:
        assert "notifications.event_id" in sql, sql
        rows = [step for step in plan if step.split()[1:2] == ["notifications"]]
        assert rows == ["SEARCH notifications USING INTEGER PRIMARY KEY (rowid=?)"], (
            plan
        )
        events = [step for step in plan if step.split()[1:2] == ["a"]]
        assert events == ["SEARCH a USING INTEGER PRIMARY KEY (rowid=?)"], plan
        assert not [step for step in plan if step.startswith("SCAN")], plan


def test_the_migration_leaves_two_indexes_and_not_the_one_they_replace(long_inbox):
    with closing(db.connect(long_inbox)) as conn:
        indexes = {
            row["name"]: row["sql"]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'notifications' "
                "AND sql IS NOT NULL"
            )
        }
    assert "idx_notifications_user" not in indexes, indexes
    assert set(indexes) >= {"idx_notifications_unread", "idx_notifications_inbox"}
    assert "event_id" not in indexes["idx_notifications_unread"]


def test_a_priority_read_few_rows_pass_grows_its_batch_instead_of_crawling(tmp_path):
    """A `min_priority` few rows pass makes the read go back through history.
    Its batch doubles, so 2,000 rows cost a handful of statements, not one per
    page of the limit."""
    db_file = tmp_path / "narrow.db"
    with closing(db.connect(db_file)) as conn:
        db.migrate(conn)
        conn.executemany(
            "INSERT INTO users (id, email, name, role) VALUES (?, ?, ?, ?)",
            [(1, "a@e.com", "A", "admin"), (2, "b@e.com", "B", "member")],
        )
        issues.create_issue(conn, title="t", body="", created_by=1, commit=True)
        for n in range(2000):
            event = activity.record(
                conn,
                actor_id=1,
                verb="commented",
                target_kind="issue",
                target_id=1,
                commit=False,
            )
            conn.execute(
                "INSERT INTO notifications (user_id, event_id) VALUES (2, ?)",
                (event["id"],),
            )
        conn.commit()
        recording = Recording(conn)
        listed = notifications.list_priority_notifications(
            recording, 2, limit=5, min_priority="urgent", actor=users.get_user(conn, 2)
        )
        reads = [
            sql
            for sql, _ in recording.statements
            if "FROM notifications" in sql and "LIMIT" in sql
        ]
    assert listed["items"] == []  # nothing here is urgent: the whole inbox is read
    assert 2 <= len(reads) <= 12, len(reads)
