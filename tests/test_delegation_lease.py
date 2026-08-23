"""The delegation claim/lease protocol — accept / yield / decline / complete.

The invariant that matters: two agents delegated the same issue cannot silently both work
it. A LEASE is the exclusive "I'm on this now" token, and these tests encode its contract:

  * claiming requires the exact reviewed issue ETag, then acquires the lease;
  * a second agent's claim is REJECTED (409) while it is live;
  * the holder re-claiming RENEWS its own window (idempotent extend);
  * a lease whose window has passed is reclaimable — a crashed holder never pins the work;
  * completing releases the lease (freeing the issue) and only the holder (or an admin) may;
  * yielding releases only the holder's lease with an honest, run-stamped reason;
  * declining removes the actor from the contributor set (visible refusal) and drops any
    lease it held;
  * only the assignee / a delegated contributor / an admin may claim;
  * every transition is an audit event, atomic with the lease write.
"""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from athena.aegis import (
    dependencies,
    issue_commands,
    issue_etags,
    issues,
    lease_commands,
    leases,
)
from athena.core import activity, db, run_context
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
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('a@e.com','AgentA',1)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('b@e.com','AgentB',1)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('c@e.com','AgentC',1)"
    )
    conn.commit()


def _actor(conn, uid):
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def _delegated_issue(conn):
    """An issue delegated to AgentA (2) and AgentB (3), owned by 1."""
    admin = _actor(conn, 1)
    issue = issues.create_issue(conn, title="work", body="do it", created_by=1)
    for agent_id in (2, 3):
        issue_commands.add_contributor(
            conn,
            actor=admin,
            issue_id=issue["id"],
            user_id=agent_id,
            require_agent=True,
        )
    return issue


def _issue_tag(conn, issue_id):
    issue = issues.get_issue(conn, issue_id)
    assert issue is not None
    return issue_etags.current_etag(conn, issue)


def _claim(conn, uid, issue_id, **kwargs):
    return lease_commands.claim_issue(
        conn,
        actor=_actor(conn, uid),
        issue_id=issue_id,
        if_match=[_issue_tag(conn, issue_id)],
        **kwargs,
    )


def _handoff_payload(
    generation: str,
    *,
    reason: str = "blocked",
    note=None,
):
    return {
        "generation": generation,
        "reason": reason,
        "note": note,
        "attempted_work": "Reviewed the issue and reproduced the blocker.",
        "evidence": ["focused tests reproduce the blocker"],
        "blocking_question": "What should happen next?",
        "resume_instructions": "Resolve the question, then rerun the focused tests.",
    }


# --- command layer: the interlock -------------------------------------------


def test_claim_acquires_and_second_agent_conflicts(tmp_path):
    conn = _migrated_conn(tmp_path / "c.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"])
    assert lease["holder_id"] == 2 and lease["active"] is True
    try:
        _claim(conn, 3, issue["id"])
        raise AssertionError("second claim should conflict")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "conflict"
        assert "AgentA" in exc.detail  # names the current holder


def test_active_competing_holder_conflict_precedes_claim_precondition(tmp_path):
    conn = _migrated_conn(tmp_path / "precedence.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"])
    events_before = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    )

    for condition in (None, ["*"], ['"stale"']):
        kwargs = {} if condition is None else {"if_match": condition}
        with pytest.raises(issue_commands.IssueCommandError) as rejected:
            lease_commands.claim_issue(
                conn,
                actor=_actor(conn, 3),
                issue_id=issue["id"],
                **kwargs,
            )
        assert rejected.value.kind == "conflict"
        assert rejected.value.current_etag is None

    assert leases.get_lease(conn, issue["id"])["holder_id"] == 2
    assert (
        activity.list_activity(conn, target_kind="issue", target_id=issue["id"])
        == events_before
    )


