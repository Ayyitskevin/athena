"""The ready rule visits open candidates only, and the desk asks it once (H3).

PERF-6, from the round-2 review: the ready rule read every issue that was not
archived, closed ones included, and ``claim-next`` did so holding the writer
lock (4.5, 35 and 156 ms at 1,000, 10,000 and 50,000 issues). The desk ran the
rule four times per read: a count and a page for the ready lane, a count and a
page for ``not_offered``.

The rule was amended twice on 2026-11-22 (decision records
``docs/decisions/2026-11-22-ready-rule-awaiting-decision.md`` and
``docs/decisions/2026-11-22-review-request-answers.md``) and its meaning must
not move: "open candidates only" changes which rows the statement visits,
never which rows it returns. The equality test proves that against the
statements as they stood before H3, kept here as the reference, on a workspace
with closed, archived, blocked, yielded, completed and review-requested issues,
custom statuses (one closed, one doing), a project where ``done`` is not
closed, and rows whose status their project does not list, for every kind of
caller, before and after each act that moves an issue between lanes. The plan
test holds the shape (principle 12), the work test its consequence, and the
count test that the desk asks the rule once.
"""

from __future__ import annotations

import re
import sqlite3
from unittest import mock

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    AppClock,
    add_user,
    as_user,
    bearer,
    bootstrap,
    lapse_lease,
    make_app,
    onboard_agent,
)

from athena.aegis import (
    desk,
    issue_query,
    issues,
    ready_claim_commands,
    ready_queue,
    status_commands,
    statuses,
)
from athena.core import access, db, work_query


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


# --- the rule's statements as they stood before H3 --------------------------


def _reference_base(conn, actor, now, *, awaiting: bool, project_id=None):
    category = statuses.category_sql(
        status_column="i.status", category_column="rps.category"
    )
    clauses, params = ready_queue._claimable_clauses(
        actor, now=now, category_expr=category
    )
    held, held_params = ready_queue.awaiting_answer_sql(conn, actor)
    clauses.append(held if awaiting else f"NOT {held}")
    params.extend(held_params)
    vis_sql, vis_params = ready_queue._visibility(conn, actor)
    base = (
        "FROM issues i LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN project_statuses rps "
        "ON rps.project_id = i.project_id AND rps.name = i.status "
        f"WHERE i.archived_at IS NULL AND {' AND '.join(clauses)}{vis_sql}"
    )
    params.extend(vis_params)
    if project_id is not None:
        base += " AND i.project_id = ?"
        params.append(project_id)
    return base, params


def reference_ready(conn, actor, now, limit, project_id=None) -> tuple[list[int], int]:
    base, params = _reference_base(
        conn, actor, now, awaiting=False, project_id=project_id
    )
    total = conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"]
    me = int(actor["id"])
    delegated = ready_queue._delegated_to_me_sql()
    rows = conn.execute(
        f"SELECT i.id {base} ORDER BY {delegated} DESC, "
        f"{issues.PRIORITY_RANK_SQL} DESC, i.created_at ASC, i.id ASC LIMIT ?",
        [*params, me, limit],
    ).fetchall()
    return [row["id"] for row in rows], total


def reference_not_offered(conn, actor, now, limit) -> tuple[list[int], int]:
    base, params = _reference_base(conn, actor, now, awaiting=True)
    total = conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"]
    rows = conn.execute(
        f"SELECT i.id {base} ORDER BY i.id ASC LIMIT ?", [*params, limit]
    ).fetchall()
    return [row["id"] for row in rows], total


def _ids(lane: dict) -> list[int]:
    return [item["issue"]["id"] for item in lane["items"]]


# --- the workspace ---------------------------------------------------------


