"""Server-owned pickup serializes selection, possession checks and acquisition."""

from fastapi.testclient import TestClient
import pytest

from athena.core import db, packet_time
from athena.main import create_app

ADMIN = {"X-Athena-Actor": "1"}


def checked(response, status=201):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "pickup.db"
    app = create_app(path)
    with TestClient(app) as client:
        checked(
            client.post(
                "/users",
                json={"email": "admin@test.example", "name": "Admin", "password": "pw"},
            )
        )
        agent = checked(
            client.post(
                "/users/onboard_agent",
                json={
                    "email": "worker@test.example",
                    "name": "Worker",
                    "scopes": ["read", "issue:write"],
                },
                headers=ADMIN,
            )
        )
        yield client, path, app, agent


def bearer(agent):
    return {"Authorization": f"Bearer {agent['token']['token']}"}


def issue(client, agent, title="Work", **fields):
    row = checked(
        client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    )
    if agent is not None:
        checked(
            client.post(
                f"/issues/{row['id']}/delegate",
                json={"user_id": agent["user"]["id"]},
                headers=ADMIN,
            )
        )
    return row


def pickup(client, agent, payload=None, **headers):
    return client.post(
        "/issues/claim-next",
        json={} if payload is None else payload,
        headers={**bearer(agent), **headers},
    )


def counts(path):
    conn = db.connect(path)
    try:
        return (
            conn.execute("SELECT COUNT(*) FROM issue_leases").fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM activity WHERE verb = 'claimed'"
            ).fetchone()[0],
        )
    finally:
        conn.close()


def test_pickup_claims_current_top_and_never_renews(setup):
    client, path, _app, agent = setup
    top = issue(client, agent, "Top", priority="urgent")
    issue(client, agent, "Later", priority="low")
    response = pickup(client, agent)
    result = checked(response, 200)
    assert result["schema"] == "athena.ready_claim.v1"
    assert result["claimed"] is True and result["issue"]["id"] == top["id"]
    assert result["attempts"] == []
    assert result["previous_lease"] is None and result["open_handoff"] is None
    assert result["lease"]["path_fence"] == "issue_only"
    assert result["lease"]["active"] is True
    assert len(result["lease"]["generation"]) == 32
    context = client.get(f"/issues/{top['id']}/work-context", headers=bearer(agent))
    assert result["issue_etag"] == context.json()["issue_etag"]
    assert result["work_context_etag"] == context.headers["etag"]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization, X-Athena-Actor"
    assert "etag" not in response.headers
    again = checked(pickup(client, agent), 200)
    assert again["reason"] == "already_holding" and again["held_by_you"] == 1
    assert again["attempts"] == []
    current = client.get(f"/issues/{top['id']}/lease", headers=bearer(agent)).json()
    assert current["generation"] == result["lease"]["generation"]
    assert current["expires_at"] == result["lease"]["expires_at"]
    assert counts(path) == (1, 1)


def execute(path, sql, params=()):
    conn = db.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def direct_claim(client, agent, item):
    tag = client.get(f"/issues/{item['id']}", headers=bearer(agent)).headers["etag"]
    return checked(
        client.post(
            f"/issues/{item['id']}/claim", headers={**bearer(agent), "If-Match": tag}
        )
    )


def project(client, key="ONE"):
    return checked(
        client.post("/projects", json={"name": key, "key": key}, headers=ADMIN)
    )


def command_actor(path, agent):
    from athena.core import users

    conn = db.connect(path)
    try:
        return users.get_user(conn, agent["user"]["id"])
    finally:
        conn.close()


def second_token(path, agent, scopes=("read", "issue:write")):
    from athena.core import tokens

    conn = db.connect(path)
    try:
        token = tokens.create_token(
            conn,
            user_id=agent["user"]["id"],
            name="Other credential",
            scopes=scopes,
            commit=True,
        )
    finally:
        conn.close()
    return {"user": agent["user"], "token": token}


@pytest.mark.parametrize(
    "payload",
    [{"limit": value} for value in (0, 6, True, 1.5, "1", None)]
    + [{"project_id": value} for value in (0, -1, 2**63, True, 1.5, "1")]
    + [{"lease_seconds": value} for value in (0, -1, 2**63, True, 60.5, "60")]
    # Pickup takes paths (C6): malformed ones refuse before selection.
    + [{"paths": value} for value in ("src", ["../x"], ["/abs"], [1], [""])]
    + [{"generation": "a" * 32}, {"coordination_root": None}, {"extra": 1}],
)
def test_strict_request_refuses_before_claim(setup, payload):
    client, path, _app, agent = setup
    issue(client, agent)
    assert pickup(client, agent, payload).status_code == 422
    assert counts(path) == (0, 0)


