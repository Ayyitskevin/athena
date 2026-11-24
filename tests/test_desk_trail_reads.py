"""The desk finds the newest visible event without reading the trail (H1).

PERF-4, measured by the round-2 review and re-run by the lead at 10,000 issues:
``GET /desk`` for a seat spent 15, 171 and 840 ms at 1,000, 10,000 and 50,000
issues in one statement, the newest event id the caller can see, asked as
``MAX(a.id)`` over the four-arm OR of the event visibility gate. SQLite answers
that by reading every event of every kind and running the gate on each. Asked
one arm at a time, newest first, as the event feed already asks
(``core/activity._paged_feed_sql``), it took 0.02 ms and gave the same id.
``GET /desk/since`` with no cursor had the same shape: the oldest 500 visible
events after 0, asked as one OR and sorted after the gate ran on all of them.

Hidden reads as missing, and the visibility rules are frozen, so the per-arm
form must answer exactly what the OR-joined form answers for every kind of
caller. The first two tests prove that on a workspace with public and private
projects and spaces, backlog issues, moved issues, orphan and imported events
and events of a kind no arm covers, after every write, rather than by argument.
The plan tests hold the shape (principle 12: a test asserts the plan, not a
timing), and the work tests hold its consequence: the work does not grow with
history.
"""

from __future__ import annotations

from collections.abc import Callable
import re
import sqlite3

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, add_user, as_user, bearer, bootstrap, make_app, onboard_agent

from athena.aegis import desk, operator_since
from athena.core import access, activity, db


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


# --- the OR-joined forms, as they stood before H1 -------------------------


def or_joined_latest(conn: sqlite3.Connection, actor: dict | None) -> int | None:
    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    where = f" WHERE {gate}" if gate else ""
    row = conn.execute(
        "SELECT MAX(a.id) AS m FROM activity a "
        f"JOIN users u ON u.id = a.actor_id{where}",
        params,
    ).fetchone()
    return row["m"]


def or_joined_events_after(
    conn: sqlite3.Connection, actor: dict | None, after_id: int
) -> tuple[list[dict], bool]:
    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    where = f"a.id > ? AND a.imported_at IS NULL{f' AND {gate}' if gate else ''}"
    rows = conn.execute(
        "SELECT a.id, a.verb, a.target_kind, a.target_id, a.detail, a.created_at, "
        "a.imported_at, a.actor_id, u.name AS actor_name FROM activity a "
        f"JOIN users u ON u.id = a.actor_id WHERE {where} ORDER BY a.id LIMIT ?",
        [after_id, *params, operator_since.SCAN_LIMIT + 1],
    ).fetchall()
    events = [dict(row) for row in rows]
    return events[: operator_since.SCAN_LIMIT], len(events) > operator_since.SCAN_LIMIT


# --- the workspace ---------------------------------------------------------


