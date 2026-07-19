"""The delegation claim/lease protocol — accept / decline / complete.

The invariant that matters: two agents delegated the same issue cannot silently both work
it. A LEASE is the exclusive "I'm on this now" token, and these tests encode its contract:

  * claiming acquires the lease; a second agent's claim is REJECTED (409) while it is live;
  * the holder re-claiming RENEWS its own window (idempotent extend);
  * a lease whose window has passed is reclaimable — a crashed holder never pins the work;
  * completing releases the lease (freeing the issue) and only the holder (or an admin) may;
  * declining removes the actor from the contributor set (visible refusal) and drops any
    lease it held;
  * only the assignee / a delegated contributor / an admin may claim;
  * every transition is an audit event, atomic with the lease write.
"""
from fastapi.testclient import TestClient

from athena.aegis import issue_commands, issues, lease_commands, leases
from athena.core import activity, db
from athena.main import create_app


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed(conn):
    # 1: human owner/admin, 2: AgentA, 3: AgentB (both agents), 4: AgentC (uninvolved).
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) VALUES ('o@e.com','O','admin',0)"
    )
    conn.execute("INSERT INTO users (email, name, is_agent) VALUES ('a@e.com','AgentA',1)")
    conn.execute("INSERT INTO users (email, name, is_agent) VALUES ('b@e.com','AgentB',1)")
    conn.execute("INSERT INTO users (email, name, is_agent) VALUES ('c@e.com','AgentC',1)")
    conn.commit()


def _actor(conn, uid):
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def _delegated_issue(conn):
    """An issue delegated to AgentA (2) and AgentB (3), owned by 1."""
    admin = _actor(conn, 1)
    issue = issues.create_issue(conn, title="work", body="do it", created_by=1)
    for agent_id in (2, 3):
        issue_commands.add_contributor(
            conn, actor=admin, issue_id=issue["id"], user_id=agent_id, require_agent=True
        )
    return issue


# --- command layer: the interlock -------------------------------------------