def test_empty_body_defaults_and_unsupported_precondition(setup):
    client, path, _app, agent = setup
    for tag in ('"current"', "", "*"):
        assert pickup(client, agent, **{"If-Match": tag}).status_code == 422
    empty = checked(client.post("/issues/claim-next", headers=bearer(agent)), 200)
    assert empty["reason"] == "nothing_ready" and empty["ready_total"] == 0
    assert empty["attempts"] == []
    assert counts(path) == (0, 0)


@pytest.mark.parametrize(
    "scopes,expected",
    [(("read",), 403), (("docs:write",), 403), (("issue:write",), 200)],
)
def test_pickup_uses_issue_write_scope_without_read_requirement(
    setup, scopes, expected
):
    client, path, _app, agent = setup
    issue(client, agent)
    credential = second_token(path, agent, scopes)
    response = pickup(client, credential)
    assert response.status_code == expected, response.text
    assert counts(path) == ((1, 1) if expected == 200 else (0, 0))
    assert client.post("/issues/claim-next", json={}).status_code == 401


def test_project_filter_applies_before_limit_and_preserves_backlog(setup):
    from athena.aegis import ready_queue

    client, path, _app, agent = setup
    chosen = project(client)
    issue(client, agent, "Global urgent", priority="urgent")
    selected = issue(
        client, agent, "Project low", priority="low", project_id=chosen["id"]
    )
    conn = db.connect(path)
    try:
        lane = ready_queue.build_ready(
            conn, actor=command_actor(path, agent), limit=1, project_id=chosen["id"]
        )
        assert lane["total"] == 1 and lane["items"][0]["issue"]["id"] == selected["id"]
    finally:
        conn.close()
    result = checked(pickup(client, agent, {"project_id": chosen["id"]}), 200)
    assert result["issue"]["id"] == selected["id"]
    assert (
        checked(pickup(client, agent, {"project_id": None}), 200)["reason"]
        == "already_holding"
    )