def test_two_connections_racing_to_claim_have_exactly_one_winner(tmp_path):
    db_path = tmp_path / "claim-race.db"
    setup = _migrated_conn(db_path)
    _seed(setup)
    issue = _delegated_issue(setup)
    issue_id = issue["id"]
    reviewed_tag = _issue_tag(setup, issue_id)
    setup.close()
    barrier = threading.Barrier(2)

    def compete(actor_id):
        conn = db.connect(db_path)
        try:
            actor = _actor(conn, actor_id)
            barrier.wait(timeout=5)
            try:
                lease_commands.claim_issue(
                    conn,
                    actor=actor,
                    issue_id=issue_id,
                    if_match=[reviewed_tag],
                )
                return "claimed"
            except issue_commands.IssueCommandError as exc:
                return exc.kind
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(compete, 2), pool.submit(compete, 3))
        outcomes = [future.result(timeout=10) for future in futures]

    assert sorted(outcomes) == ["claimed", "conflict"]
    check = db.connect(db_path)
    try:
        assert leases.get_lease(check, issue_id)["holder_id"] in (2, 3)
        claim_events = activity.list_activity(
            check,
            target_kind="issue",
            target_id=issue_id,
            verb="claimed",
        )
        assert len(claim_events) == 1
    finally:
        check.close()


def test_holder_reclaim_renews_window(tmp_path):
    conn = _migrated_conn(tmp_path / "r.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    first = _claim(conn, 2, issue["id"])
    again = _claim(conn, 2, issue["id"], generation=first["generation"])
    assert again["holder_id"] == 2 and again["active"] is True
    assert again["generation"] == first["generation"]
    # The renewal records a distinct verb so the trail tells taking from extending.
    verbs = [
        e["verb"]
        for e in activity.list_activity(
            conn, target_kind="issue", target_id=issue["id"]
        )
    ]
    assert "claimed" in verbs and "lease_renewed" in verbs


def test_expired_lease_is_reclaimable_by_another_agent(tmp_path):
    conn = _migrated_conn(tmp_path / "exp.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"])
    # Force the lease into the past — a crashed holder whose window has elapsed.
    conn.execute(
        "UPDATE issue_leases SET expires_at = datetime('now','-1 minute') WHERE issue_id = ?",
        (issue["id"],),
    )
    conn.commit()
    assert leases.get_lease(conn, issue["id"])["active"] is False
    # A different eligible agent can now take it — no crash pins the work forever.
    lease = _claim(conn, 3, issue["id"])
    assert lease["holder_id"] == 3 and lease["active"] is True


def test_complete_releases_only_for_the_holder(tmp_path):
    conn = _migrated_conn(tmp_path / "done.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"])
    # A different contributor cannot complete someone else's claim.
    try:
        lease_commands.complete_claim(
            conn,
            actor=_actor(conn, 3),
            issue_id=issue["id"],
            generation=lease["generation"],
        )
        raise AssertionError("non-holder complete should conflict")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "conflict"
    # The holder completes → lease released, issue free for the next claimant.
    lease_commands.complete_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        generation=lease["generation"],
    )
    assert leases.get_lease(conn, issue["id"]) is None
    reclaim = _claim(conn, 3, issue["id"])
    assert reclaim["holder_id"] == 3


def test_decline_removes_contributor_and_drops_lease(tmp_path):
    conn = _migrated_conn(tmp_path / "dec.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"])
    remaining = lease_commands.decline_delegation(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        generation=lease["generation"],
    )
    assert [c["user_id"] for c in remaining] == [3]  # AgentA removed, AgentB stays
    assert leases.get_lease(conn, issue["id"]) is None  # its lease was released too
    verbs = [
        e["verb"]
        for e in activity.list_activity(
            conn, target_kind="issue", target_id=issue["id"]
        )
    ]
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
        lease_commands.decline_delegation(
            conn, actor=_actor(conn, 4), issue_id=issue["id"]
        )
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
    _claim(conn, 2, issue["id"])
    before = [
        e["verb"]
        for e in activity.list_activity(
            conn, target_kind="issue", target_id=issue["id"]
        )
    ]
    try:
        _claim(conn, 3, issue["id"])
    except issue_commands.IssueCommandError:
        pass
    assert leases.get_lease(conn, issue["id"])["holder_id"] == 2  # still AgentA
    after = [
        e["verb"]
        for e in activity.list_activity(
            conn, target_kind="issue", target_id=issue["id"]
        )
    ]
    assert after == before  # no event from the rejected claim


