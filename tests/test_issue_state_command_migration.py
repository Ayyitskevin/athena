"""Archive/restore and parent nesting are audited-atomic commands.

These two issue-state writes used to run the data-layer mutation and the activity
recorder as SEPARATE commits: a crash between them could archive an issue with no
'archived' event, or nest it with no 'set_parent'. issue_commands.set_issue_archived
and set_issue_parent now fold the write and its audit into one transaction, under
the same can-act-on gate as every other issue write, reachable identically from
REST, the browser, and (via REST) MCP. These pin atomicity, attribution, the
gate, no-op silence, and transport parity.
"""
from fastapi.testclient import TestClient

from athena.aegis import issue_commands
from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="issue_state.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _issue(client, title="work", headers=H1):
    return client.post("/issues", json={"title": title}, headers=headers).json()


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=100)
    conn.close()
    return rows


def test_archive_and_restore_are_attributed_and_recorded_once(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        assert c.post(f"/issues/{issue['id']}/archive", headers=H1).status_code == 200
        # Idempotent: re-archiving re-stamps but records no new fact.
        assert c.post(f"/issues/{issue['id']}/archive", headers=H1).status_code == 200
        assert c.post(f"/issues/{issue['id']}/unarchive", headers=H1).status_code == 200

    archived = _events(db_file, "archived")
    unarchived = _events(db_file, "unarchived")
    assert [(e["actor_id"], e["target_id"]) for e in archived] == [(1, issue["id"])]
    assert [(e["actor_id"], e["target_id"]) for e in unarchived] == [(1, issue["id"])]


def test_parent_set_and_clear_are_attributed_and_recorded(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        parent = _issue(c, "parent")
        child = _issue(c, "child")
        assert (
            c.put(
                f"/issues/{child['id']}/parent",
                json={"parent_id": parent["id"]},
                headers=H1,
            ).status_code
            == 200
        )
        assert (
            c.put(
                f"/issues/{child['id']}/parent", json={"parent_id": None}, headers=H1
            ).status_code
            == 200
        )
    set_ev = _events(db_file, "set_parent")
    removed_ev = _events(db_file, "removed_parent")
    assert [e["target_id"] for e in set_ev] == [child["id"]]
    assert [e["target_id"] for e in removed_ev] == [child["id"]]


def test_command_rolls_back_the_event_when_the_write_area_fails(tmp_path):
    # Atomicity at the command layer: if the audit recorder raises, the archive
    # flip rolls back with it — no archived issue without its event, and vice versa.
    import athena.aegis.issue_activity as issue_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
    conn = db.connect(db_file)
    actor = {"id": 1, "role": "admin", "_token_scopes": None}
    original = issue_activity.record_archive_change

    def boom(*a, **k):
        raise RuntimeError("audit down")

    issue_activity.record_archive_change = boom
    try:
        try:
            issue_commands.set_issue_archived(
                conn, actor=actor, issue_id=issue["id"], archived=True
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        issue_activity.record_archive_change = original
    # The flip rolled back: the issue is NOT archived, and no event landed.
    from athena.aegis import issues

    assert issues.get_issue(conn, issue["id"])["archived_at"] is None
    conn.close()
    assert _events(db_file, "archived") == []


def test_state_writes_share_the_can_act_on_gate(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bystander = c.post(
            "/users",
            json={"email": "b@e.com", "name": "Bee", "role": "member"},
            headers=H1,
        ).json()
        issue = _issue(c)  # created by admin
        as_bystander = {"X-Athena-Actor": str(bystander["id"])}
        # A member who is neither creator/assignee/contributor/admin is refused.
        assert c.post(f"/issues/{issue['id']}/archive", headers=as_bystander).status_code == 403
        assert (
            c.put(
                f"/issues/{issue['id']}/parent", json={"parent_id": None}, headers=as_bystander
            ).status_code
            == 403
        )


def test_parent_rejects_self_cycle_and_hidden(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        a = _issue(c, "a")
        b = _issue(c, "b")
        # Self-parent → 422.
        assert (
            c.put(
                f"/issues/{a['id']}/parent", json={"parent_id": a["id"]}, headers=H1
            ).status_code
            == 422
        )
        # a under b, then b under a would cycle → 422.
        c.put(f"/issues/{a['id']}/parent", json={"parent_id": b["id"]}, headers=H1)
        assert (
            c.put(
                f"/issues/{b['id']}/parent", json={"parent_id": a["id"]}, headers=H1
            ).status_code
            == 422
        )


def test_mcp_client_reaches_the_migrated_writes(tmp_path):
    from athena.mcp.client import AthenaClient

    app, db_file = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        raw = tc.post(
            "/tokens", json={"name": "t", "scopes": ["read", "issue:write"]}, headers=H1
        ).json()["token"]
        parent = _issue(tc, "parent")
        child = _issue(tc, "child")
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        tc.headers.pop("X-Athena-Actor", None)
        ath = AthenaClient(client=tc, run_id="state-run")
        ath.set_issue_parent(child["id"], parent["id"])
        ath.archive_issue(child["id"])
    finally:
        tc.__exit__(None, None, None)
    conn = db.connect(db_file)
    ev = conn.execute(
        "SELECT run_id FROM activity WHERE verb = 'archived'"
    ).fetchone()
    conn.close()
    assert ev["run_id"] == "state-run"  # attributed to the agent's run
