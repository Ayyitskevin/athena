"""A ready queue, and claiming the next ready issue in one call.

Finding work used to take three reads and a judgement: the desk, a work-context
packet, then a claim with the right ETag, with nothing saying which visible issue
was actually free to take. An issue is READY for an actor when:

- the actor can see it;
- its status category is to-do or doing;
- nobody holds a live lease on it (a lapsed lease does not count);
- no issue that is not done blocks it (the same rule as the open-blocker read,
  and a blocker the actor cannot see still blocks);
- the actor may claim it under the claim command's own policy (assignee,
  delegated contributor, or admin), so the queue never offers what a claim refuses.

Delegated-to-me first, then priority, then age. Exposed as ``is:ready`` in the
work query, a bounded ``work.ready`` desk lane with its own total, and
``claim_next_ready``, which claims through the existing claim command. These pin it.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading

import pytest
from fastapi.testclient import TestClient

from athena.aegis import dependencies, ready_queue
from athena.core import db, users, work_query
from athena.main import create_app
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}
ADMIN = {"id": 1, "role": "admin"}
_TS = "%Y-%m-%d %H:%M:%S"


def _app(tmp_path, name="ready.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", name="Sol"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _ago(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).strftime(_TS)


def _sql(db_file, statement, params=()):
    conn = db.connect(db_file)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _issue(client, title, **fields):
    return client.post("/issues", json={"title": title, **fields}, headers=H1).json()


def _delegate(client, issue_id, agent):
    sent = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    assert sent.status_code in (200, 201), sent.text


def _block(client, blocked_id, blocker_id):
    linked = client.post(
        f"/issues/{blocked_id}/links",
        json={"target_ref": str(blocker_id), "relation": "blocked_by"},
        headers=H1,
    )
    assert linked.status_code in (200, 201), linked.text


def _claim(client, agent, issue_id):
    tag = client.get(f"/issues/{issue_id}", headers=_bearer(agent)).headers["etag"]
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        headers={**_bearer(agent), "If-Match": tag},
    )
    assert claimed.status_code in (200, 201), claimed.text
    return claimed.json()


def _lane(client, headers=H1, limit=None):
    params = {} if limit is None else {"limit": limit}
    response = client.get("/desk/ready", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(lane):
    return [row["issue"]["id"] for row in lane["items"]]


def _mcp(app, agent):
    tc = TestClient(app)
    tc.__enter__()
    tc.headers.update(_bearer(agent))
    return tc, AthenaClient(client=tc)


# ── the rule ────────────────────────────────────────────────────────────────


def test_a_blocked_issue_is_not_ready_until_its_blocker_is_done(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        blocked = _issue(c, "blocked")
        blocker = _issue(c, "blocker")
        _block(c, blocked["id"], blocker["id"])

        assert blocked["id"] not in _ids(_lane(c))
        assert blocker["id"] in _ids(_lane(c))  # positive control: the blocker is
        # Done blockers release it.
        _sql(
            db_file, "UPDATE issues SET status = 'done' WHERE id = ?", (blocker["id"],)
        )
        lane = _lane(c)
        assert blocked["id"] in _ids(lane)
        assert blocker["id"] not in _ids(lane)  # done is not ready


def test_a_blocker_the_actor_cannot_see_still_blocks(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        project = c.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=H1
        ).json()
        hidden_blocker = _issue(c, "hidden blocker", project_id=project["id"])
        work = _issue(c, "visible work")
        free = _issue(c, "free work")
        _block(c, work["id"], hidden_blocker["id"])
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        for issue in (work, free):
            _delegate(c, issue["id"], agent)

        lane = _lane(c, headers=_bearer(agent))
        assert work["id"] not in _ids(lane)
        assert _ids(lane) == [free["id"]]  # positive control
        # And the hidden blocker's identity is not disclosed by the lane.
        assert "hidden blocker" not in str(lane)


def test_done_is_not_ready_and_todo_and_doing_are(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        todo = _issue(c, "todo")
        doing = _issue(c, "doing")
        done = _issue(c, "done")
        _sql(
            db_file,
            "UPDATE issues SET status = 'in_progress' WHERE id = ?",
            (doing["id"],),
        )
        _sql(db_file, "UPDATE issues SET status = 'done' WHERE id = ?", (done["id"],))
        archived = _issue(c, "archived")
        _sql(
            db_file,
            "UPDATE issues SET archived_at = datetime('now') WHERE id = ?",
            (archived["id"],),
        )
        assert set(_ids(_lane(c))) == {todo["id"], doing["id"]}


def test_the_blocker_rule_is_the_open_blocker_reads_own(tmp_path):
    # The ready rule's blocker check runs in SQL so the lane's total is exact; it
    # must agree with open_blockers_by_issue on every case, including a project
    # whose done state was renamed (an issue still saying 'done' there is NOT done).
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = c.post(
            "/projects", json={"name": "Renamed", "key": "REN"}, headers=H1
        ).json()
        targets = [_issue(c, f"target {n}") for n in range(6)]
        blockers = [
            _issue(c, "backlog open"),
            _issue(c, "backlog done"),
            _issue(c, "project open", project_id=project["id"]),
            _issue(c, "project shipped", project_id=project["id"]),
            _issue(c, "project stale done", project_id=project["id"]),
        ]
        _sql(
            db_file,
            "UPDATE project_statuses SET name = 'shipped' "
            "WHERE project_id = ? AND name = 'done'",
            (project["id"],),
        )
        _sql(
            db_file,
            "UPDATE issues SET status = 'done' WHERE id = ?",
            (blockers[1]["id"],),
        )
        _sql(
            db_file,
            "UPDATE issues SET status = 'shipped' WHERE id = ?",
            (blockers[3]["id"],),
        )
        _sql(
            db_file,
            "UPDATE issues SET status = 'done' WHERE id = ?",
            (blockers[4]["id"],),
        )
        for target, blocker in zip(targets, blockers, strict=False):
            _block(c, target["id"], blocker["id"])
        # targets[5] has no blocker at all.

        conn = db.connect(db_file)
        reference = dependencies.open_blockers_by_issue(
            conn, [t["id"] for t in targets]
        )
        twin = {
            row["id"]
            for row in conn.execute(
                "SELECT i.id FROM issues i WHERE "
                f"{dependencies.open_blocker_exists_sql('i.id')}"
            )
        }
        conn.close()
        expected = {tid for tid, found in reference.items() if found}
        assert {t["id"] for t in targets} & twin == expected
        # The cases the reference itself decides: open blocks, done and shipped do
        # not, a stale 'done' in a renamed project still blocks.
        assert expected == {targets[0]["id"], targets[2]["id"], targets[4]["id"]}


# ── leases: shown, never swept ──────────────────────────────────────────────


def test_a_leased_issue_is_not_ready_and_a_lapsed_one_is_with_its_last_holder(
    tmp_path,
):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        first = _agent(c, "one@e.com", "One")
        second = _agent(c, "two@e.com", "Two")
        held = _issue(c, "held")
        for agent in (first, second):
            _delegate(c, held["id"], agent)
        _claim(c, first, held["id"])

        assert held["id"] not in _ids(_lane(c, headers=_bearer(second)))
        _sql(
            db_file,
            "UPDATE issue_leases SET expires_at = ? WHERE issue_id = ?",
            (_ago(hours=2), held["id"]),
        )
        conn = db.connect(db_file)
        before = [tuple(r) for r in conn.execute("SELECT * FROM issue_leases")]
        changes = conn.total_changes
        lane = ready_queue.build_ready(
            conn, actor=users.get_user(conn, second["user"]["id"])
        )
        assert conn.total_changes == changes
        assert [tuple(r) for r in conn.execute("SELECT * FROM issue_leases")] == before
        conn.close()

        (row,) = lane["items"]
        assert row["issue"]["id"] == held["id"]
        assert row["previous_lease"]["holder_name"] == "One"
        assert row["previous_lease"]["expired_at"] is not None
        assert row["open_handoff"] is None
        # The REST lane reads the same thing and also writes nothing (K1).
        assert _ids(_lane(c, headers=_bearer(second))) == [held["id"]]
        conn = db.connect(db_file)
        assert [tuple(r) for r in conn.execute("SELECT * FROM issue_leases")] == before
        conn.close()


def test_a_yielded_issue_is_ready_with_its_open_handoff(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        first = _agent(c, "one@e.com", "One")
        second = _agent(c, "two@e.com", "Two")
        issue = _issue(c, "handed back")
        for agent in (first, second):
            _delegate(c, issue["id"], agent)
        lease = _claim(c, first, issue["id"])
        yielded = c.post(
            f"/issues/{issue['id']}/yield",
            json={
                "generation": lease["generation"],
                "reason": "needs_input",
                "attempted_work": "Read the code.",
                "evidence": ["notes"],
                "blocking_question": "Which path?",
                "resume_instructions": "Pick a path.",
            },
            headers=_bearer(first),
        )
        assert yielded.status_code == 201, yielded.text

        (row,) = _lane(c, headers=_bearer(second))["items"]
        assert row["open_handoff"]["handoff_token"] == yielded.json()["handoff_token"]
        assert row["open_handoff"]["reason"] == "needs_input"
        assert row["open_handoff"]["yielded_by"] == "One"


# ── order ───────────────────────────────────────────────────────────────────


def test_delegated_to_me_sorts_first_then_priority_then_age(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        assigned_urgent = _issue(c, "assigned urgent", priority="urgent")
        c.put(
            f"/issues/{assigned_urgent['id']}/assignee",
            json={"assignee_id": agent["user"]["id"]},
            headers=H1,
        )
        delegated_low = _issue(c, "delegated low", priority="low")
        delegated_high_new = _issue(c, "delegated high new", priority="high")
        delegated_high_old = _issue(c, "delegated high old", priority="high")
        _sql(
            db_file,
            "UPDATE issues SET created_at = ? WHERE id = ?",
            (_ago(days=9), delegated_high_old["id"]),
        )
        for issue in (delegated_low, delegated_high_new, delegated_high_old):
            _delegate(c, issue["id"], agent)
        _issue(c, "not mine")  # visible, but the agent may not claim it

        lane = _lane(c, headers=_bearer(agent))
        assert _ids(lane) == [
            delegated_high_old["id"],
            delegated_high_new["id"],
            delegated_low["id"],
            assigned_urgent["id"],
        ]
        assert lane["items"][0]["why"] == {
            "delegated_to_you": True,
            "assigned_to_you": False,
        }
        assert lane["total"] == 4
        # Positive control: the admin may claim anything, so the unassigned one is
        # ready for the admin.
        assert "not mine" in [r["issue"]["title"] for r in _lane(c)["items"]]


def test_the_lane_is_bounded_and_says_so(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        for n in range(5):
            _issue(c, f"work {n}")
        lane = _lane(c, limit=2)
        assert len(lane["items"]) == 2
        assert lane["total"] == 5 and lane["clipped"] is True
        assert _lane(c, limit=5)["clipped"] is False  # positive control
        assert c.get("/desk/ready", params={"limit": 0}, headers=H1).status_code == 422
        assert c.get("/desk/ready", params={"limit": 51}, headers=H1).status_code == 422
        desk = c.get("/desk", headers=H1).json()["work"]["ready"]
        assert desk["total"] == 5 and "items" in desk and "meaning" in desk


# ── visibility ──────────────────────────────────────────────────────────────


def test_a_hidden_projects_items_are_excluded(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        project = c.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=H1
        ).json()
        hidden = _issue(c, "hidden work", project_id=project["id"])
        public = _issue(c, "backlog work")
        _delegate(c, public["id"], agent)
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        # A contributor row on the hidden issue, as if delegated before it went
        # private: eligibility alone must not leak it.
        _sql(
            db_file,
            "INSERT INTO issue_contributors (issue_id, user_id, added_by) VALUES (?, ?, 1)",
            (hidden["id"], agent["user"]["id"]),
        )

        lane = _lane(c, headers=_bearer(agent))
        assert _ids(lane) == [public["id"]] and lane["total"] == 1
        query = c.get(
            "/issues", params={"q": "is:ready"}, headers=_bearer(agent)
        ).json()
        assert [row["id"] for row in query] == [public["id"]]
        count = c.get(
            "/issues/query/count", params={"q": "is:ready"}, headers=_bearer(agent)
        ).json()
        assert count["matched"] == 1
        # Positive control: the admin sees both.
        assert set(_ids(_lane(c))) == {hidden["id"], public["id"]}


# ── the query atom ──────────────────────────────────────────────────────────


def test_is_ready_is_a_query_atom_with_the_same_rule(tmp_path):
    assert work_query.parse("is:ready").terms[0].value == "ready"
    with pytest.raises(work_query.QueryError) as caught:
        work_query.parse("is:redy")
    assert caught.value.atom == "is:redy" and "is:ready" in str(caught.value)

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        ready = _issue(c, "ready")
        blocked = _issue(c, "blocked")
        blocker = _issue(c, "blocker")
        _block(c, blocked["id"], blocker["id"])
        held = _issue(c, "held")
        for issue in (ready, blocked, held):
            _delegate(c, issue["id"], agent)
        _claim(c, agent, held["id"])

        titles = [
            r["title"]
            for r in c.get(
                "/issues", params={"q": "is:ready"}, headers=_bearer(agent)
            ).json()
        ]
        assert titles == ["ready"]
        # The negation wraps the positive form.
        others = c.get(
            "/issues", params={"q": "-is:ready"}, headers=_bearer(agent)
        ).json()
        assert "ready" not in [r["title"] for r in others]
        # An anonymous caller has no "ready for me": refused, not empty.
        refused = c.get("/issues", params={"q": "is:ready"})
        assert refused.status_code in (401, 422)
        if refused.status_code == 422:
            assert refused.json()["detail"]["atom"] == "is:ready"


# ── claim_next_ready ────────────────────────────────────────────────────────


def test_claim_next_ready_claims_the_top_item_and_returns_the_tags(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        top = _issue(c, "top", priority="urgent")
        later = _issue(c, "later", priority="low")
        for issue in (top, later):
            _delegate(c, issue["id"], agent)
        tc, client = _mcp(app, agent)
        try:
            result = client.claim_next_ready()
            assert result["claimed"] is True
            assert result["issue"]["id"] == top["id"]
            assert result["lease"]["holder_id"] == agent["user"]["id"]
            assert len(result["lease"]["generation"]) == 32
            context = tc.get(f"/issues/{top['id']}/work-context")
            assert result["issue_etag"] == context.json()["issue_etag"]
            assert result["work_context_etag"] == context.headers["etag"]
            # One claim per call: a second call refuses while the first is held.
            again = client.claim_next_ready()
            assert again["claimed"] is False and again["reason"] == "already_holding"
            conn = db.connect(db_file)
            assert [
                r["issue_id"] for r in conn.execute("SELECT issue_id FROM issue_leases")
            ] == [top["id"]]
            conn.close()
        finally:
            tc.__exit__(None, None, None)


def test_claim_next_ready_with_nothing_ready_refuses_cleanly(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        tc, client = _mcp(app, agent)
        try:
            result = client.claim_next_ready()
            assert result == {
                "claimed": False,
                "reason": "nothing_ready",
                "ready_total": 0,
                "attempts": [],
            }
        finally:
            tc.__exit__(None, None, None)


def test_claim_next_ready_limit_is_bounded(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        tc, client = _mcp(app, agent)
        try:
            for bad in (0, 6):
                with pytest.raises(ValueError):
                    client.claim_next_ready(limit=bad)
        finally:
            tc.__exit__(None, None, None)


def test_two_concurrent_claim_next_ready_calls_never_both_win(tmp_path):
    db_file = tmp_path / "race.db"
    setup_app = create_app(db_file)
    with TestClient(setup_app) as c:
        _bootstrap(c)
        first = _agent(c, "one@e.com", "One")
        second = _agent(c, "two@e.com", "Two")
        only = _issue(c, "the only one")
        for agent in (first, second):
            _delegate(c, only["id"], agent)

    barrier = threading.Barrier(2)

    def compete(agent):
        # Separate app instances on one database, like two processes.
        app = create_app(db_file)
        tc, client = _mcp(app, agent)
        original = client.ready_work

        def read_then_wait(**kwargs):
            # Both read the lane before either claims: the worst interleaving.
            lane = original(**kwargs)
            barrier.wait(timeout=10)
            return lane

        client.ready_work = read_then_wait
        try:
            return client.claim_next_ready()
        finally:
            tc.__exit__(None, None, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            f.result(timeout=30)
            for f in (pool.submit(compete, first), pool.submit(compete, second))
        ]
    winners = [o for o in outcomes if o["claimed"]]
    losers = [o for o in outcomes if not o["claimed"]]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0]["reason"] == "lost_race"
    assert losers[0]["attempts"][0]["issue_id"] == only["id"]
    assert losers[0]["attempts"][0]["status"] == 409
    conn = db.connect(db_file)
    rows = conn.execute("SELECT issue_id, holder_id FROM issue_leases").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["holder_id"] == winners[0]["lease"]["holder_id"]


def test_an_etag_race_returns_a_clean_refusal_and_writes_no_lease(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue = _issue(c, "edited mid-claim")
        _delegate(c, issue["id"], agent)
        tc, client = _mcp(app, agent)
        original = client.get_issue

        def read_then_someone_edits(ref):
            seen = original(ref)
            edited = c.patch(f"/issues/{ref}", json={"title": "edited"}, headers=H1)
            assert edited.status_code == 200, edited.text
            return seen

        client.get_issue = read_then_someone_edits
        try:
            result = client.claim_next_ready()
            assert result["claimed"] is False
            assert result["reason"] == "lost_race"
            assert result["attempts"] == [
                {"issue_id": issue["id"], "status": 412, "outcome": "stale_revision"}
            ]
            conn = db.connect(db_file)
            assert (
                conn.execute("SELECT COUNT(*) AS n FROM issue_leases").fetchone()["n"]
                == 0
            )
            conn.close()
            # Positive control: without the edit, the same call claims it.
            client.get_issue = original
            assert client.claim_next_ready()["claimed"] is True
        finally:
            tc.__exit__(None, None, None)


def test_claim_next_ready_tries_the_next_item_only_within_its_limit(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        rival = _agent(c, "riv@e.com", "Rival")
        top = _issue(c, "top", priority="urgent")
        second = _issue(c, "second", priority="low")
        for issue in (top, second):
            for who in (agent, rival):
                _delegate(c, issue["id"], who)
        tc, client = _mcp(app, agent)
        original = client.get_issue

        def rival_takes_top(ref):
            seen = original(ref)
            if int(ref) == top["id"]:
                _claim(c, rival, top["id"])
            return seen

        client.get_issue = rival_takes_top
        try:
            result = client.claim_next_ready(limit=2)
            assert result["claimed"] is True and result["issue"]["id"] == second["id"]
            assert result["attempts"] == [
                {"issue_id": top["id"], "status": 409, "outcome": "lost_race"}
            ]
        finally:
            tc.__exit__(None, None, None)


# ── the packet points at the lane ───────────────────────────────────────────


def test_the_work_context_packet_points_at_the_ready_lane(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c, "any")
        packet = c.get(f"/issues/{issue['id']}/work-context", headers=H1).json()
        semantics = packet["semantics"]
        assert "ready" in semantics["does_not_assert"]  # the packet still does not
        assert "/desk/ready" in semantics["readiness"]
        assert "work.ready" in semantics["readiness"]


def test_the_mcp_tool_is_registered_with_the_claim_scope():
    from athena.core import tokens
    from athena.mcp.server import TOOL_SCOPES

    assert TOOL_SCOPES["claim_next_ready"] == tokens.ISSUE_WRITE_SCOPE
    assert TOOL_SCOPES["claim_next_ready"] == TOOL_SCOPES["claim_issue"]