def test_claim_requires_one_exact_strong_issue_tag(tmp_path):
    conn = _migrated_conn(tmp_path / "guard.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    events_before = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    )

    with pytest.raises(issue_commands.IssueCommandError) as missing:
        lease_commands.claim_issue(conn, actor=_actor(conn, 2), issue_id=issue["id"])
    assert missing.value.kind == "precondition_required"

    for condition in (
        ["*"],
        [""],
        ['W/"weak"'],
        ['"one", "two"'],
        ['"same"', '"same"'],
    ):
        with pytest.raises(issue_commands.IssueCommandError) as invalid:
            lease_commands.claim_issue(
                conn,
                actor=_actor(conn, 2),
                issue_id=issue["id"],
                if_match=condition,
            )
        assert invalid.value.kind == "invalid_precondition"

    assert leases.get_lease(conn, issue["id"]) is None
    assert (
        activity.list_activity(conn, target_kind="issue", target_id=issue["id"])
        == events_before
    )


def test_stale_claim_returns_current_tag_without_a_lease_or_event(tmp_path):
    conn = _migrated_conn(tmp_path / "stale-guard.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    reviewed_tag = _issue_tag(conn, issue["id"])
    issue_commands.update_issue(
        conn,
        actor=_actor(conn, 1),
        issue_id=issue["id"],
        title="changed after review",
        if_match=[reviewed_tag],
    )
    current_tag = _issue_tag(conn, issue["id"])
    events_before = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    )

    with pytest.raises(issue_commands.IssueCommandError) as stale:
        lease_commands.claim_issue(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            if_match=[reviewed_tag],
        )

    assert stale.value.kind == "precondition_failed"
    assert stale.value.current_etag == current_tag
    assert leases.get_lease(conn, issue["id"]) is None
    assert (
        activity.list_activity(conn, target_kind="issue", target_id=issue["id"])
        == events_before
    )


def test_stale_renewal_does_not_extend_or_record(tmp_path):
    conn = _migrated_conn(tmp_path / "stale-renew.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    reviewed_tag = _issue_tag(conn, issue["id"])
    _claim(conn, 2, issue["id"])
    lease_before = leases.get_lease(conn, issue["id"])
    issue_commands.update_issue(
        conn,
        actor=_actor(conn, 1),
        issue_id=issue["id"],
        title="changed while claimed",
        if_match=[reviewed_tag],
    )
    events_before = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    )

    with pytest.raises(issue_commands.IssueCommandError) as stale:
        lease_commands.claim_issue(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            if_match=[reviewed_tag],
            generation=lease_before["generation"],
        )

    assert stale.value.kind == "precondition_failed"
    assert leases.get_lease(conn, issue["id"]) == lease_before
    assert (
        activity.list_activity(conn, target_kind="issue", target_id=issue["id"])
        == events_before
    )
    renewed = _claim(
        conn,
        2,
        issue["id"],
        generation=lease_before["generation"],
    )
    assert renewed["holder_id"] == 2
    assert (
        activity.list_activity(conn, target_kind="issue", target_id=issue["id"])[0][
            "verb"
        ]
        == "lease_renewed"
    )


@pytest.mark.parametrize(
    ("reason", "note"),
    [
        ("needs_input", None),
        ("blocked", "  waiting for operator  "),
        ("capacity", "   "),
        ("blocked", "x" * 500),
    ],
)
def test_holder_yields_with_reason_and_run_stamp(tmp_path, reason, note):
    conn = _migrated_conn(tmp_path / f"yield-{reason}.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    issues.set_assignee(conn, issue["id"], 2)
    blocker = issues.create_issue(conn, title="existing blocker", body="", created_by=1)
    reason_text, inserted = dependencies.add_link(
        conn,
        from_id=issue["id"],
        to_id=blocker["id"],
        relation="blocked_by",
        created_by=1,
    )
    assert reason_text is None and inserted is True
    lease = _claim(conn, 2, issue["id"])
    issue_before = issues.get_issue(conn, issue["id"])
    contributors_before = conn.execute(
        "SELECT user_id FROM issue_contributors WHERE issue_id = ? ORDER BY user_id",
        (issue["id"],),
    ).fetchall()
    links_before = conn.execute(
        "SELECT from_id, to_id, kind, created_by FROM issue_links "
        "WHERE from_id = ? OR to_id = ? ORDER BY from_id, to_id, kind",
        (issue["id"], issue["id"]),
    ).fetchall()

    token = run_context.set_run_id(f"run-{reason}")
    try:
        handoff = lease_commands.yield_claim(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            **_handoff_payload(lease["generation"], reason=reason, note=note),
        )
    finally:
        run_context.reset_run_id(token)

    assert leases.get_lease(conn, issue["id"]) is None
    assert issues.get_issue(conn, issue["id"]) == issue_before
    assert (
        conn.execute(
            "SELECT user_id FROM issue_contributors WHERE issue_id = ? ORDER BY user_id",
            (issue["id"],),
        ).fetchall()
        == contributors_before
    )
    assert (
        conn.execute(
            "SELECT from_id, to_id, kind, created_by FROM issue_links "
            "WHERE from_id = ? OR to_id = ? ORDER BY from_id, to_id, kind",
            (issue["id"], issue["id"]),
        ).fetchall()
        == links_before
    )
    event = activity.list_activity(
        conn,
        target_kind="issue",
        target_id=issue["id"],
        verb="claim_yielded",
    )[0]
    assert event["actor_id"] == 2
    assert event["detail"] == (
        f"generation {lease['generation']}; "
        f"handoff {handoff['handoff_token']}; "
        f"{reason}: What should happen next?"
    )
    assert event["run_id"] == f"run-{reason}"
    assert handoff["state"] == "awaiting_resume"
    expected_note = (note.strip() or None) if note is not None else None
    assert handoff["note"] == expected_note
    assert handoff["lease_generation"] == lease["generation"]


def test_current_holder_can_yield_after_eligibility_was_removed(tmp_path):
    conn = _migrated_conn(tmp_path / "yield-ineligible.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"])
    issue_commands.remove_contributor(
        conn, actor=_actor(conn, 1), issue_id=issue["id"], user_id=2
    )

    lease_commands.yield_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        **_handoff_payload(lease["generation"], reason="needs_input"),
    )

    assert leases.get_lease(conn, issue["id"]) is None