class Workspace:
    """An operator (the admin), two agents Bo and Rex, a human member Mo of the
    private project, a human non-member Ola, and issues in every state the
    rule has a clause for. ``moves`` are the acts that carry an issue from one
    lane to another."""

    def __init__(self, client: TestClient, db_file, clock: AppClock):
        self.client = client
        self.db_file = db_file
        self.clock = clock
        bootstrap(client, email="admin@e.com", name="Admin")
        self.mo = add_user(client, "mo@e.com", name="Mo")
        self.ola = add_user(client, "ola@e.com", name="Ola")
        self.bo = onboard_agent(client, email="bo@e.com", name="Bo")
        self.rex = onboard_agent(client, email="rex@e.com", name="Rex")
        self.pub = self.project("Public", "PUB", "public")
        self.prv = self.project("Private", "PRV", "private")
        self.cus = self.project("Custom", "CUS", "public")
        self.dne = self.project("Done open", "DNE", "public")
        ok(
            client.post(
                f"/projects/{self.prv}/members",
                json={"user_id": self.mo["id"]},
                headers=ADMIN,
            ),
            201,
        )
        for name, category in (("review", "doing"), ("shipped", "done")):
            ok(
                client.post(
                    f"/projects/{self.cus}/statuses",
                    json={"name": name, "category": category},
                    headers=ADMIN,
                ),
                201,
            )
        self.made: dict[str, int] = {}
        self.candidates: list[int] = []
        self._build()

    # helpers -------------------------------------------------------------

    def project(self, name: str, key: str, visibility: str) -> int:
        return ok(
            self.client.post(
                "/projects",
                json={"name": name, "key": key, "visibility": visibility},
                headers=ADMIN,
            ),
            201,
        )["id"]

    def issue(self, name: str, project: int | None, *, to=(), **fields) -> int:
        made = ok(
            self.client.post(
                "/issues",
                json={"title": name, "project_id": project, **fields},
                headers=ADMIN,
            ),
            201,
        )["id"]
        for who in to:
            ok(
                self.client.post(
                    f"/issues/{made}/delegate",
                    json={"user_id": self.user_id(who)},
                    headers=ADMIN,
                ),
                201,
            )
        self.made[name] = made
        return made

    def user_id(self, who) -> int:
        return who["user"]["id"] if "user" in who else who["id"]

    def headers(self, who) -> dict:
        return bearer(who) if "user" in who else as_user(who["id"])

    def assign(self, name: str, who) -> None:
        ok(
            self.client.put(
                f"/issues/{self.made[name]}/assignee",
                json={"assignee_id": self.user_id(who)},
                headers=ADMIN,
            )
        )

    def patch(self, name: str, **fields) -> None:
        ok(self.client.patch(f"/issues/{self.made[name]}", json=fields, headers=ADMIN))

    def claim(self, who, name: str, **body) -> dict:
        issue_id = self.made[name]
        tag = self.client.get(f"/issues/{issue_id}", headers=self.headers(who))
        return ok(
            self.client.post(
                f"/issues/{issue_id}/claim",
                json=body,
                headers={**self.headers(who), "If-Match": tag.headers["ETag"]},
            ),
            201,
        )

    def complete(self, who, name: str, **report) -> dict:
        lease = self.claim(who, name)
        return ok(
            self.client.post(
                f"/issues/{self.made[name]}/complete",
                json={"generation": lease["generation"], **report},
                headers=self.headers(who),
            )
        )

    def yield_(self, who, name: str, reason: str) -> None:
        lease = self.claim(who, name)
        ok(
            self.client.post(
                f"/issues/{self.made[name]}/yield",
                json={
                    "generation": lease["generation"],
                    "reason": reason,
                    "attempted_work": "Reproduced it.",
                    "evidence": ["the case reproduces"],
                    "blocking_question": "Which path?",
                    "resume_instructions": "Choose, then rerun.",
                },
                headers=self.headers(who),
            ),
            201,
        )

    def comment(self, name: str, headers=ADMIN) -> None:
        ok(
            self.client.post(
                f"/issues/{self.made[name]}/comments",
                json={"body": "An answer."},
                headers=headers,
            ),
            201,
        )

    def link(self, blocked: str, blocker: str) -> None:
        ok(
            self.client.post(
                f"/issues/{self.made[blocked]}/links",
                json={"target_ref": str(self.made[blocker]), "relation": "blocked_by"},
                headers=ADMIN,
            ),
            201,
        )

    def sql(self, statement: str, params=()) -> None:
        conn = db.connect(self.db_file)
        try:
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

    def submit(self, build: str, revision: str) -> None:
        previous = self.candidates[-1] if self.candidates else None
        done = self.complete(
            self.bo,
            build,
            verdict="PASS",
            candidate_submission={
                "identity": {"kind": "git", "source": "w/x", "revision": revision},
                "expected_previous_candidate_id": previous,
            },
        )
        self.candidates.append(done["candidate_submission"]["candidate_id"])

    def request_review(self, review: str, reviewer) -> None:
        ok(
            self.client.post(
                f"/issues/{self.made[review]}/review-requests",
                json={
                    "candidate_id": self.candidates[-1],
                    "reviewer_id": self.user_id(reviewer),
                },
                headers=ADMIN,
            )
        )

    # the issues ----------------------------------------------------------

    def _build(self) -> None:
        bo, rex, mo = self.bo, self.rex, self.mo
        self.issue("plain", self.pub, to=[bo])
        self.issue("backlog plain", None, to=[bo, rex])
        self.issue("urgent", self.pub, to=[rex], priority="urgent")
        self.issue("going", self.pub, to=[bo])
        self.patch("going", status="in_progress")
        self.issue("mine by assignment", self.pub)
        self.assign("mine by assignment", bo)
        self.issue("closed", self.pub, to=[bo])
        self.patch("closed", status="done")
        self.issue("archived open", self.pub, to=[bo])
        ok(
            self.client.post(
                f"/issues/{self.made['archived open']}/archive", headers=ADMIN
            )
        )
        self.issue("archived closed", self.pub, to=[bo])
        self.patch("archived closed", status="done")
        ok(
            self.client.post(
                f"/issues/{self.made['archived closed']}/archive", headers=ADMIN
            )
        )
        # Blocked while its blocker is open; the blocked one is in progress, so
        # a narrowing to status 'open' would lose it once it is ready.
        self.issue("blocker", self.pub, to=[rex])
        self.issue("blocked", self.pub, to=[bo])
        self.patch("blocked", status="in_progress")
        self.link("blocked", "blocker")
        self.issue("closed blocker", self.pub)
        self.patch("closed blocker", status="done")
        self.issue("unblocked", self.pub, to=[bo])
        self.link("unblocked", "closed blocker")
        self.issue("asks", self.pub, to=[bo, rex])
        self.yield_(bo, "asks", "needs_input")
        self.issue("stuck", self.pub, to=[bo])
        self.yield_(bo, "stuck", "blocked")
        self.issue("handed back", self.pub, to=[bo, rex])
        self.complete(bo, "handed back")
        self.issue("answered", self.pub, to=[bo])
        self.complete(bo, "answered")
        self.comment("answered")
        self.issue("held", self.pub, to=[rex])
        self.claim(rex, "held")
        self.issue("lapsed", self.pub, to=[bo])
        # The shortest lease there is: the clock passes it and leaves Rex's
        # hold live.
        self.claim(bo, "lapsed", lease_seconds=60)
        lapse_lease(self.clock, self.db_file, self.made["lapsed"])
        self.issue("private", self.prv)
        self.assign("private", mo)
        self.issue("private for bo", self.prv)
        self.assign("private for bo", bo)
        self.issue("in review", self.cus, to=[bo])
        self.patch("in review", status="review")
        self.issue("shipped", self.cus, to=[bo])
        self.patch("shipped", status="shipped")
        self.issue("done but open", self.dne, to=[bo])
        # Review requests (D16): Rex holds the review issue, then a newer
        # request names Rex again.
        self.issue("build", self.cus, to=[bo])
        self.issue("review issue", self.cus, to=[rex])
        self.submit("build", "a" * 40)
        self.request_review("review issue", rex)
        self.complete(
            rex, "review issue", verdict="HOLD", review_candidate_id=self.candidates[-1]
        )
        # Rows whose status their project does not list: written around the
        # commands, as an import or an old database can leave them. The
        # category of each falls to the neutral 'doing'.
        self.issue("odd status", self.cus, to=[bo])
        self.sql(
            "UPDATE issues SET status = 'Mystery' WHERE id = ?",
            (self.made["odd status"],),
        )
        self.issue("backlog shipped", None, to=[bo])
        self.sql(
            "UPDATE issues SET status = 'shipped' WHERE id = ?",
            (self.made["backlog shipped"],),
        )
        self.issue("Done cased", None, to=[bo])
        self.sql(
            "UPDATE issues SET status = 'Done' WHERE id = ?", (self.made["Done cased"],)
        )

    def moves(self):
        """Acts that carry issues between lanes, each followed by a comparison."""
        return [
            ("the blocker closes", lambda: self.patch("blocker", status="done")),
            ("another account answers", lambda: self.comment("asks")),
            (
                "a new candidate, and a request naming the holder",
                lambda: (
                    self.submit("build", "b" * 40),
                    self.request_review("review issue", self.rex),
                ),
            ),
            (
                "a project makes 'done' a doing status",
                lambda: self.sql(
                    "UPDATE project_statuses SET category = 'doing' "
                    "WHERE project_id = ? AND name = 'done'",
                    (self.dne,),
                ),
            ),
            (
                "an issue there takes 'done', open in that project",
                lambda: self.patch("done but open", status="done"),
            ),
            (
                "an empty project makes 'done' a to-do status, through its routes",
                self._side_project,
            ),
            (
                "an issue there takes 'done', a to-do status in that project",
                lambda: self.patch("side issue", status="done"),
            ),
            ("a ready issue closes", lambda: self.patch("plain", status="done")),
            (
                "a ready issue is archived",
                lambda: ok(
                    self.client.post(
                        f"/issues/{self.made['urgent']}/archive", headers=ADMIN
                    )
                ),
            ),
        ]

    def _side_project(self) -> None:
        side = self.project("Side", "SDE", "public")
        ok(
            self.client.delete(f"/projects/{side}/statuses/done", headers=ADMIN),
        )
        ok(
            self.client.post(
                f"/projects/{side}/statuses",
                json={"name": "done", "category": "todo"},
                headers=ADMIN,
            ),
            201,
        )
        self.issue("side issue", side, to=[self.bo])

    def actors(self, conn) -> dict[str, dict]:
        def user(user_id: int) -> dict:
            return dict(
                conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            )

        return {
            "admin": user(1),
            "agent Bo": user(self.bo["user"]["id"]),
            "agent Rex": user(self.rex["user"]["id"]),
            "member Mo": user(self.mo["id"]),
            "non-member Ola": user(self.ola["id"]),
        }


