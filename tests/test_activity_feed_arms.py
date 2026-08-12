"""The gated feed reads one page per visibility arm, and reads exactly what it used to.

`core/access.event_visibility_clause` is an OR across four disjoint target-kind arms.
Asking SQLite that OR with an ORDER BY / LIMIT on top is not answerable incrementally:
with no ANALYZE stats — which is every real Athena database, since nothing in the
product runs ANALYZE — the planner unions the arms, evaluates the correlated
membership subqueries, and sorts every survivor through a temp B-tree before the LIMIT
applies. So `core/activity._paged_feed_sql` asks each arm for its own bounded page and
merges them.

That is an ACCESS-PATTERN change, not a policy change, and these tests hold it to
exactly that: the predicate is still assembled from the same arms (byte-for-byte), and
the paged reads still return the same rows in the same order as the single-statement
form they replaced — proven against a reference implementation of the OLD shape, over a
mixed public/private fixture, across the filter matrix.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issues, projects
from athena.core import access, activity, db, users
from athena.mentor import pages, spaces


def _plan(conn: sqlite3.Connection, sql: str, params=()) -> str:
    return " | ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )


def _reference_page(
    conn, actor, *, direction="DESC", limit=50, **filters
) -> list[dict]:
    """The read as it was written BEFORE the per-arm split: one statement, the whole
    OR in the WHERE, ordered and limited on top. The behavioral oracle these tests
    compare against — if the merged read ever disagrees with this, the change stopped
    being an access-pattern change."""
    select = (
        "SELECT a.id, a.actor_id, a.verb, a.target_kind, a.target_id, a.detail, "
        "a.created_at, a.run_id, a.parent_run_id, a.forked_from_event_id, "
        "a.imported_at, a.reverses_event_id, u.name AS actor_name FROM activity a "
        "JOIN users u ON u.id = a.actor_id"
    )
    clauses: list[str] = []
    params: list = []
    gate, gate_params = access.event_visibility_clause(conn, actor, alias="a")
    if gate:
        clauses.append(gate)
        params.extend(gate_params)
    for column, value in (
        ("a.target_kind", filters.get("target_kind")),
        ("a.verb", filters.get("verb")),
        ("a.actor_id", filters.get("actor_id")),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if filters.get("before_id") is not None:
        clauses.append("a.id < ?")
        params.append(filters["before_id"])
    if filters.get("after_id") is not None:
        clauses.append("a.id > ?")
        params.append(filters["after_id"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"{select}{where} ORDER BY a.id {direction} LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def _mixed_fixture(tmp_path):
    """A trail spanning every arm, half of it behind private containers: public and
    private projects, a backlog issue (no project to gate on), public and private
    spaces with pages in each, and events on issues, pages, spaces and projects."""
    conn = db.connect(tmp_path / "feed.db")
    db.migrate(conn)
    admin = users.create_user(conn, email="a@e.com", name="Admin", role="admin")
    owner = users.create_user(conn, email="o@e.com", name="Owner")
    member = users.create_user(conn, email="m@e.com", name="Member")
    outsider = users.create_user(conn, email="x@e.com", name="Outsider")

    pub = projects.create_project(conn, name="Pub", key="PUB", created_by=owner["id"])
    priv = projects.create_project(conn, name="Priv", key="PRV", created_by=owner["id"])
    conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (priv["id"],))
    access.add_project_member(conn, priv["id"], member["id"], owner["id"])

    pub_space = spaces.create_space(conn, key="PS", name="Pub", created_by=owner["id"])
    priv_space = spaces.create_space(
        conn, key="RS", name="Priv", created_by=owner["id"]
    )
    conn.execute(
        "UPDATE spaces SET visibility='private' WHERE id=?", (priv_space["id"],)
    )
    access.add_space_member(conn, priv_space["id"], member["id"], owner["id"])
    conn.commit()

    targets: list[tuple[str, int]] = []
    for project in (pub, priv, None):
        for n in range(4):
            issue = issues.create_issue(
                conn,
                title=f"I{n}",
                body="b",
                created_by=owner["id"],
                project_id=project["id"] if project else None,
            )
            targets.append(("issue", issue["id"]))
    for space in (pub_space, priv_space):
        for n in range(3):
            page = pages.create_page(
                conn, space_id=space["id"], title=f"P{n}", created_by=owner["id"]
            )
            targets.append(("page", page["id"]))
        targets.append(("space", space["id"]))
    targets.append(("project", pub["id"]))
    targets.append(("project", priv["id"]))

    verbs = ("issue_created", "issue_updated", "page_edited")
    for i, (kind, target_id) in enumerate(targets * 3):
        activity.record(
            conn,
            actor_id=(owner, member, admin)[i % 3]["id"],
            verb=verbs[i % len(verbs)],
            target_kind=kind,
            target_id=target_id,
            detail=f"d{i}",
        )
    return conn, {"admin": admin, "owner": owner, "member": member, "out": outsider}


def test_clause_is_exactly_its_arms_or_joined(tmp_path):
    """The single predicate and the arms are one authority, not two that agree today.

    Every non-paged caller (undo's event-by-id probe, the export gate) still uses the
    joined clause, so the split must not have quietly reworded it."""
    conn, who = _mixed_fixture(tmp_path)
    for actor in (None, who["member"], who["out"]):
        arms = access.event_visibility_arms(conn, actor, alias="a")
        assert arms is not None
        joined = "((" + ") OR (".join(clause for _, clause, _ in arms) + "))"
        joined_params = [p for _, _, arm_params in arms for p in arm_params]
        clause, params = access.event_visibility_clause(conn, actor, alias="a")
        assert clause == joined
        assert params == joined_params
    # An admin is "no gate", which must stay distinguishable from "a gate admitting
    # nothing" — collapsing the two would show an admin an empty feed.
    assert access.event_visibility_arms(conn, who["admin"]) is None
    assert access.event_visibility_clause(conn, who["admin"]) == ("", [])


def test_arms_cover_exactly_the_visible_target_kinds(tmp_path):
    conn, who = _mixed_fixture(tmp_path)
    arms = access.event_visibility_arms(conn, who["member"], alias="a")
    assert arms is not None
    assert [kind for kind, _, _ in arms] == ["issue", "page", "space", "project"]


def test_paged_feed_matches_the_single_statement_form(tmp_path):
    """The merged read returns the same rows in the same order as the OR-joined read.

    This is the acceptance criterion for the whole change: same rows, same order, for
    a mixed public/private fixture, across actors and filters."""
    conn, who = _mixed_fixture(tmp_path)
    actors = [None, who["member"], who["out"], who["owner"], who["admin"]]
    filter_sets: list[dict] = [
        {},
        {"target_kind": "issue"},
        {"target_kind": "page"},
        {"target_kind": "space"},
        {"target_kind": "project"},
        {"verb": "issue_created"},
        {"verb": "page_edited"},
        {"actor_id": who["owner"]["id"]},
        {"before_id": 40},
        {"verb": "issue_updated", "before_id": 60},
    ]
    for actor in actors:
        for filters in filter_sets:
            for limit in (1, 5, 50, 500):
                expected = _reference_page(
                    conn, actor, direction="DESC", limit=limit, **filters
                )
                got = activity.list_activity(conn, limit=limit, actor=actor, **filters)
                assert got == expected, (actor, filters, limit)


def test_forward_event_stream_matches_the_single_statement_form(tmp_path):
    """list_events walks the same index the other way; the cursor contract is exact."""
    conn, who = _mixed_fixture(tmp_path)
    for actor in (None, who["member"], who["out"], who["admin"]):
        for filters in ({}, {"target_kind": "issue"}, {"verb": "issue_created"}):
            for after_id in (0, 25, 10**9):
                expected = _reference_page(
                    conn, actor, direction="ASC", limit=25, after_id=after_id, **filters
                )
                got = activity.list_events(
                    conn, after_id=after_id, limit=25, actor=actor, **filters
                )
                assert got == expected, (actor, filters, after_id)


def test_cursor_paging_walks_the_whole_visible_trail_without_gaps(tmp_path):
    """Paging is where a per-arm LIMIT could silently drop rows: each arm returns its
    own newest N, so a page boundary must not lose a row that sat just behind another
    arm's cut. Walk the entire feed one small page at a time and compare to the whole
    visible trail read at once."""
    conn, who = _mixed_fixture(tmp_path)
    for actor in (who["member"], who["out"], None):
        everything = [
            e["id"] for e in activity.list_activity(conn, limit=10_000, actor=actor)
        ]
        walked: list[int] = []
        cursor = None
        while True:
            page = activity.list_activity(conn, limit=3, actor=actor, before_id=cursor)
            if not page:
                break
            walked.extend(e["id"] for e in page)
            cursor = page[-1]["id"]
        assert walked == everything
        assert len(walked) == len(set(walked))  # no row served twice


def test_unreachable_target_kind_returns_empty_without_scanning(tmp_path):
    """No arm covers 'run', so a non-admin asking for run events sees an empty feed —
    the same answer the OR gave, reached without touching the table."""
    conn, who = _mixed_fixture(tmp_path)
    assert (
        activity.list_activity(conn, limit=50, actor=who["member"], target_kind="run")
        == []
    )
    assert (
        activity.list_events(
            conn, after_id=0, limit=50, actor=who["out"], target_kind="run"
        )
        == []
    )
    # An admin has no gate, so the same request is a plain (empty) read, not a refusal.
    assert (
        activity.list_activity(conn, limit=50, actor=who["admin"], target_kind="run")
        == []
    )


def test_kind_id_index_exists_and_each_arm_seeks_it(tmp_path):
    """The behavioral intent of migration 0076: every arm SEEKS by (target_kind, id)
    and none of them falls back to scanning the trail. Pinned as a plan assertion, on
    a database with no ANALYZE statistics — the state every real deployment is in."""
    conn, who = _mixed_fixture(tmp_path)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity'"
        )
    }
    assert "idx_activity_kind_id" in names

    arms = access.event_visibility_arms(conn, who["member"], alias="a")
    assert arms is not None
    for kind, clause, params in arms:
        sql = (
            "SELECT a.id FROM activity a JOIN users u ON u.id = a.actor_id "
            f"WHERE ({clause}) ORDER BY a.id DESC LIMIT 50"
        )
        plan = _plan(conn, sql, params)
        assert "idx_activity_kind_id" in plan, (kind, plan)
        assert "SCAN a" not in plan, (kind, plan)
        # The whole point: no arm materializes-and-sorts before its LIMIT.
        assert "USE TEMP B-TREE FOR ORDER BY" not in plan, (kind, plan)


def test_gated_feed_no_longer_plans_a_multi_index_or(tmp_path):
    """The old shape's signature — MULTI-INDEX OR over the four arms, then a temp
    B-tree over every survivor — must not come back for the real paged read."""
    conn, who = _mixed_fixture(tmp_path)
    built = activity._paged_feed_sql(
        conn,
        who["member"],
        clauses=[],
        params=[],
        target_kind=None,
        direction="DESC",
        limit=50,
    )
    assert built is not None
    plan = _plan(conn, *built)
    assert "MULTI-INDEX OR" not in plan
    assert plan.count("idx_activity_kind_id") == 4  # one seek per arm


def test_admin_and_ungated_reads_are_untouched(tmp_path):
    """The no-gate path must stay the plain single statement it always was — the
    internal callers and the per-entity timelines ride it."""
    conn, who = _mixed_fixture(tmp_path)
    for actor in (activity._UNGATED, who["admin"]):
        built = activity._paged_feed_sql(
            conn,
            actor,
            clauses=[],
            params=[],
            target_kind=None,
            direction="DESC",
            limit=50,
        )
        assert built is not None
        sql, _params = built
        assert "UNION ALL" not in sql