def test_nonholder_cannot_yield_or_delete_a_replacement_claim(tmp_path):
    conn = _migrated_conn(tmp_path / "yield-holder.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"])
    conn.execute(
        "UPDATE issue_leases SET expires_at = datetime('now','-1 minute') "
        "WHERE issue_id = ?",
        (issue["id"],),
    )
    conn.commit()
    replacement = _claim(conn, 3, issue["id"])

    for actor_id in (1, 2):
        with pytest.raises(issue_commands.IssueCommandError) as rejected:
            lease_commands.yield_claim(
                conn,
                actor=_actor(conn, actor_id),
                issue_id=issue["id"],
                **_handoff_payload(replacement["generation"]),
            )
        assert rejected.value.kind == "conflict"

    assert leases.get_lease(conn, issue["id"])["holder_id"] == 3
    assert (
        activity.list_activity(
            conn,
            target_kind="issue",
            target_id=issue["id"],
            verb="claim_yielded",
        )
        == []
    )


def test_yield_rejects_absent_expired_and_invalid_requests(tmp_path):
    conn = _migrated_conn(tmp_path / "yield-invalid.db")
    _seed(conn)
    issue = _delegated_issue(conn)

    with pytest.raises(issue_commands.IssueCommandError) as absent:
        lease_commands.yield_claim(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            **_handoff_payload("0" * 32),
        )
    assert absent.value.kind == "conflict"

    expired_lease = _claim(conn, 2, issue["id"])
    conn.execute(
        "UPDATE issue_leases SET expires_at = datetime('now','-1 minute') "
        "WHERE issue_id = ?",
        (issue["id"],),
    )
    conn.commit()
    with pytest.raises(issue_commands.IssueCommandError) as expired:
        lease_commands.yield_claim(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            **_handoff_payload(expired_lease["generation"]),
        )
    assert expired.value.kind == "conflict"

    for reason, note in (
        ("other", None),
        ("blocked", 7),
        ("blocked", " " + ("x" * 499) + " "),
    ):
        with pytest.raises(issue_commands.IssueCommandError) as invalid:
            lease_commands.yield_claim(
                conn,
                actor=_actor(conn, 2),
                issue_id=issue["id"],
                **_handoff_payload(
                    expired_lease["generation"],
                    reason=reason,
                    note=note,
                ),
            )
        assert invalid.value.kind == "invalid"


