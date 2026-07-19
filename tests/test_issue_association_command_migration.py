"""Label attach/detach and contributor add/remove are audited-atomic commands.

These four issue-association writes ran the join-table mutation and the activity
recorder (and, for contributors, the auto-watch) as SEPARATE commits: a crash
between them could label an issue with no 'labeled' event, or delegate to an
agent with no 'delegated' event (and no inbox watch). issue_commands.attach_label
/ detach_label / add_contributor / remove_contributor now fold each write and its
audit into one transaction, under the same can_act_on gate, reachable identically
from REST, the browser, and (via REST) MCP. These pin atomicity, attribution, the
gate, idempotence, and transport parity.
"""
from fastapi.testclient import TestClient

from athena.aegis import issue_commands
from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="issue_assoc.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _issue(client, title="work", headers=H1):
    return client.post("/issues", json={"title": title}, headers=headers).json()


def _label(client, name="bug"):
    return client.post(
        "/labels", json={"name": name, "color": "#ff0000"}, headers=H1
    ).json()


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=100)
    conn.close()
    return rows


def test_label_attach_and_detach_are_recorded_once_and_attributed(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        label = _label(c)
        assert (
            c.post(
                f"/issues/{issue['id']}/labels",
                json={"label_id": label["id"]},
                headers=H1,
            ).status_code
            == 201
        )
        # Idempotent: re-attach records nothing new.
        c.post(
            f"/issues/{issue['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        assert (
            c.delete(f"/issues/{issue['id']}/labels/{label['id']}", headers=H1).status_code
            == 200
        )
    labeled = _events(db_file, "labeled")
    unlabeled = _events(db_file, "unlabeled")
    assert [(e["actor_id"], e["target_id"]) for e in labeled] == [(1, issue["id"])]
    assert [(e["actor_id"], e["target_id"]) for e in unlabeled] == [(1, issue["id"])]


def test_contributor_add_delegate_and_remove_are_recorded(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = c.post(
            "/users", json={"email": "m@e.com", "name": "Mem", "role": "member"},
            headers=H1,
        ).json()
        agent = c.post(
            "/users",
            json={"email": "sol@e.com", "name": "Sol", "role": "member", "is_agent": True},
            headers=H1,
        ).json()
        issue = _issue(c)
        assert (
            c.post(
                f"/issues/{issue['id']}/contributors",
                json={"user_id": member["id"]},
                headers=H1,
            ).status_code
            == 201
        )
        # Delegation to an agent records the distinct 'delegated' verb.
        assert (
            c.post(
                f"/issues/{issue['id']}/delegate",
                json={"user_id": agent["id"]},
                headers=H1,
            ).status_code
            == 201
        )
        assert (
            c.delete(
                f"/issues/{issue['id']}/contributors/{member['id']}", headers=H1
            ).status_code
            == 200
        )
    assert [e["target_id"] for e in _events(db_file, "added_contributor")] == [issue["id"]]
    assert [e["target_id"] for e in _events(db_file, "delegated")] == [issue["id"]]
    assert [e["target_id"] for e in _events(db_file, "removed_contributor")] == [issue["id"]]
    # The delegated agent is auto-watching (the watch committed atomically with it).
    conn = db.connect(db_file)
    watching = conn.execute(
        "SELECT 1 FROM watches WHERE user_id = ? AND target_kind = 'issue' AND target_id = ?",
        (agent["id"], issue["id"]),
    ).fetchone()
    conn.close()
    assert watching is not None


def test_label_command_rolls_back_the_event_when_audit_fails(tmp_path):
    import athena.aegis.issue_activity as issue_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        label = _label(c)
    conn = db.connect(db_file)
    actor = {"id": 1, "role": "admin", "_token_scopes": None}
    original = issue_activity.record_label_added

    def boom(*a, **k):
        raise RuntimeError("audit down")

    issue_activity.record_label_added = boom
    try:
        try:
            issue_commands.attach_label(
                conn, actor=actor, issue_id=issue["id"], label_id=label["id"]
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        issue_activity.record_label_added = original
    # The attach rolled back with its failed event — no label pairing, no event.
    from athena.core import labels

    assert labels.labels_for_issue(conn, issue["id"]) == []
    conn.close()
    assert _events(db_file, "labeled") == []


def test_association_writes_share_the_can_act_on_gate(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bystander = c.post(
            "/users", json={"email": "b@e.com", "name": "Bee", "role": "member"},
            headers=H1,
        ).json()
        issue = _issue(c)  # created by admin
        label = _label(c)
        as_bystander = {"X-Athena-Actor": str(bystander["id"])}
        assert (
            c.post(
                f"/issues/{issue['id']}/labels",
                json={"label_id": label["id"]},
                headers=as_bystander,
            ).status_code
            == 403
        )
        assert (
            c.post(
                f"/issues/{issue['id']}/contributors",
                json={"user_id": bystander["id"]},
                headers=as_bystander,
            ).status_code
            == 403
        )


def test_delegate_rejects_a_non_agent_target(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        human = c.post(
            "/users", json={"email": "h@e.com", "name": "Human", "role": "member"},
            headers=H1,
        ).json()
        issue = _issue(c)
        r = c.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": human["id"]},
            headers=H1,
        )
        assert r.status_code == 422
        assert "must be an agent" in r.json()["detail"]


def test_mcp_client_reaches_the_migrated_association_writes(tmp_path):
    from athena.mcp.client import AthenaClient

    app, db_file = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        issue = _issue(tc)
        label = _label(tc)
        raw = tc.post(
            "/tokens", json={"name": "t", "scopes": ["read", "issue:write"]}, headers=H1
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        tc.headers.pop("X-Athena-Actor", None)
        ath = AthenaClient(client=tc, run_id="assoc-run")
        ath.attach_label(issue["id"], label["id"])
    finally:
        tc.__exit__(None, None, None)
    conn = db.connect(db_file)
    ev = conn.execute("SELECT run_id FROM activity WHERE verb = 'labeled'").fetchone()
    conn.close()
    assert ev["run_id"] == "assoc-run"  # attributed to the agent's run