@pytest.fixture
def workspace(clock, tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        yield Workspace(client, db_file, clock)


def _compare(workspace: Workspace, step: str) -> set[tuple]:
    """Every lane read against the reference, for every caller; returns the
    (caller, lane, ids) seen, so the test can show the workspace exercised
    the lanes."""
    seen: set[tuple] = set()
    # The lease clock is the app's: the workspace holds a live lease and a
    # lapsed one.
    now = workspace.clock.now()
    conn = db.connect(workspace.db_file)
    try:
        for who, actor in workspace.actors(conn).items():
            for limit in (1, 2, ready_queue.READY_LIMIT, ready_queue.MAX_READY_LIMIT):
                expected = reference_ready(conn, actor, now, limit)
                lane = ready_queue.build_ready(conn, actor=actor, now=now, limit=limit)
                assert (_ids(lane), lane["total"]) == expected, (step, who, limit)
                held = reference_not_offered(conn, actor, now, limit)
                listed = ready_queue.build_not_offered(
                    conn, actor=actor, now=now, limit=limit
                )
                assert (_ids(listed), listed["total"]) == held, (step, who, limit)
                both = ready_queue.build_lanes(
                    conn, actor=actor, now=now, limit=limit, not_offered_limit=limit
                )
                assert both == (lane, listed), (step, who, limit)
                seen.add((who, "ready", tuple(expected[0])))
                seen.add((who, "held", tuple(held[0])))
            for project in (workspace.pub, workspace.cus, workspace.prv):
                expected = reference_ready(conn, actor, now, 50, project_id=project)
                lane = ready_queue.build_ready(
                    conn, actor=actor, now=now, limit=50, project_id=project
                )
                assert (_ids(lane), lane["total"]) == expected, (step, who, project)
            # The query atom, and its negation, against the atom read without
            # the candidates (every issue, as before H3).
            for text in ("is:ready", "-is:ready", "is:ready priority:urgent"):
                ids = _query_ids(conn, actor, text)
                with mock.patch.object(
                    ready_queue,
                    "_open_candidates_sql",
                    lambda _conn: ("SELECT id FROM issues", []),
                ):
                    assert ids == _query_ids(conn, actor, text), (step, who, text)
    finally:
        conn.close()
    return seen


def _query_ids(conn, actor, text: str) -> list[int]:
    rows = issue_query.run_query(
        conn,
        work_query.parse(text),
        actor=actor,
        visible_project_ids=access.visible_project_filter(conn, actor),
        limit=500,
    )
    return [row["id"] for row in rows]


def test_the_rule_returns_the_same_rows_for_every_caller_before_and_after_each_move(
    workspace,
):
    seen = _compare(workspace, "as built")

    def ready_for(who: str) -> set[str]:
        names = {issue_id: name for name, issue_id in workspace.made.items()}
        lanes = [
            ids for (caller, lane, ids) in seen if caller == who and lane == "ready"
        ]
        return {names[i] for ids in lanes for i in ids}

    # The workspace puts rows where the rule must find them: ready in progress,
    # in a custom doing status, under a status the project does not list, a
    # lapsed lease, a blocked yield; and held back for Bo alone.
    assert {
        "going",
        "in review",
        "odd status",
        "backlog shipped",
        "Done cased",
        "lapsed",
        "stuck",
        "unblocked",
        "answered",
    } <= ready_for("agent Bo")
    assert not {"closed", "shipped", "blocked", "asks", "held"} & ready_for("admin")
    for step, move in workspace.moves():
        move()
        seen |= _compare(workspace, step)
    assert {"blocked", "asks", "review issue"} <= ready_for("agent Rex") | ready_for(
        "agent Bo"
    )
    assert {"done but open", "side issue"} <= ready_for("agent Bo")
    _routes_serve_the_reference(workspace)


def _routes_serve_the_reference(workspace: Workspace) -> None:
    """Through the routes: the desk's two lanes, the ready read and pickup."""
    conn = db.connect(workspace.db_file)
    try:
        actors = workspace.actors(conn)
        now = workspace.clock.now()
        expected = {
            who: (
                reference_ready(conn, actors[who], now, ready_queue.READY_LIMIT),
                reference_not_offered(conn, actors[who], now, 20),
            )
            for who in actors
        }
    finally:
        conn.close()
    for who, cast in (
        ("admin", {"id": 1}),
        ("agent Bo", workspace.bo),
        ("agent Rex", workspace.rex),
        ("member Mo", workspace.mo),
        ("non-member Ola", workspace.ola),
    ):
        headers = workspace.headers(cast) if who != "admin" else ADMIN
        work = ok(workspace.client.get("/desk", headers=headers))["work"]
        ready, held = expected[who]
        assert (_ids(work["ready"]), work["ready"]["total"]) == ready, who
        not_offered = work["waiting_on_operator"]["not_offered"]
        assert (_ids(not_offered), not_offered["total"]) == held, who
        lane = ok(workspace.client.get("/desk/ready", headers=headers))
        assert (_ids(lane), lane["total"]) == ready, who
    picked = ok(
        workspace.client.post("/issues/claim-next", headers=bearer(workspace.bo))
    )
    assert picked["claimed"] and picked["issue"]["id"] == expected["agent Bo"][0][0][0]


# --- the plan ----------------------------------------------------------------


class Recording:
    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.statements: list[tuple[str, list]] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, sql, params=()):
        self.statements.append((sql, list(params)))
        return self._inner.execute(sql, params)