def test_yield_rolls_back_lease_delete_when_audit_fails(tmp_path, monkeypatch):
    conn = _migrated_conn(tmp_path / "yield-rollback.db")
    _seed(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"])
    lease_before = leases.get_lease(conn, issue["id"])

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(
        lease_commands.issue_activity, "record_claim_yielded", fail_audit
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        lease_commands.yield_claim(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            **_handoff_payload(lease["generation"], reason="capacity"),
        )

    assert leases.get_lease(conn, issue["id"]) == lease_before
    assert (
        activity.list_activity(
            conn,
            target_kind="issue",
            target_id=issue["id"],
            verb="claim_yielded",
        )
        == []
    )


# --- REST parity + a hidden-issue read gate ---------------------------------


def _rest_seed(db_file):
    conn = db.connect(db_file)
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) VALUES ('o@e.com','O','admin',0)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('a@e.com','AgentA',1)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('b@e.com','AgentB',1)"
    )
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
        issue_tag = client.get(f"/issues/{issue['id']}").headers["etag"]
        # AgentA claims (201); AgentB's claim conflicts (409).
        a = client.post(
            f"/issues/{issue['id']}/claim",
            headers={"X-Athena-Actor": "2", "If-Match": issue_tag},
        )
        assert a.status_code == 201 and a.json()["holder_id"] == 2
        b = client.post(
            f"/issues/{issue['id']}/claim",
            headers={"X-Athena-Actor": "3", "If-Match": issue_tag},
        )
        assert b.status_code == 409
        # The lease read now shows AgentA holds it, active.
        lease = client.get(f"/issues/{issue['id']}/lease").json()
        assert lease["holder_id"] == 2 and lease["active"] is True
        # AgentA completes (200) → lease gone, issue still open.
        done = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": lease["generation"]},
            headers={"X-Athena-Actor": "2"},
        )
        assert done.status_code == 200
        body = done.json()
        assert body["released"] is True
        assert body["issue_still_open"] is True
        assert body["issue_status"] == "open"
        again = client.post(
            f"/issues/{issue['id']}/claim",
            headers={"X-Athena-Actor": "3", "If-Match": issue_tag},
        )
        assert again.status_code == 201 and again.json()["holder_id"] == 3


def test_rest_claim_requires_auth(tmp_path):
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        _rest_seed(tmp_path / "auth.db")
        owner = {"X-Athena-Actor": "1"}
        issue = client.post("/issues", json={"title": "work"}, headers=owner).json()
        # Anonymous claim is rejected (no actor).
        assert client.post(f"/issues/{issue['id']}/claim").status_code in (401, 403)


def test_claim_openapi_marks_if_match_required_without_runtime_validation(tmp_path):
    operation = create_app(tmp_path / "openapi.db").openapi()["paths"][
        "/issues/{issue_id}/claim"
    ]["post"]
    parameters = [
        item
        for item in operation["parameters"]
        if item["in"] == "header" and item["name"].lower() == "if-match"
    ]

    assert parameters == [
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "description": "Exactly one strong root issue ETag.",
            "schema": {"type": "string"},
        }
    ]