class Workspace:
    """Every kind of caller the trail's gate distinguishes, and writes that
    land on each side of it.

    The admin (1); Mo (2), a human member of the private project and the
    private space; Ola (3), a human member of neither; Sol, an agent member of
    neither; Ari, an agent member of the private project only. Anonymous is
    asked at the function level: both routes refuse a caller with no
    credential before either read runs."""

    def __init__(self, client: TestClient, db_file):
        self.client = client
        self.db_file = db_file
        bootstrap(client, email="admin@e.com", name="Admin")
        self.mo = add_user(client, "mo@e.com", name="Mo")
        self.ola = add_user(client, "ola@e.com", name="Ola")
        self.sol = onboard_agent(client, email="sol@e.com", name="Sol")
        self.ari = onboard_agent(client, email="ari@e.com", name="Ari")
        self.public = self.project("Public", "PUB", "public")
        self.private = self.project("Private", "PRV", "private")
        ok(
            client.post(
                f"/projects/{self.private}/members",
                json={"user_id": self.mo["id"]},
                headers=ADMIN,
            ),
            201,
        )
        ok(
            client.post(
                f"/projects/{self.private}/members",
                json={"user_id": self.ari["user"]["id"]},
                headers=ADMIN,
            ),
            201,
        )
        self.open_space = ok(
            client.post("/spaces", json={"key": "OPN", "name": "Open"}, headers=ADMIN),
            201,
        )["id"]
        self.closed_space = ok(
            client.post(
                "/spaces", json={"key": "CLS", "name": "Closed"}, headers=ADMIN
            ),
            201,
        )["id"]
        ok(
            client.put(
                f"/spaces/{self.closed_space}/visibility",
                json={"visibility": "private"},
                headers=ADMIN,
            )
        )
        ok(
            client.post(
                f"/spaces/{self.closed_space}/members",
                json={"user_id": self.mo["id"]},
                headers=ADMIN,
            ),
            201,
        )

    def project(self, name: str, key: str, visibility: str) -> int:
        return ok(
            self.client.post(
                "/projects",
                json={"name": name, "key": key, "visibility": visibility},
                headers=ADMIN,
            ),
            201,
        )["id"]

    def actors(self, conn: sqlite3.Connection) -> dict[str, dict | None]:
        def user(user_id: int) -> dict:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return dict(row.fetchone())

        return {
            "admin": user(1),
            "member of the private project": user(self.mo["id"]),
            "non-member": user(self.ola["id"]),
            "agent": user(self.sol["user"]["id"]),
            "agent member of the private project": user(self.ari["user"]["id"]),
            "anonymous": None,
        }

    def writes(self) -> list[Callable[[], object]]:
        """Writes in an order that leaves the newest event hidden from some
        caller after most of them, and visible after others."""
        client = self.client
        made: dict[str, int] = {}

        def issue(name: str, project: int | None) -> Callable[[], object]:
            def write():
                made[name] = ok(
                    client.post(
                        "/issues",
                        json={"title": name, "project_id": project},
                        headers=ADMIN,
                    ),
                    201,
                )["id"]

            return write

        def comment(name: str) -> Callable[[], object]:
            return lambda: ok(
                client.post(
                    f"/issues/{made[name]}/comments",
                    json={"body": f"on {name}"},
                    headers=ADMIN,
                ),
                201,
            )

        def move(name: str, project: int | None) -> Callable[[], object]:
            return lambda: ok(
                client.put(
                    f"/issues/{made[name]}/project",
                    json={"project_id": project},
                    headers=ADMIN,
                )
            )

        def page(name: str, space: int) -> Callable[[], object]:
            def write():
                made[name] = ok(
                    client.post(
                        f"/spaces/{space}/pages",
                        json={"title": name, "body": "text"},
                        headers=ADMIN,
                    ),
                    201,
                )["id"]

            return write

        def raw(**event) -> Callable[[], object]:
            def write():
                conn = db.connect(self.db_file)
                try:
                    activity.record(conn, actor_id=1, commit=True, **event)
                finally:
                    conn.close()

            return write

        return [
            issue("public one", self.public),
            issue("backlog one", None),
            issue("private one", self.private),
            comment("public one"),
            page("open page", self.open_space),
            comment("private one"),
            page("closed page", self.closed_space),
            issue("moving", self.public),
            move("moving", self.private),
            comment("backlog one"),
            move("moving", None),
            comment("moving"),
            # An event on an issue that does not exist: its scope cannot be
            # resolved, so it is restricted (an orphan), visible to the admin.
            raw(verb="commented", target_kind="issue", target_id=999_999),
            issue("private two", self.private),
            # Foreign history: imported, and so never "since" for anyone.
            raw(
                verb="commented",
                target_kind="issue",
                target_id=1,
                imported_at="2020-01-01 00:00:00",
            ),
            # A kind no arm covers: the admin's alone.
            raw(verb="updated", target_kind="user", target_id=2),
        ]