#: Every evaluation of the rule reads the newest settlement per candidate.
RULE_MARK = "rqs.verb IN"


def _rule_statements(recording: Recording) -> list[tuple[str, list]]:
    return [(sql, params) for sql, params in recording.statements if RULE_MARK in sql]


def _issue_steps(plan: list[str]) -> list[str]:
    return [step for step in plan if re.match(r"(SCAN|SEARCH) (i|issues)\b", step)]


def _assert_open_candidates_only(conn, sql: str, params: list) -> None:
    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    steps = _issue_steps(plan)
    # Never a walk of the issues, nor of every unarchived issue: the candidates
    # come from reads of the status index between the closed names (and of
    # the project index for a project that reopens one), and each issue is
    # reached by its key.
    assert not [step for step in steps if step.startswith("SCAN")], plan
    candidates = [step for step in steps if step.startswith("SEARCH issues")]
    assert "SEARCH issues USING COVERING INDEX idx_issues_status (status<?)" in (
        candidates
    ), plan
    assert all(
        "idx_issues_status (status" in step or "idx_issues_project (project_id" in step
        for step in candidates
    ), plan
    for step in steps:
        if step.startswith("SEARCH i "):
            assert "rowid=?" in step, plan
    assert "done" in params


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


@pytest.mark.parametrize("analyzed", [False, True], ids=["no-statistics", "analyzed"])
def test_the_rule_reads_open_candidates_only_on_the_desk_and_on_pickup(
    workspace, analyzed
):
    _statistics(workspace.db_file, analyzed)
    conn = db.connect(workspace.db_file)
    try:
        actors = workspace.actors(conn)
        for who in ("admin", "agent Bo", "member Mo"):
            recording = Recording(conn)
            ready_queue.build_ready(
                recording, actor=actors[who], now=workspace.clock.now()
            )
            statements = _rule_statements(recording)
            assert statements, recording.statements
            for statement in statements:
                _assert_open_candidates_only(conn, *statement)
    finally:
        conn.close()
    # Pickup selects under the writer lock with the same statement.
    conn = db.connect(workspace.db_file)
    try:
        bo = dict(
            conn.execute(
                "SELECT * FROM users WHERE id = ?", (workspace.bo["user"]["id"],)
            ).fetchone()
        )
        bo["_token_scopes"] = frozenset({"read", "issue:write"})
        recording = Recording(conn)
        picked = ready_claim_commands.claim_next_ready(recording, actor=bo)
        assert picked["claimed"], picked
        statements = _rule_statements(recording)
        assert statements, recording.statements
        for statement in statements:
            _assert_open_candidates_only(conn, *statement)
    finally:
        conn.close()