def test_rest_claim_requires_exact_root_issue_tag(tmp_path):
    app = create_app(tmp_path / "rest-guard.db")
    with TestClient(app) as client:
        _rest_seed(tmp_path / "rest-guard.db")
        owner = {"X-Athena-Actor": "1"}
        agent = {"X-Athena-Actor": "2"}
        created = client.post(
            "/issues", json={"title": "guarded work"}, headers=owner
        ).json()
        client.post(
            f"/issues/{created['id']}/delegate",
            json={"user_id": 2},
            headers=owner,
        )
        context = client.get(f"/issues/{created['id']}/work-context", headers=agent)
        issue_tag = context.json()["issue_etag"]
        context_tag = context.headers["etag"]

        missing = client.post(f"/issues/{created['id']}/claim", headers=agent)
        assert missing.status_code == 428
        assert missing.json() == {
            "detail": "If-Match with exactly one strong issue ETag is required to claim",
            "code": "precondition_required",
        }
        assert "no-store" in missing.headers["cache-control"]
        assert "etag" not in missing.headers

        invalid_conditions = (
            "*",
            "",
            f"W/{issue_tag}",
            f"{issue_tag}, {issue_tag}",
        )
        for condition in invalid_conditions:
            invalid = client.post(
                f"/issues/{created['id']}/claim",
                headers={**agent, "If-Match": condition},
            )
            assert invalid.status_code == 400
            assert invalid.json()["code"] == "invalid_if_match"

        oversized = client.post(
            f"/issues/{created['id']}/claim",
            headers={**agent, "If-Match": '"' + ("x" * 8200) + '"'},
        )
        assert oversized.status_code == 431
        assert oversized.json()["code"] == "if_match_too_large"
        assert client.get(f"/issues/{created['id']}/lease").json() is None

        wrong_scope = client.post(
            f"/issues/{created['id']}/claim",
            headers={**agent, "If-Match": context_tag},
        )
        assert wrong_scope.status_code == 412
        assert wrong_scope.headers["etag"] == issue_tag

        claimed = client.post(
            f"/issues/{created['id']}/claim",
            headers={**agent, "If-Match": issue_tag},
        )
        assert claimed.status_code == 201
        assert "etag" not in claimed.headers