@pytest.fixture
def workspace(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        yield Workspace(client, db_file)


def _asks_per_arm(conn: sqlite3.Connection, read) -> bool:
    """Whether ``read`` asks the trail one visibility arm at a time: one
    statement over ``activity``, four bounded arm reads joined by UNION ALL."""
    recording = Recording(conn)
    read(recording)
    trail = [sql for sql, _params in recording.statements if "FROM activity a" in sql]
    return (
        len(trail) == 1
        and trail[0].count(" UNION ALL ") == 3
        and trail[0].count("ORDER BY a.id") == 4
    )


def test_the_newest_visible_event_is_the_same_id_for_every_caller(workspace):
    """After every write, for every kind of caller, the per-arm read returns
    the id the OR-joined predicate returns, and the desk route serves it."""
    distinct: set[tuple] = set()
    for write in workspace.writes():
        write()
        conn = db.connect(workspace.db_file)
        try:
            answers = {}
            for who, actor in workspace.actors(conn).items():
                if actor is None or actor["role"] != "admin":
                    assert _asks_per_arm(
                        conn,
                        lambda c, a=actor: desk.latest_visible_event_id(c, actor=a),
                    ), "the read compared here is not the per-arm one"
                expected = or_joined_latest(conn, actor)
                assert desk.latest_visible_event_id(conn, actor=actor) == expected, who
                answers[who] = expected
            distinct.add(tuple(answers.values()))
        finally:
            conn.close()
    # The workspace did separate the callers: some caller's newest visible event
    # was not another's at some point (else the comparison proves little).
    assert any(len(set(answers)) > 2 for answers in distinct), distinct

    conn = db.connect(workspace.db_file)
    try:
        actors = workspace.actors(conn)
        expected = {who: or_joined_latest(conn, actor) for who, actor in actors.items()}
    finally:
        conn.close()
    headers = {
        "admin": ADMIN,
        "member of the private project": as_user(workspace.mo["id"]),
        "non-member": as_user(workspace.ola["id"]),
        "agent": bearer(workspace.sol),
        "agent member of the private project": bearer(workspace.ari),
    }
    for who, header in headers.items():
        signals = ok(workspace.client.get("/desk", headers=header))["signals"]
        assert signals["latest_visible_event_id"] == expected[who], who
    assert workspace.client.get("/desk").status_code == 401


def test_the_events_after_a_cursor_are_the_same_for_every_caller(
    workspace, monkeypatch
):
    """The oldest events after a cursor, per arm and merged, are the rows the
    OR-joined read returns: same events, same order, same cap, at every
    cursor. A small scan limit makes the merge choose between arms."""
    for write in workspace.writes():
        write()
    conn = db.connect(workspace.db_file)
    try:
        head = conn.execute("SELECT MAX(id) FROM activity").fetchone()[0]
        actors = workspace.actors(conn)
        assert _asks_per_arm(
            conn,
            lambda c: operator_since._events_after(
                c, actor=actors["agent"], after_id=0
            ),
        ), "the read compared here is not the per-arm one"
        # The shipped limit last, so it is the one in force below.
        for limit in (3, operator_since.SCAN_LIMIT):
            monkeypatch.setattr(operator_since, "SCAN_LIMIT", limit)
            for after_id in range(0, head + 1):
                for who, actor in actors.items():
                    assert operator_since._events_after(
                        conn, actor=actor, after_id=after_id
                    ) == or_joined_events_after(conn, actor, after_id), (
                        who,
                        limit,
                        after_id,
                    )
        expected = {
            who: or_joined_events_after(conn, actor, 0) for who, actor in actors.items()
        }
    finally:
        conn.close()
    headers = {
        "admin": ADMIN,
        "member of the private project": as_user(workspace.mo["id"]),
        "non-member": as_user(workspace.ola["id"]),
        "agent": bearer(workspace.sol),
        "agent member of the private project": bearer(workspace.ari),
    }
    for who, header in headers.items():
        since = ok(workspace.client.get("/desk/since", headers=header))
        events, _capped = expected[who]
        assert since["examined_events"] == len(events), who
        assert since["last_shown_event_id"] == max(e["id"] for e in events), who


# --- the plans ---------------------------------------------------------------


class Recording:
    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.statements: list[tuple[str, list]] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, sql, params=()):
        self.statements.append((sql, list(params)))
        return self._inner.execute(sql, params)


def _plan(conn: sqlite3.Connection, read) -> list[str]:
    recording = Recording(conn)
    read(recording)
    trail = [s for s in recording.statements if "FROM activity a" in s[0]]
    assert len(trail) == 1, recording.statements
    sql, params = trail[0]
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _activity_steps(plan: list[str]) -> list[str]:
    return [step for step in plan if re.match(r"(SCAN|SEARCH) a\b", step)]