def test_hidden_and_missing_project_refuse_even_when_holding(setup):
    client, path, _app, agent = setup
    hidden = project(client)
    checked(
        client.put(
            f"/projects/{hidden['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        ),
        200,
    )
    direct_claim(client, agent, issue(client, agent))
    invisible = pickup(client, agent, {"project_id": hidden["id"]})
    missing = pickup(client, agent, {"project_id": 999999})
    assert invisible.status_code == missing.status_code == 404
    assert (
        invisible.json()
        == missing.json()
        == {"detail": "no such project", "code": "not_found"}
    )
    assert invisible.headers["cache-control"] == "private, no-store"
    assert invisible.headers["vary"] == "Authorization, X-Athena-Actor"
    assert counts(path) == (1, 1)


@pytest.mark.parametrize("condition", ["hidden", "archived", "done"])
def test_account_guard_includes_other_project_and_unlisted_possession(setup, condition):
    client, path, _app, agent = setup
    first, other = project(client), project(client, "TWO")
    held = issue(client, agent, "Hidden possession sentinel", project_id=first["id"])
    direct_claim(client, agent, held)
    if condition == "hidden":
        checked(
            client.put(
                f"/projects/{first['id']}/visibility",
                json={"visibility": "private"},
                headers=ADMIN,
            ),
            200,
        )
    elif condition == "archived":
        execute(
            path,
            "UPDATE issues SET archived_at=datetime('now') WHERE id=?",
            (held["id"],),
        )
    else:
        execute(path, "UPDATE issues SET status='done' WHERE id=?", (held["id"],))
    issue(client, agent, project_id=other["id"])
    result = checked(pickup(client, agent, {"project_id": other["id"]}), 200)
    assert result["reason"] == "already_holding" and result["held_by_you"] == 1
    assert set(result) == {
        "schema",
        "observed_at",
        "claimed",
        "attempts",
        "reason",
        "held_by_you",
        "review_request",
    }
    # No review is asked of this account, and the answer says so (D16).
    assert result["review_request"] is None
    assert counts(path) == (1, 1)


def test_direct_claim_policy_remains_readiness_independent_and_multiple(setup):
    client, path, _app, agent = setup
    first = issue(client, agent)
    checked(pickup(client, agent), 200)
    blocked = issue(client, agent, "Blocked")
    blocker = issue(client, None, "Blocker")
    checked(
        client.post(
            f"/issues/{blocked['id']}/links",
            json={"target_ref": str(blocker["id"]), "relation": "blocked_by"},
            headers=ADMIN,
        )
    )
    direct_claim(client, agent, blocked)
    assert first["id"] != blocked["id"] and counts(path) == (2, 2)


@pytest.mark.parametrize("same_account", [False, True])
def test_concurrent_http_pickup_has_one_winner_per_account_or_issue(
    setup, same_account
):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    client, path, _app, agent = setup
    top = issue(client, agent, "Top")
    if same_account:
        rival = second_token(path, agent)
        issue(client, agent, "Second")
    else:
        rival = checked(
            client.post(
                "/users/onboard_agent",
                json={
                    "email": "rival@test.example",
                    "name": "Rival",
                    "scopes": ["read", "issue:write"],
                },
                headers=ADMIN,
            )
        )
        checked(
            client.post(
                f"/issues/{top['id']}/delegate",
                json={"user_id": rival["user"]["id"]},
                headers=ADMIN,
            )
        )
    barrier = threading.Barrier(2)

    def compete(credential, run):
        with TestClient(create_app(path)) as transport:
            barrier.wait(timeout=10)
            return checked(pickup(transport, credential, **{"X-Athena-Run": run}), 200)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(compete, credential, run)
            for credential, run in ((agent, "first"), (rival, "second"))
        ]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(result["claimed"] for result in outcomes) == [False, True]
    refused = next(result for result in outcomes if not result["claimed"])
    assert refused["reason"] == ("already_holding" if same_account else "nothing_ready")
    assert counts(path) == (1, 1)
    conn = db.connect(path)
    try:
        assert conn.execute(
            "SELECT run_id FROM activity WHERE verb='claimed'"
        ).fetchone()[0] in {"first", "second"}
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change", ["blocker", "done", "archived", "eligibility", "visibility"]
)
def test_waiting_pickup_reads_writer_change_before_selection(setup, change):
    """Equivalent GREEN for baseline RED, at the actual server writer boundary."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from athena.aegis import ready_claim_commands, ready_queue

    client, path, _app, agent = setup
    group = project(client)
    target = issue(client, agent, project_id=group["id"])
    blocker = issue(client, None, "Blocker")
    actor = command_actor(path, agent)
    writer, worker = db.connect(path), db.connect(path)
    entered = threading.Event()
    worker.set_trace_callback(
        lambda sql: entered.set() if sql == "BEGIN IMMEDIATE" else None
    )
    assert ready_queue.build_ready(worker, actor=actor)["total"] == 1
    writer.execute("BEGIN IMMEDIATE")
    if change == "blocker":
        writer.execute(
            "INSERT INTO issue_links(from_id,to_id,kind,created_by) VALUES(?,?,'blocks',1)",
            (blocker["id"], target["id"]),
        )
    elif change == "done":
        writer.execute("UPDATE issues SET status='done' WHERE id=?", (target["id"],))
    elif change == "archived":
        writer.execute(
            "UPDATE issues SET archived_at=datetime('now') WHERE id=?", (target["id"],)
        )
    elif change == "eligibility":
        writer.execute(
            "DELETE FROM issue_contributors WHERE issue_id=?", (target["id"],)
        )
    else:
        writer.execute(
            "UPDATE projects SET visibility='private' WHERE id=?", (group["id"],)
        )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                ready_claim_commands.claim_next_ready, worker, actor=actor
            )
            try:
                assert entered.wait(timeout=5)
                assert not future.done()
            finally:
                writer.commit()
            result = future.result(timeout=10)
        assert result["reason"] == "nothing_ready"
        assert not worker.in_transaction and counts(path) == (0, 0)
        if change == "blocker":
            direct_claim(client, agent, target)
            assert counts(path) == (1, 1)
    finally:
        writer.close()
        worker.close()


def test_waiting_pickup_samples_clock_after_reservation(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta
    import threading
    from athena.aegis import ready_claim_commands, leases

    client, path, _app, agent = setup
    held = issue(client, agent, "Held", status="done")
    direct_claim(client, agent, held)
    issue(client, agent, "New")
    actor = command_actor(path, agent)
    before = datetime.now(UTC)
    after = before + timedelta(hours=2)
    execute(
        path,
        "UPDATE issue_leases SET expires_at=?",
        (leases.clock_stamp(after),),
    )
    reserved, sampled = threading.Event(), threading.Event()
    writer, worker = db.connect(path), db.connect(path)
    worker.set_trace_callback(
        lambda sql: reserved.set() if sql == "BEGIN IMMEDIATE" else None
    )

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is UTC
            sampled.set()
            return after

    monkeypatch.setattr(ready_claim_commands, "datetime", Clock)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                ready_claim_commands.claim_next_ready, worker, actor=actor
            )
            try:
                assert reserved.wait(timeout=5)
                assert not sampled.is_set()
            finally:
                writer.commit()
            result = future.result(timeout=10)
        assert sampled.is_set() and result["claimed"] is True
        assert result["observed_at"] == leases.clock_stamp(after)
        assert not worker.in_transaction
    finally:
        writer.close()
        worker.close()


@pytest.mark.parametrize("mode", ["BEGIN", "BEGIN IMMEDIATE"])
def test_nested_call_refuses_without_committing_or_rolling_back_caller(setup, mode):
    from athena.aegis import ready_claim_commands
    from athena.aegis.issue_commands import IssueCommandError

    client, path, _app, agent = setup
    target = issue(client, agent)
    actor = command_actor(path, agent)
    conn = db.connect(path)
    try:
        conn.execute(mode)
        conn.execute("UPDATE issues SET title='Pending' WHERE id=?", (target["id"],))
        with pytest.raises(
            IssueCommandError, match="without an open transaction"
        ) as caught:
            ready_claim_commands.claim_next_ready(conn, actor=actor)
        assert caught.value.kind == "invalid" and conn.in_transaction
        assert (
            conn.execute(
                "SELECT title FROM issues WHERE id=?", (target["id"],)
            ).fetchone()[0]
            == "Pending"
        )
        conn.rollback()
        assert counts(path) == (0, 0)
    finally:
        conn.close()


@pytest.mark.parametrize("failure", ["audit", "context", "context_none"])
def test_precommit_failure_rolls_back_claim_and_event(setup, monkeypatch, failure):
    from athena.aegis import issue_activity, ready_claim_commands, work_context

    client, path, _app, agent = setup
    issue(client, agent)
    actor = command_actor(path, agent)

    def fail(*args, **kwargs):
        if failure == "context_none":
            return None
        raise RuntimeError("injected precommit failure")

    if failure == "audit":
        monkeypatch.setattr(issue_activity, "record_issue_claimed", fail)
    else:
        monkeypatch.setattr(work_context, "build_work_context", fail)
    conn = db.connect(path)
    try:
        with pytest.raises(RuntimeError):
            ready_claim_commands.claim_next_ready(conn, actor=actor)
        assert not conn.in_transaction
        assert counts(path) == (0, 0)
    finally:
        conn.close()


@pytest.mark.parametrize("limit", [1, 2])
def test_bounded_injected_candidate_conflict_is_generic_and_rolls_back(
    setup, monkeypatch, limit
):
    """Composition coverage; issue-only pickup has no normal path-fence conflict."""
    from athena.aegis import lease_commands
    from athena.aegis.issue_commands import IssueCommandError

    client, path, _app, agent = setup
    top = issue(client, agent, "First", priority="urgent")
    next_item = issue(client, agent, "Second")
    original = lease_commands.claim_issue

    def conflict(conn, **kwargs):
        # A nested command refusal must roll back even if its owner touched rows.
        with db.transaction(conn):
            lease = original(conn, **kwargs)
            if kwargs["issue_id"] == top["id"]:
                raise IssueCommandError("conflict", "private conflict sentinel")
            return lease

    monkeypatch.setattr(lease_commands, "claim_issue", conflict)
    result = checked(pickup(client, agent, {"limit": limit}), 200)
    assert result["attempts"] == [{"issue_id": top["id"], "outcome": "conflict"}]
    assert "private conflict sentinel" not in str(result)
    if limit == 1:
        assert result["reason"] == "candidates_conflicted" and counts(path) == (0, 0)
    else:
        assert result["claimed"] is True and result["issue"]["id"] == next_item["id"]
        assert counts(path) == (1, 1)


def test_historical_keyed_success_survives_restart_and_release_without_reacquiring(
    setup,
):
    client, path, _app, agent = setup
    target = issue(client, agent)
    headers = {"Idempotency-Key": "pickup-once", "X-Athena-Run": "pickup-run"}
    first = checked(pickup(client, agent, **headers), 200)
    checked(
        client.post(
            f"/issues/{target['id']}/complete",
            json={"generation": first["lease"]["generation"]},
            headers=bearer(agent),
        ),
        200,
    )
    assert counts(path) == (0, 1)
    # Completion holds the issue back from its completer until another account
    # answers (docs/decisions/2026-11-22-ready-rule-awaiting-decision.md).
    checked(
        client.post(
            f"/issues/{target['id']}/comments", json={"body": "Again"}, headers=ADMIN
        )
    )
    with TestClient(create_app(path)) as restarted:
        replay = pickup(restarted, agent, **headers)
        assert replay.headers["idempotent-replay"] == "true"
        assert checked(replay, 200) == first
        assert counts(path) == (0, 1)
        new = checked(
            pickup(restarted, agent, **{**headers, "Idempotency-Key": "new-intent"}),
            200,
        )
        assert (
            new["claimed"]
            and new["lease"]["generation"] != first["lease"]["generation"]
        )
    assert counts(path) == (1, 2)


def test_expired_success_replay_is_historical_new_pickup_reacquires(setup):
    client, path, _app, agent = setup
    target = issue(client, agent)
    headers = {"Idempotency-Key": "before-expiry"}
    first = checked(pickup(client, agent, **headers), 200)
    execute(
        path,
        "UPDATE issue_leases SET expires_at='2000-01-01 00:00:00' WHERE issue_id=?",
        (target["id"],),
    )
    assert checked(pickup(client, agent, **headers), 200) == first
    assert counts(path) == (1, 1)
    renewed = checked(pickup(client, agent), 200)
    assert renewed["lease"]["generation"] != first["lease"]["generation"]
    assert renewed["previous_lease"]["holder_id"] == agent["user"]["id"]
    assert renewed["previous_lease"]["expired_at"] == "2000-01-01T00:00:00Z"
    assert counts(path) == (1, 2)


def test_negative_receipt_requires_new_poll_key_and_detects_a_body_mismatch(
    setup,
):
    client, path, _app, agent = setup
    headers = {"Idempotency-Key": "empty-once", "X-Athena-Run": "run-a"}
    original = checked(pickup(client, agent, **headers), 200)
    assert original["reason"] == "nothing_ready"
    issue(client, agent)
    assert checked(pickup(client, agent, **headers), 200) == original
    assert counts(path) == (0, 0)
    for payload in ({"limit": 2}, {"project_id": 999}):
        refused = pickup(client, agent, payload, **headers)
        assert (
            refused.status_code == 409
            and refused.json()["code"] == "idempotency_mismatch"
        )
    # The key names the poll, not the session (F14): the same poll under
    # another run is the same receipt, marked as a replay, and claims nothing.
    other_run = pickup(client, agent, **{**headers, "X-Athena-Run": "run-b"})
    assert checked(other_run, 200) == original
    assert other_run.headers["idempotent-replay"] == "true"
    assert counts(path) == (0, 0)
    acquired = checked(
        pickup(client, agent, **{**headers, "Idempotency-Key": "new-poll"}), 200
    )
    assert acquired["claimed"] and counts(path) == (1, 1)


def test_simultaneous_same_key_executes_one_claim(setup):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    client, path, _app, agent = setup
    issue(client, agent)
    barrier = threading.Barrier(2)

    def request():
        with TestClient(create_app(path)) as transport:
            barrier.wait(timeout=10)
            return pickup(transport, agent, **{"Idempotency-Key": "shared-key"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    assert checked(responses[0], 200) == checked(responses[1], 200)
    assert sum(r.headers.get("idempotent-replay") == "true" for r in responses) == 1
    assert counts(path) == (1, 1)


@pytest.mark.parametrize(
    "change,expected", [("pause", 403), ("revoke", 401), ("membership", 409)]
)
def test_authority_change_refuses_pickup_receipt_replay(setup, change, expected):
    client, path, _app, agent = setup
    group = project(client)
    checked(
        client.put(
            f"/projects/{group['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        ),
        200,
    )
    checked(
        client.post(
            f"/projects/{group['id']}/members",
            json={"user_id": agent["user"]["id"]},
            headers=ADMIN,
        )
    )
    issue(client, agent, project_id=group["id"])
    headers = {"Idempotency-Key": "authority-bound"}
    checked(pickup(client, agent, **headers), 200)
    if change == "pause":
        checked(
            client.put(
                f"/users/{agent['user']['id']}/paused",
                json={"paused": True},
                headers=ADMIN,
            ),
            200,
        )
    elif change == "revoke":
        response = client.delete(
            f"/tokens/{agent['token']['id']}",
            headers={"X-Athena-Actor": str(agent["user"]["id"])},
        )
        assert response.status_code == 204, response.text
    else:
        response = client.delete(
            f"/projects/{group['id']}/members/{agent['user']['id']}", headers=ADMIN
        )
        assert response.status_code == 200, response.text
    refused = pickup(client, agent, **headers)
    assert refused.status_code == expected, refused.text
    assert "idempotent-replay" not in refused.headers
    assert counts(path) == (1, 1)


def test_postcommit_receipt_failure_keeps_claim_and_fails_closed(setup, monkeypatch):
    from athena.core import idempotency

    client, path, _app, agent = setup
    issue(client, agent)
    original = idempotency.complete

    def fail(*args, **kwargs):
        raise RuntimeError("injected publication failure")

    with TestClient(create_app(path), raise_server_exceptions=False) as transport:
        monkeypatch.setattr(idempotency, "complete", fail)
        response = pickup(transport, agent, **{"Idempotency-Key": "ambiguous"})
        monkeypatch.setattr(idempotency, "complete", original)
        assert response.status_code == 500
        assert counts(path) == (
            1,
            1,
        )  # Domain commit cannot be rolled back by publication.
        retry = pickup(transport, agent, **{"Idempotency-Key": "ambiguous"})
        assert (
            retry.status_code == 409
            and retry.json()["code"] == "idempotency_indeterminate"
        )
        assert counts(path) == (1, 1)


def test_open_handoff_survives_pickup_and_requires_explicit_resume(setup):
    client, path, _app, agent = setup
    target = issue(client, agent)
    first = checked(pickup(client, agent), 200)
    handoff = checked(
        client.post(
            f"/issues/{target['id']}/yield",
            json={
                "generation": first["lease"]["generation"],
                "reason": "needs_input",
                "attempted_work": "Read specification",
                "evidence": [],
                "blocking_question": "Which branch?",
                "resume_instructions": "Read operator answer",
            },
            headers=bearer(agent),
        )
    )
    # A needs_input yield is offered to nobody until another account answers.
    assert checked(pickup(client, agent), 200)["reason"] == "nothing_ready"
    checked(
        client.post(
            f"/issues/{target['id']}/comments", json={"body": "Main"}, headers=ADMIN
        )
    )
    next_claim = checked(pickup(client, agent), 200)
    assert next_claim["lease"]["generation"] != first["lease"]["generation"]
    assert (
        next_claim["lease"]["open_claim_handoff"]["handoff_token"]
        == handoff["handoff_token"]
    )
    assert next_claim["open_handoff"]["handoff_token"] == handoff["handoff_token"]
    refused = client.post(
        f"/issues/{target['id']}/complete",
        json={"generation": next_claim["lease"]["generation"]},
        headers=bearer(agent),
    )
    assert refused.status_code == 409
    assert counts(path) == (1, 2)


@pytest.mark.parametrize(
    "status", ["open", "in_progress", "done", "custom", "stale_unknown"]
)
def test_selection_preserves_ready_status_semantics(setup, status):
    from athena.aegis import ready_queue

    client, path, _app, agent = setup
    group = project(client)
    target = issue(client, agent, project_id=group["id"])
    if status == "custom":
        execute(
            path,
            "UPDATE project_statuses SET name='custom' WHERE project_id=? AND category='todo'",
            (group["id"],),
        )
    execute(path, "UPDATE issues SET status=? WHERE id=?", (status, target["id"]))
    conn = db.connect(path)
    try:
        lane = ready_queue.build_ready(conn, actor=command_actor(path, agent))
    finally:
        conn.close()
    result = checked(pickup(client, agent), 200)
    assert result["claimed"] is (status != "done")
    assert result["claimed"] is bool(lane["items"])
    if result["claimed"]:
        # The pickup answers the lane's issue in the packet shape (A13).
        assert result["issue"] == packet_time.stamp(
            lane["items"][0]["issue"], now=packet_time.clock()
        )


def test_hidden_archived_open_blocker_blocks_without_metadata_disclosure(setup):
    client, path, _app, agent = setup
    group = project(client)
    target = issue(client, agent)
    blocker = issue(client, None, "Private blocker sentinel", project_id=group["id"])
    checked(
        client.post(
            f"/issues/{target['id']}/links",
            json={"target_ref": str(blocker["id"]), "relation": "blocked_by"},
            headers=ADMIN,
        )
    )
    execute(
        path,
        "UPDATE issues SET archived_at=datetime('now') WHERE id=?",
        (blocker["id"],),
    )
    checked(
        client.put(
            f"/projects/{group['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        ),
        200,
    )
    result = checked(pickup(client, agent), 200)
    assert result["reason"] == "nothing_ready"
    assert "Private blocker sentinel" not in str(result)
    execute(path, "UPDATE issues SET status='done' WHERE id=?", (blocker["id"],))
    assert checked(pickup(client, agent), 200)["claimed"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": True},
        {"limit": 6},
        {"lease_seconds": False},
        {"lease_seconds": 0},
        {"project_id": True},
        {"project_id": 2**63},
    ],
)
def test_command_rejects_invalid_inputs_without_transaction_or_writes(setup, kwargs):
    from athena.aegis import ready_claim_commands
    from athena.aegis.issue_commands import IssueCommandError

    _client, path, _app, agent = setup
    conn = db.connect(path)
    try:
        with pytest.raises(IssueCommandError) as caught:
            ready_claim_commands.claim_next_ready(
                conn, actor=command_actor(path, agent), **kwargs
            )
        assert caught.value.kind == "invalid" and not conn.in_transaction
        assert counts(path) == (0, 0)
    finally:
        conn.close()


def test_creator_without_assignment_or_delegation_is_not_eligible(setup):
    client, path, _app, agent = setup
    checked(
        client.post("/issues", json={"title": "Creator only"}, headers=bearer(agent))
    )
    assert checked(pickup(client, agent), 200)["reason"] == "nothing_ready"
    assert counts(path) == (0, 0)


def test_unexpected_claim_error_propagates_without_trying_another_candidate(
    setup, monkeypatch
):
    from athena.aegis import lease_commands
    from athena.aegis.issue_commands import IssueCommandError

    client, path, _app, agent = setup
    first = issue(client, agent, "First", priority="urgent")
    issue(client, agent, "Second")
    seen = []

    def refuse(conn, **kwargs):
        seen.append(kwargs["issue_id"])
        raise IssueCommandError(
            "precondition_failed", "injected stale revision", current_etag='"changed"'
        )

    monkeypatch.setattr(lease_commands, "claim_issue", refuse)
    response = pickup(client, agent, {"limit": 2})
    assert response.status_code == 412
    assert response.headers["etag"] == '"changed"'
    assert response.headers["cache-control"] == "private, no-store"
    assert seen == [first["id"]] and counts(path) == (0, 0)


def test_each_account_can_acquire_distinct_ready_work_in_parallel(setup):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    client, path, _app, agent = setup
    rival = checked(
        client.post(
            "/users/onboard_agent",
            json={
                "email": "other@test.example",
                "name": "Other",
                "scopes": ["read", "issue:write"],
            },
            headers=ADMIN,
        )
    )
    items = [issue(client, agent, f"Work {index}") for index in range(2)]
    for item in items:
        checked(
            client.post(
                f"/issues/{item['id']}/delegate",
                json={"user_id": rival["user"]["id"]},
                headers=ADMIN,
            )
        )
    barrier = threading.Barrier(2)

    def compete(credential):
        with TestClient(create_app(path)) as transport:
            barrier.wait(timeout=10)
            return checked(pickup(transport, credential), 200)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, credential) for credential in (agent, rival)]
        outcomes = [future.result(timeout=20) for future in futures]
    assert all(result["claimed"] for result in outcomes)
    assert {result["issue"]["id"] for result in outcomes} == {
        item["id"] for item in items
    }
    assert counts(path) == (2, 2)