def test_the_desk_asks_the_ready_rule_once(workspace):
    """The ready lane, its total, the not-offered list and its total: one
    evaluation. Before H3, four."""
    conn = db.connect(workspace.db_file)
    try:
        for who, actor in workspace.actors(conn).items():
            recording = Recording(conn)
            desk.build_desk(recording, actor=actor, now=workspace.clock.now())
            assert len(_rule_statements(recording)) == 1, who
    finally:
        conn.close()


# --- the work does not grow with closed history ----------------------------


def _history(
    tmp_path, name: str, closed: int, *, closed_as: str = "done", analyzed=False
) -> tuple[sqlite3.Connection, dict]:
    """An agent with five open issues assigned in a public project and
    ``closed`` closed ones there under ``closed_as``, written raw for speed
    (the rule reads the rows, not how they were made)."""
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) "
        "VALUES ('bo@e.com', 'Bo', 'member', 1)"
    )
    agent = dict(
        conn.execute("SELECT * FROM users WHERE email = 'bo@e.com'").fetchone()
    )
    conn.execute(
        "INSERT INTO projects (key, name, visibility, created_by, activity_scope_key)"
        " VALUES ('P', 'P', 'public', 1, '0123456789abcdef0123456789abcdef')"
    )
    project = conn.execute("SELECT id FROM projects").fetchone()[0]
    statuses.seed_defaults(conn, project, commit=False)
    if closed_as != "done":
        conn.execute(
            "INSERT INTO project_statuses (project_id, name, category, position) "
            "VALUES (?, ?, 'done', 9)",
            (project, closed_as),
        )
    conn.executemany(
        "INSERT INTO issues (title, status, created_by, assignee_id, project_id) "
        "VALUES (?, ?, 1, ?, ?)",
        [(f"closed {n}", closed_as, agent["id"], project) for n in range(closed)]
        + [(f"open {n}", "open", agent["id"], project) for n in range(5)],
    )
    conn.commit()
    if analyzed:
        conn.execute("ANALYZE")
        conn.commit()
    return conn, agent


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