def test_claim_acquires_and_second_agent_conflicts(tmp_path):
    conn = _migrated_conn(tmp_path / "c.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease = lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    assert lease["holder_id"] == 2 and lease["active"] is True
    try:
        lease_commands.claim_issue(conn, actor=_actor(conn, 3), issue_id=issue["id"])
        raise AssertionError("second claim should conflict")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "conflict"
        assert "AgentA" in exc.detail  # names the current holder


def test_holder_reclaim_renews_window(tmp_path):
    conn = _migrated_conn(tmp_path / "r.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    again = lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    assert again["holder_id"] == 2 and again["active"] is True
    # The renewal records a distinct verb so the trail tells taking from extending.
    verbs = [e["verb"] for e in activity.list_activity(conn, target_kind="issue", target_id=issue["id"])]
    assert "claimed" in verbs and "lease_renewed" in verbs


def test_expired_lease_is_reclaimable_by_another_agent(tmp_path):
    conn = _migrated_conn(tmp_path / "exp.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    # Force the lease into the past — a crashed holder whose window has elapsed.
    conn.execute(
        "UPDATE issue_leases SET expires_at = datetime('now','-1 minute') WHERE issue_id = ?",
        (issue["id"],),
    )
    conn.commit()
    assert leases.get_lease(conn, issue["id"])["active"] is False
    # A different eligible agent can now take it — no crash pins the work forever.
    lease = lease_commands.claim_issue(conn, actor=_actor(conn, 3), issue_id=issue["id"])
    assert lease["holder_id"] == 3 and lease["active"] is True


def test_complete_releases_only_for_the_holder(tmp_path):
    conn = _migrated_conn(tmp_path / "done.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    # A different contributor cannot complete someone else's claim.
    try:
        lease_commands.complete_claim(conn, actor=_actor(conn, 3), issue_id=issue["id"])
        raise AssertionError("non-holder complete should conflict")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "conflict"
    # The holder completes → lease released, issue free for the next claimant.
    lease_commands.complete_claim(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    assert leases.get_lease(conn, issue["id"]) is None
    reclaim = lease_commands.claim_issue(conn, actor=_actor(conn, 3), issue_id=issue["id"])
    assert reclaim["holder_id"] == 3


def test_decline_removes_contributor_and_drops_lease(tmp_path):
    conn = _migrated_conn(tmp_path / "dec.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    remaining = lease_commands.decline_delegation(
        conn, actor=_actor(conn, 2), issue_id=issue["id"]
    )
    assert [c["user_id"] for c in remaining] == [3]  # AgentA removed, AgentB stays
    assert leases.get_lease(conn, issue["id"]) is None  # its lease was released too
    verbs = [e["verb"] for e in activity.list_activity(conn, target_kind="issue", target_id=issue["id"])]
    assert "delegation_declined" in verbs


def test_non_contributor_cannot_claim(tmp_path):
    conn = _migrated_conn(tmp_path / "nc.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    try:
        lease_commands.claim_issue(conn, actor=_actor(conn, 4), issue_id=issue["id"])
        raise AssertionError("uninvolved agent should be forbidden")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "forbidden"


def test_decline_by_non_contributor_is_404(tmp_path):
    conn = _migrated_conn(tmp_path / "d404.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    try:
        lease_commands.decline_delegation(conn, actor=_actor(conn, 4), issue_id=issue["id"])
        raise AssertionError("declining work you weren't delegated should 404")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "not_found"


def test_invalid_lease_window_rejected(tmp_path):
    conn = _migrated_conn(tmp_path / "iv.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    try:
        lease_commands.claim_issue(
            conn, actor=_actor(conn, 2), issue_id=issue["id"], lease_seconds=5
        )
        raise AssertionError("a sub-minimum lease window should be rejected")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "invalid"


def test_conflict_leaves_holder_and_trail_untouched(tmp_path):
    # A rejected claim must not perturb the existing lease or file a spurious event.
    conn = _migrated_conn(tmp_path / "atomic.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    before = [e["verb"] for e in activity.list_activity(conn, target_kind="issue", target_id=issue["id"])]
    try:
        lease_commands.claim_issue(conn, actor=_actor(conn, 3), issue_id=issue["id"])
    except issue_commands.IssueCommandError:
        pass
    assert leases.get_lease(conn, issue["id"])["holder_id"] == 2  # still AgentA
    after = [e["verb"] for e in activity.list_activity(conn, target_kind="issue", target_id=issue["id"])]
    assert after == before  # no event from the rejected claim


# --- REST parity + a hidden-issue read gate ---------------------------------


def _rest_seed(db_file):
    conn = db.connect(db_file)
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) VALUES ('o@e.com','O','admin',0)"
    )
    conn.execute("INSERT INTO users (email, name, is_agent) VALUES ('a@e.com','AgentA',1)")
    conn.execute("INSERT INTO users (email, name, is_agent) VALUES ('b@e.com','AgentB',1)")
    conn.commit()
    conn.close()


def test_rest_claim_conflict_and_lease_read(tmp_path):
    app = create_app(tmp_path / "rest.db")
    with TestClient(app) as client:
        _rest_seed(tmp_path / "rest.db")
        owner = {"X-Athena-Actor": "1"}
        issue = client.post("/issues", json={"title": "work"}, headers=owner).json()
        for uid in (2, 3):
            client.post(
                f"/issues/{issue['id']}/delegate", json={"user_id": uid}, headers=owner
            )
        # Unclaimed → the lease read is null.
        assert client.get(f"/issues/{issue['id']}/lease").json() is None
        # AgentA claims (201); AgentB's claim conflicts (409).
        a = client.post(f"/issues/{issue['id']}/claim", headers={"X-Athena-Actor": "2"})
        assert a.status_code == 201 and a.json()["holder_id"] == 2
        b = client.post(f"/issues/{issue['id']}/claim", headers={"X-Athena-Actor": "3"})
        assert b.status_code == 409
        # The lease read now shows AgentA holds it, active.
        lease = client.get(f"/issues/{issue['id']}/lease").json()
        assert lease["holder_id"] == 2 and lease["active"] is True
        # AgentA completes (204) → free again for AgentB.
        assert client.post(
            f"/issues/{issue['id']}/complete", headers={"X-Athena-Actor": "2"}
        ).status_code == 204
        again = client.post(f"/issues/{issue['id']}/claim", headers={"X-Athena-Actor": "3"})
        assert again.status_code == 201 and again.json()["holder_id"] == 3


def test_rest_claim_requires_auth(tmp_path):
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        _rest_seed(tmp_path / "auth.db")
        owner = {"X-Athena-Actor": "1"}
        issue = client.post("/issues", json={"title": "work"}, headers=owner).json()
        # Anonymous claim is rejected (no actor).
        assert client.post(f"/issues/{issue['id']}/claim").status_code in (401, 403)
