"""The idle sort reads a stored key through an index, and answers as before (H5).

``sort:idle-desc`` ordered issues by COALESCE(last work, created_at), where "last
work" is the newest native activity row on the issue with a substantive work verb
(``work_progress.last_work_sql``). That was computed for every matching issue and
the whole set sorted before a page was taken: about 8.0 M SQLite steps at 50,000
issues. Migration 0096 stores it (``issues.last_work_at``) and the issue's
open/closed verdict (``issues.status_closed``), each kept by triggers, and indexes
both, so a page of the most idle issues, open or not, is a seek and a short walk.

What this file holds:

- **Nothing answers differently.** A random history of issues, events of every
  kind (the verbs that count as work and those that do not, imported ones, rows
  on other kinds of target), comments, status changes, project moves, projects'
  own statuses changing, archiving, and rows edited or removed outside the app,
  is replayed step by step. After every step each issue's stored values equal
  what the old definition computes from the trail, and every idle sort and filter
  answers the same issues in the same order as the old SQL, kept here as the
  reference.
- **The copies of two Python constants in the migration's SQL are the
  constants.** A trigger cannot call Python, so the work verbs and the default
  status mapping are written out; a change to either fails here.
- **The backfill** computes the same values on a database that has history.
- **The plan** seeks one of the two indexes and never reads the trail or sorts,
  with and without statistics, and the work of a page does not grow with the
  issues it does not show.
"""

from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from pathlib import Path
import random
import re
import sqlite3

import pytest
from test_history_bounded_reads import FLEET_STATISTICS

from athena.aegis import (
    comment_commands,
    issue_commands,
    issue_query,
    issues,
    projects,
    statuses,
    work_progress,
)
from athena.core import activity, db, users, work_query
from athena.core import clock as app_clock

MIGRATION = "0096_issue_idle_key.sql"
_TS = "%Y-%m-%d %H:%M:%S"

# --- the old definition, kept as the reference ------------------------------------
#
# This is what issue_query computed before 0096, word for word: the idle anchor
# read the trail per issue, and open/closed resolved the category through the
# project's own status row, else the default mapping.

OLD_ANCHOR = f"COALESCE({work_progress.last_work_sql('i.id')}, i.created_at)"
OLD_CATEGORY = statuses.category_sql(
    status_column="i.status", category_column="qps.category"
)
OLD_OPEN = f"{OLD_CATEGORY} != 'done'"
OLD_CLOSED = f"{OLD_CATEGORY} = 'done'"
JOIN = (
    " LEFT JOIN project_statuses qps "
    "ON qps.project_id = i.project_id AND qps.name = i.status"
)

NON_WORK_VERBS = (
    "created",
    "claimed",
    "lease_renewed",
    "lease_lapsed",
    "lease_released",
    "assigned",
    "labeled",
    "changed_priority",
    "archived",
    "comment_deleted",
    "unlinked",
    "delegated",
)