def test_rest_stale_claim_and_idempotent_retry_are_side_effect_safe(tmp_path):
    app = create_app(tmp_path / "rest-stale.db")
    with TestClient(app) as client:
        _rest_seed(tmp_path / "rest-stale.db")
        owner = {"X-Athena-Actor": "1"}
        agent = {"X-Athena-Actor": "2"}
        created_response = client.post(
            "/issues", json={"title": "review me"}, headers=owner
        )
        issue = created_response.json()
        reviewed_tag = created_response.headers["etag"]
        client.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": 2},
            headers=owner,
        )
        edited = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "changed"},
            headers={**owner, "If-Match": reviewed_tag},
        )
        current_tag = edited.headers["etag"]

        stale = client.post(
            f"/issues/{issue['id']}/claim",
            headers={**agent, "If-Match": reviewed_tag},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "precondition_failed"
        assert stale.headers["etag"] == current_tag
        assert client.get(f"/issues/{issue['id']}/lease").json() is None

        headers = {
            **agent,
            "If-Match": current_tag,
            "Idempotency-Key": "claim-once",
        }
        first = client.post(f"/issues/{issue['id']}/claim", headers=headers)
        replay = client.post(f"/issues/{issue['id']}/claim", headers=headers)
        assert first.status_code == replay.status_code == 201
        assert replay.headers["idempotent-replay"] == "true"
        assert replay.json() == first.json()

        mismatch = client.post(
            f"/issues/{issue['id']}/claim",
            headers={
                **agent,
                "If-Match": '"different"',
                "Idempotency-Key": "claim-once",
            },
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "idempotency_mismatch"
        events = client.get("/events", params={"kind": "issue"}, headers=owner).json()
        assert [event["verb"] for event in events["events"]].count("claimed") == 1


def test_rest_yield_is_run_stamped_and_idempotent_without_reassignment(tmp_path):
    app = create_app(tmp_path / "rest-yield.db")
    with TestClient(app) as client:
        _rest_seed(tmp_path / "rest-yield.db")
        owner = {"X-Athena-Actor": "1"}
        agent = {"X-Athena-Actor": "2"}
        created = client.post(
            "/issues", json={"title": "yieldable"}, headers=owner
        ).json()
        client.post(
            f"/issues/{created['id']}/delegate",
            json={"user_id": 2},
            headers=owner,
        )
        issue_tag = client.get(f"/issues/{created['id']}").headers["etag"]
        claimed = client.post(
            f"/issues/{created['id']}/claim",
            headers={**agent, "If-Match": issue_tag},
        )
        assert claimed.status_code == 201

        yield_headers = {
            **agent,
            "X-Athena-Run": "yield-run",
            "Idempotency-Key": "yield-once",
        }
        body = _handoff_payload(
            claimed.json()["generation"],
            reason="blocked",
            note=" waiting on operator ",
        )
        first = client.post(
            f"/issues/{created['id']}/yield",
            json=body,
            headers=yield_headers,
        )
        replay = client.post(
            f"/issues/{created['id']}/yield",
            json=body,
            headers=yield_headers,
        )
        assert first.status_code == replay.status_code == 201
        assert replay.headers["idempotent-replay"] == "true"
        assert replay.json() == first.json()
        assert first.json()["state"] == "awaiting_resume"
        assert client.get(f"/issues/{created['id']}/lease").json() is None
        issue = client.get(f"/issues/{created['id']}").json()
        assert issue["assignee_id"] is None
        contributors = client.get(
            f"/issues/{created['id']}/contributors", headers=owner
        ).json()
        assert [row["user_id"] for row in contributors] == [2]

        mismatch = client.post(
            f"/issues/{created['id']}/yield",
            json={"reason": "capacity"},
            headers=yield_headers,
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "idempotency_mismatch"

        events = client.get("/events", params={"kind": "issue"}, headers=owner).json()
        yielded = [
            event for event in events["events"] if event["verb"] == "claim_yielded"
        ]
        assert len(yielded) == 1
        assert yielded[0]["detail"] == (
            f"generation {claimed.json()['generation']}; "
            f"handoff {first.json()['handoff_token']}; "
            "blocked: What should happen next?"
        )
        assert yielded[0]["run_id"] == "yield-run"

        for invalid_body in (
            {"reason": "other"},
            {"reason": "blocked", "note": "x" * 501},
            {"reason": "blocked", "extra": True},
        ):
            invalid = client.post(
                f"/issues/{created['id']}/yield",
                json=invalid_body,
                headers=agent,
            )
            assert invalid.status_code == 422


def test_rest_yield_conflicts_are_holder_only_and_side_effect_free(tmp_path):
    db_path = tmp_path / "rest-yield-conflicts.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _rest_seed(db_path)
        owner = {"X-Athena-Actor": "1"}
        agent_a = {"X-Athena-Actor": "2"}
        agent_b = {"X-Athena-Actor": "3"}
        issue = client.post(
            "/issues", json={"title": "holder only"}, headers=owner
        ).json()
        for user_id in (2, 3):
            delegated = client.post(
                f"/issues/{issue['id']}/delegate",
                json={"user_id": user_id},
                headers=owner,
            )
            assert delegated.status_code == 201

        absent = client.post(
            f"/issues/{issue['id']}/yield",
            json=_handoff_payload("0" * 32),
            headers=agent_a,
        )
        assert absent.status_code == 409

        issue_tag = client.get(f"/issues/{issue['id']}").headers["etag"]
        claimed = client.post(
            f"/issues/{issue['id']}/claim",
            headers={**agent_a, "If-Match": issue_tag},
        )
        assert claimed.status_code == 201
        active_generation = claimed.json()["generation"]

        for nonholder in (agent_b, owner):
            rejected = client.post(
                f"/issues/{issue['id']}/yield",
                json=_handoff_payload(active_generation, reason="capacity"),
                headers=nonholder,
            )
            assert rejected.status_code == 409
            assert client.get(f"/issues/{issue['id']}/lease").json()["holder_id"] == 2

        conn = db.connect(db_path)
        conn.execute(
            "UPDATE issue_leases SET expires_at = datetime('now','-1 minute') "
            "WHERE issue_id = ?",
            (issue["id"],),
        )
        conn.commit()
        conn.close()
        expired = client.post(
            f"/issues/{issue['id']}/yield",
            json=_handoff_payload(active_generation, reason="needs_input"),
            headers=agent_a,
        )
        assert expired.status_code == 409

        events = client.get("/events", params={"kind": "issue"}, headers=owner).json()
        assert all(event["verb"] != "claim_yielded" for event in events["events"])