def _atom(conn, actor) -> list[dict]:
    return issue_query.run_query(
        conn,
        work_query.parse("is:ready"),
        actor=actor,
        visible_project_ids=access.visible_project_filter(conn, actor),
        limit=50,
    )


@pytest.mark.parametrize("analyzed", [False, True], ids=["no-statistics", "analyzed"])
def test_the_rules_work_does_not_grow_with_closed_history(tmp_path, analyzed):
    """Sixteen times the closed history costs the same, for the desk's lane,
    pickup's lane and the query atom alike. Before H3 all three read every
    unarchived issue."""
    small, agent = _history(tmp_path, "small.db", 500, analyzed=analyzed)
    large, _ = _history(tmp_path, "large.db", 8_000, analyzed=analyzed)
    try:
        for read in (
            lambda c: ready_queue.build_ready(c, actor=agent),
            lambda c: _atom(c, agent),
        ):
            before = _work(small, lambda read=read: read(small))
            after = _work(large, lambda read=read: read(large))
            assert before > 0
            assert after / before <= 1.5, (before, after)
        assert ready_queue.build_ready(large, actor=agent)["total"] == 5
        assert len(_atom(large, agent)) == 5
    finally:
        small.close()
        large.close()


def test_a_project_that_reopens_done_does_not_switch_the_skip_off(tmp_path):
    """Closed-ness is decided per project. A project whose menu makes ``done``
    a to-do status (through the status commands, as an operator would) is read
    through its own issues; every other project's closed issues stay unread.
    Before, one such project anywhere made the rule read every issue."""
    conn, agent = _history(tmp_path, "reopen.db", 8_000)
    try:
        before = _work(conn, lambda: ready_queue.build_ready(conn, actor=agent))
        admin = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
        conn.execute(
            "INSERT INTO projects (key, name, visibility, created_by, "
            "activity_scope_key) VALUES ('S', 'S', 'public', 1, "
            "'fedcba9876543210fedcba9876543210')"
        )
        side = conn.execute("SELECT id FROM projects WHERE key = 'S'").fetchone()[0]
        statuses.seed_defaults(conn, side, commit=True)
        status_commands.remove_status(conn, actor=admin, project_id=side, name="done")
        status_commands.add_status(
            conn, actor=admin, project_id=side, name="done", category="todo"
        )
        after = _work(conn, lambda: ready_queue.build_ready(conn, actor=agent))
        assert ready_queue.build_ready(conn, actor=agent)["total"] == 5
        # One more read (the reopening project's own issues: none), a fixed
        # cost that does not grow with the 8,000 closed issues.
        assert after - before <= 150, (before, after)
        with mock.patch.object(
            ready_queue,
            "_open_candidates_sql",
            lambda _conn: ("SELECT id FROM issues", []),
        ):
            every_issue = _work(
                conn, lambda: ready_queue.build_ready(conn, actor=agent)
            )
        assert after * 10 < every_issue, (after, every_issue)
    finally:
        conn.close()


