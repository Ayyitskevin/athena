"""POST /issues/bulk — best-effort batch triage.

Agents and triage workflows must move many issues at once without N round-trips.
This endpoint applies the same change to a list of issues, each attempted and
AUTHORIZED on its own (creator / assignee / delegated contributor / admin per
issue, exactly like the single-issue writes), reporting the per-issue outcome —
so one issue's 403/404/422 never sinks the rest. These pin: the multi-field apply, the per-item failures, set-null
clearing, the request-level guards, and that the audit trail records each change.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # bootstrap admin
H2 = {"X-Athena-Actor": "2"}  # a second member


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "B", "role": "member"}, headers=H1
    )


def _project(client, name="Ops", key="OPS"):
    return client.post("/projects", json={"name": name, "key": key}, headers=H1).json()[
        "id"
    ]


def _issue(client, pid, title="x", headers=H1):
    return client.post(
        "/issues", json={"title": title, "project_id": pid}, headers=headers
    ).json()["id"]


def _get(client, iid, field):
    return client.get(f"/issues/{iid}", headers=H1).json()[field]


def test_best_effort_multi_field_with_per_item_outcomes(tmp_path):
    # The batch runs as the MEMBER (user 2): admins may act on any issue, so the
    # per-item "forbidden" outcome only exists for a non-admin actor.
    with TestClient(create_app(tmp_path / "bulk.db")) as client:
        _bootstrap(client)
        pid = _project(client)
        a, b = (
            _issue(client, pid, "a", headers=H2),
            _issue(client, pid, "b", headers=H2),
        )
        theirs = _issue(client, pid, "theirs")  # owned by the admin, not the member
        sprint = client.post(
            f"/projects/{pid}/sprints", json={"name": "S1"}, headers=H1
        ).json()["id"]

        r = client.post(
            "/issues/bulk",
            json={
                "ids": [a, b, theirs, 9999],
                "status": "in_progress",
                "assignee_id": 2,
                "sprint_id": sprint,
            },
            headers=H2,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["updated"] == 2 and body["failed"] == 2
        outcomes = {res["id"]: res for res in body["results"]}
        assert outcomes[a]["ok"] and outcomes[b]["ok"]
        # The two we don't own / that don't exist are reported, not fatal.
        assert (
            not outcomes[theirs]["ok"]
            and "delegated contributor" in outcomes[theirs]["error"]
        )
        assert not outcomes[9999]["ok"] and outcomes[9999]["error"] == "no such issue"

        # The owned issues actually moved on all three fields; the others didn't.
        assert (
            _get(client, a, "status"),
            _get(client, a, "assignee_id"),
            _get(client, a, "sprint_id"),
        ) == (
            "in_progress",
            2,
            sprint,
        )
        assert _get(client, theirs, "status") == "open"


def test_set_null_clears_assignee_and_sprint(tmp_path):
    with TestClient(create_app(tmp_path / "clear.db")) as client:
        _bootstrap(client)
        pid = _project(client)
        iid = _issue(client, pid)
        sprint = client.post(
            f"/projects/{pid}/sprints", json={"name": "S1"}, headers=H1
        ).json()["id"]
        client.post(
            "/issues/bulk",
            json={"ids": [iid], "assignee_id": 1, "sprint_id": sprint},
            headers=H1,
        )
        assert (
            _get(client, iid, "assignee_id") == 1
            and _get(client, iid, "sprint_id") == sprint
        )

        # An explicit null unassigns / moves to the backlog (distinct from omitting).
        r = client.post(
            "/issues/bulk",
            json={"ids": [iid], "assignee_id": None, "sprint_id": None},
            headers=H1,
        )
        assert r.json()["updated"] == 1
        assert (
            _get(client, iid, "assignee_id") is None
            and _get(client, iid, "sprint_id") is None
        )


def test_cross_project_sprint_is_atomic_per_item_and_batch_continues(tmp_path):
    # WHY: bulk is best-effort between issues but atomic within one issue. The
    # valid project-B item lands every requested field; the project-A item lands
    # none of them when its sprint validation fails.
    db_file = tmp_path / "cross.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid_a = _project(client, "A", "AAA")
        pid_b = _project(client, "B", "BBB")
        issue_a = _issue(client, pid_a, "invalid item")
        issue_b = _issue(client, pid_b, "valid item")
        sprint_b = client.post(
            f"/projects/{pid_b}/sprints",
            json={"name": "S"},
            headers=H1,
        ).json()["id"]

        response = client.post(
            "/issues/bulk",
            json={
                "ids": [issue_b, issue_a],
                "status": "done",
                "priority": "urgent",
                "assignee_id": 1,
                "sprint_id": sprint_b,
            },
            headers=H1,
        )
        body = response.json()
        assert (body["updated"], body["failed"]) == (1, 1)
        outcomes = {result["id"]: result for result in body["results"]}
        assert outcomes[issue_b]["ok"] is True
        assert outcomes[issue_a] == {
            "id": issue_a,
            "ok": False,
            "error": "sprint belongs to a different project than the issue",
        }
        assert (
            _get(client, issue_b, "status"),
            _get(client, issue_b, "priority"),
            _get(client, issue_b, "assignee_id"),
            _get(client, issue_b, "sprint_id"),
        ) == ("done", "urgent", 1, sprint_b)
        assert (
            _get(client, issue_a, "status"),
            _get(client, issue_a, "priority"),
            _get(client, issue_a, "assignee_id"),
            _get(client, issue_a, "sprint_id"),
        ) == ("open", "medium", None, None)

    conn = db.connect(db_file)
    assert [
        row["verb"]
        for row in conn.execute(
            "SELECT verb FROM activity "
            "WHERE target_kind = 'issue' AND target_id = ? ORDER BY id",
            (issue_a,),
        )
    ] == ["created"]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM issues AS i "
            "LEFT JOIN sprints AS s ON s.id = i.sprint_id "
            "WHERE i.sprint_id IS NOT NULL "
            "AND (s.id IS NULL OR i.project_id IS NULL "
            "OR s.project_id <> i.project_id)"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_bad_status_is_per_item_not_fatal(tmp_path):
    with TestClient(create_app(tmp_path / "status.db")) as client:
        _bootstrap(client)
        pid = _project(client)
        good, bad_target = _issue(client, pid, "good"), _issue(client, pid, "other")
        # 'bogus' is not a status in any project; 'good' move still applies.
        r = client.post(
            "/issues/bulk", json={"ids": [good], "status": "done"}, headers=H1
        )
        assert r.json()["updated"] == 1 and _get(client, good, "status") == "done"
        bad = client.post(
            "/issues/bulk", json={"ids": [bad_target], "status": "bogus"}, headers=H1
        )
        assert bad.json()["results"][0]["error"] == "no such status for this project"


def test_request_level_guards(tmp_path):
    with TestClient(create_app(tmp_path / "guards.db")) as client:
        _bootstrap(client)
        pid = _project(client)
        iid = _issue(client, pid)
        assert (
            client.post(
                "/issues/bulk", json={"ids": [], "status": "open"}, headers=H1
            ).status_code
            == 422
        )
        assert (
            client.post("/issues/bulk", json={"ids": [iid]}, headers=H1).status_code
            == 422
        )
        assert (
            client.post(
                "/issues/bulk", json={"ids": [iid], "status": None}, headers=H1
            ).status_code
            == 422
        )
        big = list(range(1, 502))  # over the 500 cap
        assert (
            client.post(
                "/issues/bulk", json={"ids": big, "status": "open"}, headers=H1
            ).status_code
            == 422
        )


def test_duplicate_ids_collapse_and_activity_recorded(tmp_path):
    db_file = tmp_path / "dupe.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _project(client)
        iid = _issue(client, pid)
        r = client.post(
            "/issues/bulk", json={"ids": [iid, iid, iid], "status": "done"}, headers=H1
        )
        # Deduped: one result row, one update.
        assert [res["id"] for res in r.json()["results"]] == [iid]
    conn = db.connect(db_file)
    verbs = [
        row["verb"]
        for row in conn.execute(
            "SELECT verb FROM activity WHERE target_kind='issue' AND target_id=? ORDER BY id",
            (iid,),
        ).fetchall()
    ]
    conn.close()
    # The bulk status move left the same audit fact a single-issue PATCH would.
    assert "changed_status" in verbs