GATED = (
    "member of the private project",
    "non-member",
    "agent",
    "agent member of the private project",
    "anonymous",
)


#: What ANALYZE wrote for these indexes on the seeded 10,000-issue fleet
#: workspace (scripts/seed_benchmark.py --shape fleet). A test workspace is too
#: small for its own statistics to steer the planner as a real one's do, so
#: after its own ANALYZE these rows are put in their place.
FLEET_STATISTICS = {
    ("activity", "idx_activity_kind_id"): "65058 65058 1",
    ("activity", "idx_activity_actor"): "65058 9294 1",
    ("activity", "idx_activity_target"): "65058 65058 7 1",
    ("activity", "idx_activity_verb_window"): "65058 7229 276 276",
    ("issues", "idx_issues_status"): "10000 5000",
    ("issues", "idx_issues_archived"): "10000 371",
    ("issues", "idx_issues_project"): "10000 1429",
    ("issues", "idx_issues_assignee"): "10000 1429",
}


def _statistics(db_file, analyzed: bool) -> None:
    """Run ANALYZE when asked, as an operator might, and give the indexes the
    hot statements choose between the statistics a 10,000-issue workspace has.
    No Athena database has statistics unless someone runs ANALYZE; with them,
    SQLite plans from them, and the statements name their indexes so the plan
    does not change."""
    if not analyzed:
        return
    conn = db.connect(db_file)
    try:
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
    finally:
        conn.close()


ANALYZED = pytest.mark.parametrize(
    "analyzed", [False, True], ids=["no-statistics", "analyzed"]
)


@ANALYZED
def test_the_desks_newest_event_read_seeks_each_arm_and_scans_no_trail(
    workspace, analyzed
):
    for write in workspace.writes():
        write()
    _statistics(workspace.db_file, analyzed)
    conn = db.connect(workspace.db_file)
    try:
        actors = workspace.actors(conn)
        for who in GATED:
            plan = _plan(
                conn,
                lambda c, actor=actors[who]: desk.latest_visible_event_id(
                    c, actor=actor
                ),
            )
            # Not the OR resolved by gathering every row of every kind, not a
            # sort of the survivors, not a walk of the table: one seek per arm
            # down the (target_kind, id) index, newest first.
            assert not any("MULTI-INDEX OR" in step for step in plan), (who, plan)
            assert not any("TEMP B-TREE" in step for step in plan), (who, plan)
            assert (
                _activity_steps(plan)
                == ["SEARCH a USING INDEX idx_activity_kind_id (target_kind=?)"] * 4
            ), (who, plan)
        plan = _plan(
            conn, lambda c: desk.latest_visible_event_id(c, actor=actors["admin"])
        )
        # Ungated, the maximum is read from the end of the rowid index.
        assert _activity_steps(plan) == ["SEARCH a"], plan
    finally:
        conn.close()


@ANALYZED
def test_the_since_read_seeks_each_arm_from_the_cursor_and_scans_no_trail(
    workspace, analyzed
):
    for write in workspace.writes():
        write()
    _statistics(workspace.db_file, analyzed)
    conn = db.connect(workspace.db_file)
    try:
        actors = workspace.actors(conn)
        for who in GATED:
            plan = _plan(
                conn,
                lambda c, actor=actors[who]: operator_since._events_after(
                    c, actor=actor, after_id=0
                ),
            )
            assert not any("MULTI-INDEX OR" in step for step in plan), (who, plan)
            assert (
                _activity_steps(plan)
                == [
                    "SEARCH a USING INDEX idx_activity_kind_id (target_kind=? AND id>?)"
                ]
                * 4
            ), (who, plan)
        plan = _plan(
            conn,
            lambda c: operator_since._events_after(
                c, actor=actors["admin"], after_id=0
            ),
        )
        assert _activity_steps(plan) == [
            "SEARCH a USING INTEGER PRIMARY KEY (rowid>?)"
        ], plan
    finally:
        conn.close()


# --- the work does not grow with history -----------------------------------