def test_a_projects_own_closed_status_is_read_by_its_index_entry_only(tmp_path):
    """The condition that remains. An issue closed under a project's own closed
    status (``shipped``) cannot be skipped by name: the same name on an issue
    in the backlog, or in a project that does not list it, is open (the neutral
    ``doing``), and no index finds those apart from the closed ones without a
    migration. So each is read once, its index entry and its project, and
    dropped before the rule runs on it: a small fixed cost per issue, where
    before H3 the whole rule ran on it."""
    done, agent = _history(tmp_path, "done.db", 8_000)
    shipped, _ = _history(tmp_path, "shipped.db", 8_000, closed_as="shipped")
    try:
        as_done = _work(done, lambda: ready_queue.build_ready(done, actor=agent))
        as_shipped = _work(
            shipped, lambda: ready_queue.build_ready(shipped, actor=agent)
        )
        assert ready_queue.build_ready(shipped, actor=agent)["total"] == 5
        # At most three units (30 operations) per closed issue.
        assert as_shipped - as_done <= 3 * 8_000, (as_done, as_shipped)
        with mock.patch.object(
            ready_queue,
            "_open_candidates_sql",
            lambda _conn: ("SELECT id FROM issues", []),
        ):
            every_issue = _work(
                shipped, lambda: ready_queue.build_ready(shipped, actor=agent)
            )
        assert as_shipped * 3 < every_issue, (as_shipped, every_issue)
    finally:
        done.close()
        shipped.close()


@pytest.mark.parametrize("limit", [0, -1, ready_queue.MAX_READY_LIMIT + 1, True])
def test_every_lane_refuses_a_limit_that_returns_no_rows(tmp_path, limit):
    """A lane's total is read from the rows its limit returns, so a limit that
    returns none would answer a total of 0 (dd594b1 counted separately and
    answered the true total). No product caller passes one; each lane refuses
    it as the ready lane always has."""
    conn, agent = _history(tmp_path, "limits.db", 0)
    try:
        for read in (
            lambda: ready_queue.build_ready(conn, actor=agent, limit=limit),
            lambda: ready_queue.build_not_offered(conn, actor=agent, limit=limit),
            lambda: ready_queue.build_lanes(conn, actor=agent, limit=limit),
            lambda: ready_queue.build_lanes(conn, actor=agent, not_offered_limit=limit),
        ):
            with pytest.raises(ValueError):
                read()
    finally:
        conn.close()