def _stored_disagreements(conn: sqlite3.Connection) -> list[dict]:
    """Every issue whose stored key differs from the old definition's value."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT i.id, i.last_work_at, "
            f"{work_progress.last_work_sql('i.id')} AS computed_work, "
            f"i.status_closed, ({OLD_CLOSED}) AS computed_closed "
            f"FROM issues i{JOIN} "
            f"WHERE i.last_work_at IS NOT {work_progress.last_work_sql('i.id')} "
            f"OR i.status_closed IS NOT ({OLD_CLOSED})"
        )
    ]


def _days_cutoff(now, days: int) -> str:
    return (now - timedelta(days=days)).strftime(_TS)


#: Each query and how the old code answered it: (raw, clauses(now), order by).
#: ``None`` for clauses means "issue_query's default": archived issues left out.
def _reference_cases(now) -> list[tuple[str, list[str], list, str]]:
    idle = f"{OLD_ANCHOR} ASC, i.id DESC"
    by_id = "i.id DESC"
    live = "i.archived_at IS NULL"
    return [
        ("sort:idle-desc", [live], [], idle),
        ("is:open sort:idle-desc", [OLD_OPEN, live], [], idle),
        ("is:closed sort:idle-desc", [OLD_CLOSED, live], [], idle),
        ("-is:open sort:idle-desc", [f"NOT ({OLD_OPEN})", live], [], idle),
        ("-is:closed sort:idle-desc", [f"NOT ({OLD_CLOSED})", live], [], idle),
        (
            "idle:>3d sort:idle-desc",
            [f"{OLD_ANCHOR} < ?", live],
            [_days_cutoff(now, 3)],
            idle,
        ),
        (
            "idle:<3d sort:idle-desc",
            [f"{OLD_ANCHOR} > ?", live],
            [_days_cutoff(now, 3)],
            idle,
        ),
        (
            "is:open idle:>2d sort:idle-desc",
            [OLD_OPEN, f"{OLD_ANCHOR} < ?", live],
            [_days_cutoff(now, 2)],
            idle,
        ),
        (
            "is:closed -idle:>5d sort:idle-desc",
            [OLD_CLOSED, f"NOT ({OLD_ANCHOR} < ?)", live],
            [_days_cutoff(now, 5)],
            idle,
        ),
        (
            "is:open idle:>1d",
            [OLD_OPEN, f"{OLD_ANCHOR} < ?", live],
            [_days_cutoff(now, 1)],
            by_id,
        ),
        ("is:archived sort:idle-desc", ["i.archived_at IS NOT NULL"], [], idle),
        ("is:open sort:created-asc", [OLD_OPEN, live], [], "i.created_at, i.id"),
        ("is:closed", [OLD_CLOSED, live], [], by_id),
    ]


def _reference(conn, clauses, params, order_by, visible) -> list[int]:
    clauses = list(clauses)
    params = list(params)
    if visible is not None:
        placeholders = ",".join("?" for _ in visible)
        clauses.append(f"(i.project_id IS NULL OR i.project_id IN ({placeholders}))")
        params.extend(visible)
    sql = (
        f"SELECT i.id FROM issues i{JOIN} WHERE {' AND '.join(clauses)} "
        f"ORDER BY {order_by}"
    )
    return [row["id"] for row in conn.execute(sql, params)]


def _assert_answers_unchanged(conn, admin, visible_sets) -> None:
    now = app_clock.now()
    for raw, clauses, params, order_by in _reference_cases(now):
        query = work_query.parse(raw)
        for visible in visible_sets:
            expected = _reference(conn, clauses, params, order_by, visible)
            rows = issue_query.run_query(
                conn, query, actor=admin, visible_project_ids=visible, limit=10_000
            )
            assert [row["id"] for row in rows] == expected, (raw, visible)
            assert issue_query.count_query(
                conn, query, actor=admin, visible_project_ids=visible
            ) == len(expected), (raw, visible)
            if raw.endswith("sort:idle-desc"):
                # The keyset pages resume on the same key, so paging through
                # three at a time walks the same order.
                paged: list[int] = []
                after = None
                while True:
                    page, after = issue_query.query_page(
                        conn,
                        query,
                        actor=admin,
                        visible_project_ids=visible,
                        limit=3,
                        after=after,
                    )
                    paged.extend(row["id"] for row in page)
                    if after is None:
                        break
                assert paged == expected, (raw, visible)


# --- the workspace ----------------------------------------------------------------


@pytest.fixture
def ws(tmp_path, clock):
    conn = db.connect(tmp_path / "idle.db")
    db.migrate(conn)
    yield conn, _admin(conn), clock
    conn.close()


def _admin(conn: sqlite3.Connection) -> dict:
    created = users.create_user(
        conn, email="owner@example.invalid", name="Owner", role="admin", commit=True
    )
    admin = users.get_user(conn, created["id"])
    assert admin is not None
    return admin


class History:
    """A random history, written step by step through the app and around it."""

    def __init__(self, conn, admin, clock, rng: random.Random):
        self.conn = conn
        self.admin = admin
        self.clock = clock
        self.rng = rng
        self.plain = projects.create_project(
            conn, name="Plain", key="PLN", created_by=admin["id"], commit=True
        )
        # A project with its own closed name, and one that makes "done" open.
        self.custom = projects.create_project(
            conn, name="Custom", key="CST", created_by=admin["id"], commit=True
        )
        statuses.add_status(conn, self.custom["id"], "shipped", "done", commit=True)
        statuses.add_status(conn, self.custom["id"], "review", "doing", commit=True)
        self.odd = projects.create_project(
            conn, name="Odd", key="ODD", created_by=admin["id"], commit=True
        )
        conn.execute(
            "UPDATE project_statuses SET category = 'todo' "
            "WHERE project_id = ? AND name = 'done'",
            (self.odd["id"],),
        )
        conn.commit()
        self.project_ids = [None, self.plain["id"], self.custom["id"], self.odd["id"]]
        self.issue_ids: list[int] = []

    # the steps ------------------------------------------------------------------

    def create(self):
        project_id = self.rng.choice(self.project_ids)
        status = self.rng.choice(statuses.status_names(self.conn, project_id))
        issue = issue_commands.create_issue(
            self.conn,
            actor=self.admin,
            title=f"issue {len(self.issue_ids)}",
            status=status,
            project_id=project_id,
        )
        self.issue_ids.append(issue["id"])

    def _issue(self) -> int:
        return self.rng.choice(self.issue_ids)

    def event(self):
        verb = self.rng.choice(work_progress.SUBSTANTIVE_WORK_VERBS + NON_WORK_VERBS)
        activity.record(
            self.conn,
            actor_id=self.admin["id"],
            verb=verb,
            target_kind="issue",
            target_id=self._issue(),
            commit=True,
        )

    def imported_event(self):
        # Foreign history: a work verb, the newest row on the issue's trail, and
        # still not work. (Written once, never edited, so only the insert
        # trigger decides.)
        activity.record(
            self.conn,
            actor_id=self.admin["id"],
            verb=self.rng.choice(work_progress.SUBSTANTIVE_WORK_VERBS),
            target_kind="issue",
            target_id=self._issue(),
            imported_at=self.clock.now().strftime(_TS),
            commit=True,
        )

    def other_target(self):
        # A work verb on a page or a project that shares an issue's id.
        activity.record(
            self.conn,
            actor_id=self.admin["id"],
            verb="commented",
            target_kind=self.rng.choice(["page", "project"]),
            target_id=self._issue(),
            commit=True,
        )

    def comment(self):
        comment_commands.create_comment(
            self.conn, actor=self.admin, issue_id=self._issue(), body="progress"
        )

    def _commandable_issue(self) -> dict | None:
        """An issue the app's commands will write: its status is one its project
        offers (the steps below that write around the app can leave one that
        is not, and the commands refuse to record a change from it)."""
        candidates = [
            issue
            for issue in (issues.get_issue(self.conn, n) for n in self.issue_ids)
            if issue is not None
            and statuses.is_valid(self.conn, issue["project_id"], issue["status"])
        ]
        return self.rng.choice(candidates) if candidates else None

    def change_status(self):
        issue = self._commandable_issue()
        if issue is None:
            return
        issue_commands.update_issue(
            self.conn,
            actor=self.admin,
            issue_id=issue["id"],
            status=self.rng.choice(
                statuses.status_names(self.conn, issue["project_id"])
            ),
        )

    def move(self):
        issue = self._commandable_issue()
        if issue is None:
            return
        issue_commands.update_issue(
            self.conn,
            actor=self.admin,
            issue_id=issue["id"],
            project_id=self.rng.choice(self.project_ids),
        )

    def move_outside(self):
        # The app's move also rewrites the status column; a shell may change
        # the project alone, and open/closed must follow that too. Prefer a
        # project where the issue's status name means the other verdict.
        issue_id = self._issue()
        status = issues.get_issue(self.conn, issue_id)["status"]

        def closed_in(project_id) -> bool:
            category = statuses.category_of(self.conn, project_id, status)
            return (category or statuses.global_category(self.conn, status)) == "done"

        here = issues.get_issue(self.conn, issue_id)["project_id"]
        flips = [p for p in self.project_ids if closed_in(p) != closed_in(here)]
        self.conn.execute(
            "UPDATE issues SET project_id = ? WHERE id = ?",
            (self.rng.choice(flips or self.project_ids), issue_id),
        )
        self.conn.commit()

    def archive(self):
        issue_id = self._issue()
        archived = issues.get_issue(self.conn, issue_id)["archived_at"] is not None
        issue_commands.set_issue_archived(
            self.conn, actor=self.admin, issue_id=issue_id, archived=not archived
        )

    def recategorise(self):
        row = self.rng.choice(
            self.conn.execute(
                "SELECT id FROM project_statuses WHERE project_id != ?",
                (self.plain["id"],),
            ).fetchall()
        )
        self.conn.execute(
            "UPDATE project_statuses SET category = ? WHERE id = ?",
            (self.rng.choice(statuses.CATEGORIES), row["id"]),
        )
        self.conn.commit()

    def rename_status(self):
        # A rename leaves the issues on the old name, which then falls back to
        # the default mapping; renaming back restores the project's row.
        row = self.conn.execute(
            "SELECT id, name FROM project_statuses WHERE project_id = ? "
            "AND name IN ('shipped', 'shipped2')",
            (self.custom["id"],),
        ).fetchone()
        new = "shipped2" if row["name"] == "shipped" else "shipped"
        self.conn.execute(
            "UPDATE project_statuses SET name = ? WHERE id = ?", (new, row["id"])
        )
        self.conn.commit()

    def add_and_remove_status(self):
        name = "done"
        existing = self.conn.execute(
            "SELECT id FROM project_statuses WHERE project_id = ? AND name = ?",
            (self.custom["id"], name),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO project_statuses (project_id, name, category, position) "
                "VALUES (?, ?, ?, 99)",
                (self.custom["id"], name, self.rng.choice(statuses.CATEGORIES)),
            )
        else:
            # Outside the app (remove_status refuses a name in use): the
            # issues on it fall back to the default mapping.
            self.conn.execute(
                "DELETE FROM project_statuses WHERE id = ?", (existing["id"],)
            )
        self.conn.commit()

    def unknown_status(self):
        self.conn.execute(
            "UPDATE issues SET status = ? WHERE id = ?",
            (self.rng.choice(["limbo", "done", "open", "shipped"]), self._issue()),
        )
        self.conn.commit()

    def backdate_event(self):
        row = self._issue_event()
        if row is None:
            return
        shift = timedelta(days=self.rng.randint(-20, 3))
        self.conn.execute(
            "UPDATE activity SET created_at = ? WHERE id = ?",
            ((self.clock.now() + shift).strftime(_TS), row["id"]),
        )
        self.conn.commit()

    def backdate_issue(self):
        self.conn.execute(
            "UPDATE issues SET created_at = ? WHERE id = ?",
            (
                (self.clock.now() - timedelta(days=self.rng.randint(0, 20))).strftime(
                    _TS
                ),
                self._issue(),
            ),
        )
        self.conn.commit()

    def flip_imported(self):
        row = self._issue_event()
        if row is None:
            return
        self.conn.execute(
            "UPDATE activity SET imported_at = CASE WHEN imported_at IS NULL "
            "THEN created_at END WHERE id = ?",
            (row["id"],),
        )
        self.conn.commit()

    def change_verb(self):
        row = self._issue_event()
        if row is None:
            return
        self.conn.execute(
            "UPDATE activity SET verb = ? WHERE id = ?",
            (
                self.rng.choice(work_progress.SUBSTANTIVE_WORK_VERBS + NON_WORK_VERBS),
                row["id"],
            ),
        )
        self.conn.commit()

    def retarget(self):
        row = self._issue_event()
        if row is None:
            return
        kind, target = self.rng.choice(
            [("issue", self._issue()), ("page", self._issue())]
        )
        self.conn.execute(
            "UPDATE activity SET target_kind = ?, target_id = ? WHERE id = ?",
            (kind, target, row["id"]),
        )
        self.conn.commit()

    def delete_event(self):
        # The trail keeps its rows in the app; a shell with foreign keys off
        # (the sqlite3 default) can still remove one.
        row = self._issue_event()
        newest = self.conn.execute("SELECT MAX(id) AS id FROM activity").fetchone()
        # Not the newest row: SQLite would hand its id to the next event, whose
        # hash-chain entry would then collide with the one left behind.
        if row is None or row["id"] == newest["id"]:
            return
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DELETE FROM activity WHERE id = ?", (row["id"],))
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = ON")

    def _issue_event(self):
        rows = self.conn.execute(
            "SELECT id FROM activity WHERE target_kind = 'issue' "
            "AND verb != 'created' ORDER BY id DESC LIMIT 30"
        ).fetchall()
        return self.rng.choice(rows) if rows else None

    STEPS = (
        ("create", 4),
        ("event", 6),
        ("imported_event", 2),
        ("other_target", 1),
        ("comment", 3),
        ("change_status", 3),
        ("move", 2),
        ("move_outside", 1),
        ("archive", 1),
        ("recategorise", 1),
        ("rename_status", 1),
        ("add_and_remove_status", 1),
        ("unknown_status", 1),
        ("backdate_event", 2),
        ("backdate_issue", 1),
        ("flip_imported", 1),
        ("change_verb", 1),
        ("retarget", 1),
        ("delete_event", 1),
    )

    def schedule(self, length: int) -> list[str]:
        """Four issues, then every kind of step once in a random order, so each
        history reaches every trigger, then ``length`` weighted random draws."""
        names = [name for name, _ in self.STEPS]
        weights = [weight for _, weight in self.STEPS]
        every = list(names)
        self.rng.shuffle(every)
        return ["create"] * 4 + every + self.rng.choices(names, weights, k=length)

    def step(self, name: str) -> None:
        # Time passes unevenly: sometimes not at all, so keys tie on the second.
        self.clock.advance(seconds=self.rng.choice([0, 1, 7, 3600, 86400, 3 * 86400]))
        getattr(self, name)()


@pytest.mark.parametrize("seed", range(6))
def test_the_stored_key_is_the_trails_value_after_every_step(ws, seed):
    conn, admin, clock = ws
    history = History(conn, admin, clock, random.Random(seed))
    visible_sets = [None, {history.plain["id"]}, set()]
    schedule = history.schedule(70)
    for n, name in enumerate(schedule):
        history.step(name)
        assert _stored_disagreements(conn) == [], (seed, n, schedule[: n + 1][-5:])
        if n % 3 == 2:
            _assert_answers_unchanged(conn, admin, visible_sets)
    _assert_answers_unchanged(conn, admin, visible_sets)


# --- the copies of Python constants in the migration --------------------------------


def _migration_sql() -> str:
    return (db.MIGRATIONS_DIR / MIGRATION).read_text()


def test_the_migrations_work_verbs_are_the_progress_views():
    lists = re.findall(r"verb IN \(([^)]*)\)", _migration_sql())
    # The backfill, the insert trigger, the update and the delete triggers.
    assert len(lists) == 4
    for listed in lists:
        verbs = tuple(re.findall(r"'([a-z_]+)'", listed))
        assert verbs == work_progress.SUBSTANTIVE_WORK_VERBS


def test_the_migrations_default_mapping_is_category_sqls():
    flat = " ".join(_migration_sql().split())
    generated = statuses.category_sql(
        status_column="issues.status", category_column="X"
    )
    case = generated[len("COALESCE(X, ") : -1]
    # The backfill and the five open/closed triggers, and no other mapping.
    assert flat.count(case) == 6
    assert flat.count("CASE issues.status") == 6


@pytest.mark.parametrize(
    "status", [name for name, _ in statuses.DEFAULT_STATUSES] + ["limbo"]
)
def test_a_backlog_issue_is_closed_exactly_when_the_default_mapping_says(ws, status):
    conn, admin, _ = ws
    issue = issue_commands.create_issue(conn, actor=admin, title="t")
    conn.execute("UPDATE issues SET status = ? WHERE id = ?", (status, issue["id"]))
    conn.commit()
    stored = conn.execute(
        "SELECT status_closed FROM issues WHERE id = ?", (issue["id"],)
    ).fetchone()["status_closed"]
    assert stored == (statuses.global_category(conn, status) == "done")
    assert _stored_disagreements(conn) == []


def test_open_closed_follows_a_projects_own_statuses(ws):
    """Each change to a project's status rows, and a move that changes the
    project alone, sets the verdict the category expression gives."""
    conn, admin, _ = ws
    project = projects.create_project(
        conn, name="Custom", key="CST", created_by=admin["id"], commit=True
    )
    statuses.add_status(conn, project["id"], "shipped", "done", commit=True)
    shipped = issue_commands.create_issue(
        conn, actor=admin, title="s", status="shipped", project_id=project["id"]
    )
    done = issue_commands.create_issue(
        conn, actor=admin, title="d", status="done", project_id=project["id"]
    )

    def closed() -> tuple[int, int]:
        assert _stored_disagreements(conn) == []
        rows = {
            row["id"]: row["status_closed"]
            for row in conn.execute("SELECT id, status_closed FROM issues")
        }
        return rows[shipped["id"]], rows[done["id"]]

    def run(sql: str, *params) -> None:
        conn.execute(sql, params)
        conn.commit()

    assert closed() == (1, 1)
    # Renamed away: "shipped" is no longer the project's, and falls to doing.
    run("UPDATE project_statuses SET name = 'gone' WHERE name = 'shipped'")
    assert closed() == (0, 1)
    run("UPDATE project_statuses SET name = 'shipped' WHERE name = 'gone'")
    assert closed() == (1, 1)
    run(
        "UPDATE project_statuses SET category = 'doing' "
        "WHERE project_id = ? AND name = 'shipped'",
        project["id"],
    )
    assert closed() == (0, 1)
    run(
        "DELETE FROM project_statuses WHERE project_id = ? AND name = 'done'",
        project["id"],
    )
    assert closed() == (0, 1)  # "done" falls to the default mapping: done
    run(
        "INSERT INTO project_statuses (project_id, name, category, position) "
        "VALUES (?, 'done', 'todo', 9)",
        project["id"],
    )
    assert closed() == (0, 0)  # the project made "done" a to-do status
    run(
        "DELETE FROM project_statuses WHERE project_id = ? AND name = 'done'",
        project["id"],
    )
    assert closed() == (0, 1)
    # The project alone changes (a shell, not the app's move, which rewrites
    # the status column too): "shipped" in the backlog is not a closed name.
    run(
        "UPDATE project_statuses SET category = 'done' "
        "WHERE project_id = ? AND name = 'shipped'",
        project["id"],
    )
    assert closed() == (1, 1)
    run("UPDATE issues SET project_id = NULL WHERE id = ?", shipped["id"])
    assert closed() == (0, 1)


def test_each_work_verb_moves_last_work_and_nothing_else_does(ws):
    conn, admin, clock = ws
    issue = issue_commands.create_issue(conn, actor=admin, title="t")

    def stored():
        return conn.execute(
            "SELECT last_work_at FROM issues WHERE id = ?", (issue["id"],)
        ).fetchone()["last_work_at"]

    assert stored() is None  # creation is not work
    for verb in NON_WORK_VERBS:
        clock.advance(minutes=1)
        activity.record(
            conn,
            actor_id=admin["id"],
            verb=verb,
            target_kind="issue",
            target_id=issue["id"],
            commit=True,
        )
        assert stored() is None, verb
    for verb in work_progress.SUBSTANTIVE_WORK_VERBS:
        stamp = clock.advance(minutes=1).strftime(_TS)
        activity.record(
            conn,
            actor_id=admin["id"],
            verb=verb,
            target_kind="issue",
            target_id=issue["id"],
            commit=True,
        )
        assert stored() == stamp, verb
        clock.advance(minutes=1)
        activity.record(
            conn,
            actor_id=admin["id"],
            verb=verb,
            target_kind="issue",
            target_id=issue["id"],
            imported_at=clock.now().strftime(_TS),
            commit=True,
        )
        assert stored() == stamp, f"imported {verb}"


def test_an_issues_read_shape_is_unchanged(ws):
    """The stored keys are index keys: no issue read carries them."""
    conn, admin, _ = ws
    issue = issue_commands.create_issue(conn, actor=admin, title="t")
    comment_commands.create_comment(conn, actor=admin, issue_id=issue["id"], body="b")
    for read in (
        issues.get_issue(conn, issue["id"]),
        *issue_query.run_query(
            conn,
            work_query.parse("sort:idle-desc"),
            actor=admin,
            visible_project_ids=None,
            limit=5,
        ),
    ):
        assert "last_work_at" not in read and "status_closed" not in read


def test_a_rolled_back_event_leaves_the_stored_key_as_it_was(ws):
    conn, admin, clock = ws
    issue = issue_commands.create_issue(conn, actor=admin, title="t")
    clock.advance(hours=1)
    conn.execute("BEGIN IMMEDIATE")
    activity.record(
        conn,
        actor_id=admin["id"],
        verb="commented",
        target_kind="issue",
        target_id=issue["id"],
        commit=False,
    )
    assert conn.execute(
        "SELECT last_work_at FROM issues WHERE id = ?", (issue["id"],)
    ).fetchone()["last_work_at"]
    conn.rollback()
    assert (
        conn.execute(
            "SELECT last_work_at FROM issues WHERE id = ?", (issue["id"],)
        ).fetchone()["last_work_at"]
        is None
    )


# --- the migration on a database with history ---------------------------------------


def test_the_backfill_computes_the_trails_values(
    tmp_path, monkeypatch, clock, migration_inventory_through
):
    packaged = db.MIGRATIONS_DIR
    migration_inventory_through("0095_issue_prior_text_removal.sql")
    assert not (db.MIGRATIONS_DIR / MIGRATION).exists()
    with closing(db.connect(tmp_path / "upgrade.db")) as conn:
        db.migrate(conn)
        admin = _admin(conn)
        history = History(conn, admin, clock, random.Random(7))
        # The app's writers only: the schema has no stored key to keep yet.
        for _ in range(12):
            history.create()
        steps = (
            "event",
            "imported_event",
            "other_target",
            "comment",
            "change_status",
            "move",
            "archive",
            "recategorise",
            "unknown_status",
            "backdate_event",
            "backdate_issue",
        )
        for n in range(80):
            clock.advance(hours=history.rng.choice([0, 1, 30]))
            getattr(history, steps[n % len(steps)])()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(issues)")}
        assert "last_work_at" not in columns

        monkeypatch.setattr(db, "MIGRATIONS_DIR", packaged)
        assert db.migrate(conn) == [MIGRATION]
        assert _stored_disagreements(conn) == []
        worked = conn.execute(
            "SELECT COUNT(*) AS n FROM issues WHERE last_work_at IS NOT NULL"
        ).fetchone()["n"]
        closed = conn.execute(
            "SELECT COUNT(*) AS n FROM issues WHERE status_closed = 1"
        ).fetchone()["n"]
        # The history reached both values of both keys.
        assert 0 < worked < len(history.issue_ids)
        assert 0 < closed < len(history.issue_ids)
        _assert_answers_unchanged(conn, admin, [None, {history.plain["id"]}])


# --- the plan, and the work of a page --------------------------------------------------


def _plan(conn, raw: str, visible) -> list[str]:
    where, params, order_by = issue_query.compile_query(
        conn, work_query.parse(raw), actor=None, visible_project_ids=visible
    )
    sql = (
        f"{issues.select_sql()}{issue_query._JOIN} WHERE {where} "
        f"ORDER BY {order_by} LIMIT 50"
    )
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _analyzed(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    conn.execute("ANALYZE")
    for (table, index), stat in FLEET_STATISTICS.items():
        conn.execute(
            "DELETE FROM sqlite_stat1 WHERE tbl = ? AND idx = ?", (table, index)
        )
        conn.execute(
            "INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (?, ?, ?)",
            (table, index, stat),
        )
    conn.commit()
    conn.close()
    return db.connect(path)


PLANS = [
    ("sort:idle-desc", "idx_issues_idle (archived_at=?)"),
    ("-is:open sort:idle-desc", "idx_issues_idle (archived_at=?)"),
    (
        "is:open sort:idle-desc",
        "idx_issues_closed_idle (archived_at=? AND status_closed=?)",
    ),
    (
        "is:closed sort:idle-desc",
        "idx_issues_closed_idle (archived_at=? AND status_closed=?)",
    ),
    ("idle:>14d sort:idle-desc", "idx_issues_idle (archived_at=? AND <expr><?)"),
    ("idle:<14d sort:idle-desc", "idx_issues_idle (archived_at=? AND <expr>>?)"),
    (
        "is:open idle:>14d sort:idle-desc",
        "idx_issues_closed_idle (archived_at=? AND status_closed=? AND <expr><?)",
    ),
]


@pytest.mark.parametrize("analyzed", [False, True], ids=["no-statistics", "analyzed"])
def test_the_idle_sort_seeks_its_index_and_never_reads_the_trail(
    ws, tmp_path, analyzed
):
    conn, admin, clock = ws
    History(conn, admin, clock, random.Random(1)).create()
    path = tmp_path / "idle.db"
    plan_conn = _analyzed(path) if analyzed else db.connect(path)
    try:
        for raw, index in PLANS:
            for visible in (None, {1, 2}):
                plan = _plan(plan_conn, raw, visible)
                assert plan[0] == f"SEARCH i USING INDEX {index}", (raw, plan)
                assert not any("TEMP B-TREE" in step for step in plan), (raw, plan)
                assert not any(
                    re.search(r"\b(activity|wa)\b", step) for step in plan
                ), (raw, plan)
    finally:
        plan_conn.close()


def _steps(conn: sqlite3.Connection, read) -> int:
    count = [0]

    def tick() -> int:
        count[0] += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        read()
    finally:
        conn.set_progress_handler(None, 1)
    return count[0]


def _quick_issue(conn, admin, *, status: str, created_at: str, work: int) -> None:
    issue = issues.create_issue(
        conn, title="t", body="", created_by=admin["id"], status=status, commit=True
    )
    conn.execute(
        "UPDATE issues SET created_at = ? WHERE id = ?", (created_at, issue["id"])
    )
    for _ in range(work):
        activity.record(
            conn,
            actor_id=admin["id"],
            verb="commented",
            target_kind="issue",
            target_id=issue["id"],
            commit=True,
        )
    conn.commit()


@pytest.mark.parametrize(
    "raw", ["sort:idle-desc", "is:open sort:idle-desc", "is:closed sort:idle-desc"]
)
def test_a_page_of_the_idle_sort_does_not_grow_with_the_issues_it_does_not_show(
    ws, clock, raw
):
    """The most idle page is read off the index: adding many issues behind it,
    closed ones older than any open one included, and long trails on them, does
    not change the work the page takes."""
    conn, admin, _ = ws
    old = (clock.now() - timedelta(days=400)).strftime(_TS)
    for status in ("open", "done") * 6:
        _quick_issue(conn, admin, status=status, created_at=old, work=0)
    query = work_query.parse(raw)

    def page():
        return issue_query.run_query(
            conn, query, actor=admin, visible_project_ids=None, limit=5
        )

    first = [row["id"] for row in page()]
    before = _steps(conn, page)
    newer = (clock.now() - timedelta(days=10)).strftime(_TS)
    for n in range(150):
        _quick_issue(
            conn, admin, status=("open", "done")[n % 2], created_at=newer, work=3
        )
    after = _steps(conn, page)
    assert [row["id"] for row in page()] == first
    # 150 issues and 450 events written behind the page, none of them read.
    assert after <= before + 10, (before, after)