def _trail(
    tmp_path, name: str, events: int, *, analyzed: bool = False
) -> tuple[sqlite3.Connection, dict]:
    """A member and ``events`` visible issue events, written raw for speed (as
    tests/test_activity_feed_scaling.py writes its trail)."""
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('m@e.com', 'M', 'member')"
    )
    conn.execute(
        "INSERT INTO projects (key, name, visibility, created_by, activity_scope_key)"
        " VALUES ('P', 'P', 'public', 1, '0123456789abcdef0123456789abcdef')"
    )
    conn.executemany(
        "INSERT INTO issues (title, status, project_id, created_by) "
        "VALUES (?, 'open', 1, 1)",
        [(f"issue {n}",) for n in range(events)],
    )
    conn.executemany(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail, "
        "created_at, visibility_restricted) "
        "VALUES (1, 'created', 'issue', ?, '', datetime('now'), 0)",
        [(row[0],) for row in conn.execute("SELECT id FROM issues")],
    )
    conn.commit()
    if analyzed:
        conn.execute("ANALYZE")
        conn.commit()
    member = dict(
        conn.execute("SELECT * FROM users WHERE email = 'm@e.com'").fetchone()
    )
    return conn, member


def _work(conn: sqlite3.Connection, read) -> int:
    """Virtual-machine work for one read, in units of 10 SQLite operations:
    deterministic, unlike a timing."""
    units = 0

    def tick():
        nonlocal units
        units += 1
        return 0

    conn.set_progress_handler(tick, 10)
    try:
        read()
    finally:
        conn.set_progress_handler(None, 0)
    return units


@ANALYZED
def test_the_work_of_both_reads_does_not_grow_with_the_trail(tmp_path, analyzed):
    """Eight times the history costs the same: before H1 the newest-event read
    grew with every event (it read them all), and so did the since read with no
    cursor (it sorted them all before taking the oldest 500). After ANALYZE,
    arms that did not name their index were planned as a walk of the rowid and
    a sort, which grew with the trail again."""
    small, member = _trail(tmp_path, "small.db", 600, analyzed=analyzed)
    large, _ = _trail(tmp_path, "large.db", 4_800, analyzed=analyzed)
    try:
        for read in (
            lambda c: desk.latest_visible_event_id(c, actor=member),
            lambda c: operator_since._events_after(c, actor=member, after_id=0),
        ):
            before = _work(small, lambda read=read: read(small))
            after = _work(large, lambda read=read: read(large))
            assert before > 0
            assert after / before <= 2.0, (before, after)
    finally:
        small.close()
        large.close()


def test_a_caller_whose_newest_events_are_hidden_gets_the_same_answer(tmp_path):
    """The condition H1 does not bound. Each arm walks newest first and stops at
    the caller's first visible event of its kind, so a caller whose newest
    events of a kind are all hidden walks past every one of them: here a member
    who sees the oldest tenth of the trail and nothing after it. That is what
    the OR-joined read cost too. This pins the answer for that caller, and that
    the per-arm read costs no more than the read it replaced."""
    conn, member = _trail(tmp_path, "quiet.db", 3_000)
    try:
        conn.execute(
            "INSERT INTO users (email, name, role) VALUES ('o@e.com', 'O', 'member')"
        )
        conn.execute(
            "INSERT INTO projects (key, name, visibility, created_by, "
            "activity_scope_key) SELECT 'Q', 'Q', 'private', id, "
            "'fedcba9876543210fedcba9876543210' FROM users WHERE email = 'o@e.com'"
        )
        hidden = conn.execute("SELECT id FROM projects WHERE key = 'Q'").fetchone()[0]
        conn.execute("UPDATE issues SET project_id = ? WHERE id > 300", (hidden,))
        conn.commit()
        newest = desk.latest_visible_event_id(conn, actor=member)
        assert newest == or_joined_latest(conn, member) == 300
        since = operator_since._events_after(conn, actor=member, after_id=0)
        assert since == or_joined_events_after(conn, member, 0)
        assert [event["id"] for event in since[0]] == list(range(1, 301))
        per_arm = _work(conn, lambda: desk.latest_visible_event_id(conn, actor=member))
        or_joined = _work(conn, lambda: or_joined_latest(conn, member))
        assert per_arm <= or_joined * 1.2, (per_arm, or_joined)
    finally:
        conn.close()
