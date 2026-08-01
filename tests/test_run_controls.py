"""Run controls: an operator's recorded requests on a live run, and the answers.

The contract under test is RUN_CONTROLS.md's honesty discipline: a control is a
REQUEST record bound to the run's owning agent at admission; only that agent can
read and settle it; every lifecycle fact is somebody's explicit claim recorded
atomically with its activity event; and "expired" is always the server clock's
verdict at read time, never a stored state. Nothing here may let Athena claim a
run was steered, cancelled, or refreshed — only what was asked and what the
bound agent said back.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from athena.core import db, run_control_commands, run_controls, tokens
from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError

H1 = {"X-Athena-Actor": "1"}

HANDOFF = {
    "summary": "Migrated the schema; tests halfway green.",
    "unresolved_questions": ["Should the legacy rows be backfilled?"],
    "athena_refs": ["issue:12", "run:goal-1"],
    "evidence_refs": ["pytest -k migration output"],
}


def _app(tmp_path, name="run_controls.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", name="Sol", scopes=("read", "issue:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _bind_run(client, bearer, run_id, title="tagged work"):
    """Bind a run id to the bearer's identity by writing one tagged event."""
    response = client.post(
        "/issues", json={"title": title}, headers={**bearer, "X-Athena-Run": run_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create(client, run_id, kind="steer", payload="use approach B", **extra):
    body = {"run_id": run_id, "kind": kind, "payload": payload, **extra}
    if body["payload"] is None:
        del body["payload"]
    return client.post("/run-controls", json=body, headers=H1)


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT id, actor_id, target_kind, target_id, detail, run_id "
        "FROM activity WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _actor(db_file, raw_token):
    """Resolve a live bearer actor dict exactly as the identity layer would."""
    conn = db.connect(db_file)
    try:
        actor = tokens.resolve_token(conn, raw_token)
        assert actor is not None
        return dict(actor)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy paths — one per control kind, over real HTTP.
# ---------------------------------------------------------------------------


def test_steer_control_full_lifecycle(tmp_path):
    """The whole loop: admin records guidance, the bound agent acknowledges and
    completes, and every fact lands as the right actor's activity event linked
    from the control row — the trail and the record vouch for each other."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-1")

        created = _create(client, "goal-1", payload="Focus on the flaky test")
        assert created.status_code == 201
        control = created.json()
        assert control["state"] == "requested"
        assert control["replayed"] is False
        assert control["agent_id"] == agent["user"]["id"]
        assert control["requested_by"] == 1
        assert control["expires_in_seconds"] > 0

        inbox = client.get("/run-controls?state=open", headers=bearer).json()
        assert [entry["id"] for entry in inbox] == [control["id"]]
        assert inbox[0]["payload"] == "Focus on the flaky test"

        acked = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=bearer
        )
        assert acked.status_code == 200
        assert acked.json()["state"] == "acknowledged"
        assert acked.json()["acknowledged_at"] is not None
        assert acked.json()["settlement"] is None

        # Re-acknowledging is an idempotent no-op: same state, same timestamp,
        # and no second receipt event — a repeated receipt is not a lifecycle
        # moment (the request_kill precedent).
        re_acked = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=bearer
        )
        assert re_acked.status_code == 200
        assert re_acked.json()["acknowledged_at"] == acked.json()["acknowledged_at"]
        assert len(_events(db_file, run_control_commands.VERB_ACKNOWLEDGED)) == 1

        done = client.post(
            f"/run-controls/{control['id']}/complete",
            json={"summary": "Narrowed the failure to test_foo and fixed it."},
            headers=bearer,
        )
        assert done.status_code == 200
        settled = done.json()
        assert settled["state"] == "completed"
        assert settled["settlement"] == "completed"
        assert settled["settled_by"] == agent["user"]["id"]
        assert settled["result_summary"].startswith("Narrowed")

        requested = _events(db_file, run_control_commands.VERB_REQUESTED)
        acknowledged = _events(db_file, run_control_commands.VERB_ACKNOWLEDGED)
        completed = _events(db_file, run_control_commands.VERB_COMPLETED)
        assert [event["actor_id"] for event in requested] == [1]
        assert [event["actor_id"] for event in acknowledged] == [agent["user"]["id"]]
        assert [event["actor_id"] for event in completed] == [agent["user"]["id"]]
        for event in (*requested, *acknowledged, *completed):
            assert event["target_kind"] == "run_control"
            assert event["target_id"] == control["id"]
        assert settled["requested_event_id"] == requested[0]["id"]
        assert settled["acknowledged_event_id"] == acknowledged[0]["id"]
        assert settled["settled_event_id"] == completed[0]["id"]


def test_request_cancel_can_be_declined_with_a_reason(tmp_path):
    """Declining is an answer, not an error: the agent's reason is stored and
    shown to the operator, and the decline event is the agent's own claim."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-2")

        control = _create(
            client, "goal-2", kind="request_cancel", payload="deadline moved"
        ).json()
        declined = client.post(
            f"/run-controls/{control['id']}/decline",
            json={"reason": "Mid-migration: stopping now would strand the schema."},
            headers=bearer,
        )
        assert declined.status_code == 200
        assert declined.json()["state"] == "declined"
        assert declined.json()["result_summary"].startswith("Mid-migration")
        assert declined.json()["handoff"] is None
        events = _events(db_file, run_control_commands.VERB_DECLINED)
        assert [event["target_id"] for event in events] == [control["id"]]


def test_fresh_context_completes_with_the_bounded_handoff_only(tmp_path):
    """request_fresh_context stores exactly the structured handoff — the closed
    envelope is the whole point: transcripts and hidden reasoning have nowhere
    to go. The other kinds refuse a handoff, and fresh-context refuses a bare
    summary, so the two completion shapes can never blur."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-3")

        control = _create(
            client, "goal-3", kind="request_fresh_context", payload=None
        ).json()
        missing = client.post(
            f"/run-controls/{control['id']}/complete",
            json={"summary": "done"},
            headers=bearer,
        )
        assert missing.status_code == 422
        both = client.post(
            f"/run-controls/{control['id']}/complete",
            json={"summary": "done", "handoff": HANDOFF},
            headers=bearer,
        )
        assert both.status_code == 422
        assert "carries its summary inside the handoff" in both.json()["detail"]
        completed = client.post(
            f"/run-controls/{control['id']}/complete",
            json={"handoff": HANDOFF},
            headers=bearer,
        )
        assert completed.status_code == 200
        stored = completed.json()["handoff"]
        assert stored == HANDOFF
        assert completed.json()["result_summary"] == HANDOFF["summary"]

        steer = _create(client, "goal-3", kind="steer", payload="guidance").json()
        wrong_shape = client.post(
            f"/run-controls/{steer['id']}/complete",
            json={"handoff": HANDOFF},
            headers=bearer,
        )
        assert wrong_shape.status_code == 422
        assert "only request_fresh_context" in wrong_shape.json()["detail"]


def test_a_heartbeat_only_run_is_steerable_through_its_sole_checkin(tmp_path):
    """A run that has only checked in cooperatively (no tagged events) still has
    one honest owner — the sole reporting agent — so the operator can reach it.
    RUNS.md: check-ins are a sidecar, but they are identity-bearing."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        beat = client.put(
            "/agent-runs/heartbeat", json={"run_id": "quiet-run"}, headers=bearer
        )
        assert beat.status_code == 200

        control = _create(client, "quiet-run", payload="check in more often")
        assert control.status_code == 201
        assert control.json()["agent_id"] == agent["user"]["id"]
        settled = client.post(
            f"/run-controls/{control.json()['id']}/complete",
            json={"summary": "increased cadence"},
            headers=bearer,
        )
        assert settled.status_code == 200


# ---------------------------------------------------------------------------
# Authority — who may create, who may settle.
# ---------------------------------------------------------------------------


def test_a_worker_targeted_control_carries_the_targeting_and_settles(tmp_path):
    """Naming a worker narrows INTENT, never authority: the control records
    which process the operator meant (worker_id + worker_key in the
    projection), and any live credential of the bound agent still settles it."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "targeted-run")
        client.put(
            "/workers/heartbeat",
            json={"worker_key": "proc-1", "node_label": "box-a"},
            headers=bearer,
        )
        worker = client.get("/workers", headers=H1).json()[0]

        control = _create(client, "targeted-run", worker_id=worker["id"])
        assert control.status_code == 201
        assert control.json()["worker_id"] == worker["id"]
        assert control.json()["worker_key"] == "proc-1"

        inbox = client.get("/run-controls?state=open", headers=bearer).json()
        assert inbox[0]["worker_key"] == "proc-1"
        settled = client.post(
            f"/run-controls/{control.json()['id']}/complete",
            json={"summary": "proc-1 took the guidance"},
            headers=bearer,
        )
        assert settled.status_code == 200
        assert settled.json()["worker_id"] == worker["id"]


def test_a_create_tagged_with_the_target_run_id_cannot_steal_the_binding(tmp_path):
    """The admission's own audit event stamps the OPERATOR's ambient run
    context. If that context names the target run and the run is only owned via
    a check-in, the event would bind the run to the ADMIN — an unsettleable
    control and a locked-out agent. The command refuses and unwinds whole:
    no control, no event, no binding, and the agent keeps its run id."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        client.put(
            "/agent-runs/heartbeat", json={"run_id": "quiet-run"}, headers=bearer
        )

        poisoned = client.post(
            "/run-controls",
            json={"run_id": "quiet-run", "kind": "steer", "payload": "guidance"},
            headers={**H1, "X-Athena-Run": "quiet-run"},
        )
        assert poisoned.status_code == 422
        assert "retry without X-Athena-Run" in poisoned.json()["detail"]

        conn = db.connect(db_file)
        try:
            assert (
                conn.execute("SELECT COUNT(*) AS n FROM run_controls").fetchone()["n"]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM run_bindings WHERE run_id = 'quiet-run'"
                ).fetchone()["n"]
                == 0
            )
        finally:
            conn.close()

        # The whole admission unwound, so the agent can still claim its own run
        # id, and an untagged retry of the same request succeeds.
        _bind_run(client, bearer, "quiet-run")
        assert _create(client, "quiet-run").status_code == 201


def test_only_an_admin_principal_may_create(tmp_path):
    """Creation is the worker-kill authority: admin role, and for bearer tokens
    the admin scope too. Agents cannot steer themselves or each other, members
    cannot steer anyone, and an admin-role token without the admin scope is
    refused — scopes bind tokens even for admins."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-4")

        assert _create(client, "goal-4").status_code == 201

        by_agent = client.post(
            "/run-controls",
            json={"run_id": "goal-4", "kind": "steer", "payload": "x"},
            headers=bearer,
        )
        assert by_agent.status_code == 403

        client.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "password": "pw"},
            headers=H1,
        )
        by_member = client.post(
            "/run-controls",
            json={"run_id": "goal-4", "kind": "steer", "payload": "x"},
            headers={"X-Athena-Actor": "3"},
        )
        assert by_member.status_code == 403

        anonymous = client.post(
            "/run-controls", json={"run_id": "goal-4", "kind": "steer", "payload": "x"}
        )
        assert anonymous.status_code == 401

        narrow = client.post(
            "/tokens", json={"name": "narrow", "scopes": ["read"]}, headers=H1
        ).json()
        by_narrow_admin_token = client.post(
            "/run-controls",
            json={"run_id": "goal-4", "kind": "steer", "payload": "x"},
            headers={"Authorization": f"Bearer {narrow['token']}"},
        )
        assert by_narrow_admin_token.status_code == 403
        assert "admin" in by_narrow_admin_token.json()["detail"]


def test_only_the_bound_agent_may_settle(tmp_path):
    """Settlement is identity-bound and private. Another agent gets the same 404
    a missing control gives (no existence oracle), an admin session or trusted
    header cannot impersonate the agent (bearer required), and a non-agent
    bearer is refused."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        other = _agent(client, email="ray@e.com", name="Ray")
        other_bearer = _bearer(other)
        _bind_run(client, bearer, "goal-5")
        control = _create(client, "goal-5").json()

        stolen = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=other_bearer
        )
        assert stolen.status_code == 404
        missing = client.post("/run-controls/999/acknowledge", headers=other_bearer)
        assert missing.status_code == 404
        assert stolen.json()["detail"] == missing.json()["detail"]

        as_admin_header = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=H1
        )
        assert as_admin_header.status_code == 403
        assert as_admin_header.json()["detail"] == "bearer token required"

        # The other agent also cannot see it in any read surface.
        assert (
            client.get(
                f"/run-controls/{control['id']}", headers=other_bearer
            ).status_code
            == 404
        )
        assert client.get("/run-controls", headers=other_bearer).json() == []


def test_wrong_run_wrong_worker_and_reserved_namespaces_are_refused(tmp_path):
    """Admission fails closed on every identity question it cannot answer:
    unknown runs, runs owned by humans, ambiguous multi-agent check-ins,
    workers belonging to someone else, workers that reported stopped, and
    server-reserved run namespaces nobody polls."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        second = _agent(client, email="ray@e.com", name="Ray")
        second_bearer = _bearer(second)

        assert _create(client, "never-seen").status_code == 404

        # A human-owned run: the admin binds a run id with their own tagged write.
        client.post(
            "/issues",
            json={"title": "ops work"},
            headers={**H1, "X-Athena-Run": "human-run"},
        )
        human_owned = _create(client, "human-run")
        assert human_owned.status_code == 422
        assert "not owned by an agent" in human_owned.json()["detail"]

        # Two agents checked into one unbound run id: ambiguity is refused,
        # never resolved by guesswork.
        client.put(
            "/agent-runs/heartbeat", json={"run_id": "shared-id"}, headers=bearer
        )
        client.put(
            "/agent-runs/heartbeat", json={"run_id": "shared-id"}, headers=second_bearer
        )
        ambiguous = _create(client, "shared-id")
        assert ambiguous.status_code == 409
        assert "ambiguous" in ambiguous.json()["detail"]

        assert _create(client, "automation:rule-1:event-2").status_code == 422
        assert _create(client, "icarus:whatever").status_code == 422

        # Worker targeting: someone else's worker is invalid, a stopped worker
        # is a conflict — a control addressed to a process that said it stopped
        # would be a request Athena knows cannot be received.
        _bind_run(client, bearer, "goal-6")
        client.put(
            "/workers/heartbeat", json={"worker_key": "w-2"}, headers=second_bearer
        )
        other_worker = client.get("/workers", headers=H1).json()[0]
        wrong = _create(client, "goal-6", worker_id=other_worker["id"])
        assert wrong.status_code == 422
        client.put("/workers/heartbeat", json={"worker_key": "w-1"}, headers=bearer)
        mine = [
            worker
            for worker in client.get("/workers", headers=H1).json()
            if worker["worker_key"] == "w-1"
        ][0]
        client.put(
            "/workers/heartbeat",
            json={"worker_key": "w-1", "state": "stopped"},
            headers=bearer,
        )
        stopped = _create(client, "goal-6", worker_id=mine["id"])
        assert stopped.status_code == 409
        assert "stopped" in stopped.json()["detail"]


def test_revoked_and_paused_principals_fail_closed(tmp_path):
    """The kill switch and the pause lever dominate controls. A revoked token
    cannot settle even with a stale actor dict (in-transaction recheck); a
    paused agent is refused at the door; and creating against a paused agent's
    run is refused rather than recording a request nobody can receive."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        agent_id = agent["user"]["id"]
        _bind_run(client, bearer, "goal-7")
        control = _create(client, "goal-7").json()

        # Resolve the live actor BEFORE revocation, then revoke: the stale dict
        # must not be able to settle below the transport.
        stale_actor = _actor(db_file, agent["token"]["token"])
        assert client.delete(f"/users/{agent_id}/tokens", headers=H1).status_code == 200
        conn = db.connect(db_file)
        try:
            with pytest.raises(run_control_commands.RunControlCommandError) as err:
                run_control_commands.acknowledge_control(
                    conn, actor=stale_actor, control_id=control["id"]
                )
            assert err.value.kind == "forbidden"
            assert err.value.detail == "live bearer token required"
        finally:
            conn.close()
        over_http = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=bearer
        )
        assert over_http.status_code == 401  # revoked bearer dies at identity

        # Pause: a fresh credential still cannot act, and new controls against
        # the paused agent's runs are refused at admission.
        mint = db.connect(db_file)
        try:
            renewed = tokens.create_token(
                mint, user_id=agent_id, name="renewed", scopes=["issue:write"]
            )
        finally:
            mint.close()
        fresh_bearer = {"Authorization": f"Bearer {renewed['token']}"}
        assert (
            client.put(
                f"/users/{agent_id}/paused", json={"paused": True}, headers=H1
            ).status_code
            == 200
        )
        paused_settle = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=fresh_bearer
        )
        assert paused_settle.status_code == 403
        assert paused_settle.json()["detail"] == "account is paused"
        paused_create = _create(client, "goal-7", kind="request_cancel", payload=None)
        assert paused_create.status_code == 409
        assert "paused" in paused_create.json()["detail"]

        # The refused attempts changed nothing: the control still reads
        # requested, and no settlement events exist.
        assert (
            client.get(f"/run-controls/{control['id']}", headers=H1).json()["state"]
            == "requested"
        )
        assert _events(db_file, run_control_commands.VERB_ACKNOWLEDGED) == []


def test_ownership_drift_blocks_settlement_for_both_stories(tmp_path):
    """A control admitted on the check-in fallback names agent A. If the run id
    is later BOUND by agent B's first tagged write, Athena now holds two
    conflicting ownership stories — so it refuses to let either settle: A hits
    the drift conflict, B hits the same 404 any wrong agent gets."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent_a = _agent(client)
        bearer_a = _bearer(agent_a)
        agent_b = _agent(client, email="ray@e.com", name="Ray")
        bearer_b = _bearer(agent_b)

        client.put(
            "/agent-runs/heartbeat", json={"run_id": "drift-run"}, headers=bearer_a
        )
        control = _create(client, "drift-run").json()
        assert control["agent_id"] == agent_a["user"]["id"]

        _bind_run(client, bearer_b, "drift-run")

        as_a = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=bearer_a
        )
        assert as_a.status_code == 409
        assert "ownership changed" in as_a.json()["detail"]
        as_b = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=bearer_b
        )
        assert as_b.status_code == 404


# ---------------------------------------------------------------------------
# Idempotency — retries replay, conflicts refuse, the index backstops races.
# ---------------------------------------------------------------------------


def test_idempotent_retry_replays_and_conflicting_reuse_refuses(tmp_path):
    """One key, one control: an identical retry returns the recorded control
    (flagged replayed), a different request under the same key is a 409, and
    the UNIQUE index makes the guarantee hold even against raw SQL."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        bearer = _bearer(_agent(client))
        _bind_run(client, bearer, "goal-8")

        first = _create(client, "goal-8", idempotency_key="steer-goal8-1")
        retry = _create(client, "goal-8", idempotency_key="steer-goal8-1")
        assert first.status_code == 201 and retry.status_code == 201
        assert retry.json()["id"] == first.json()["id"]
        assert first.json()["replayed"] is False
        assert retry.json()["replayed"] is True

        conflicting = _create(
            client, "goal-8", payload="DIFFERENT", idempotency_key="steer-goal8-1"
        )
        assert conflicting.status_code == 409
        assert "already used" in conflicting.json()["detail"]

        # The requested LIFETIME is part of the request's identity too: a retry
        # asking for a different TTL must refuse, never silently return a
        # control that expires at the original deadline.
        different_ttl = _create(
            client, "goal-8", ttl_seconds=7200, idempotency_key="steer-goal8-1"
        )
        assert different_ttl.status_code == 409

        listed = client.get("/run-controls?run_id=goal-8", headers=H1).json()
        assert len(listed) == 1

        conn = db.connect(db_file)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO run_controls (run_id, agent_id, kind, payload, "
                    "requested_by, idempotency_key, expires_at) "
                    "VALUES ('goal-8', 2, 'steer', 'x', 1, 'steer-goal8-1', "
                    "datetime('now', '+60 seconds'))"
                )
        finally:
            conn.close()


def test_transport_idempotency_key_header_replays_the_response(tmp_path):
    """The /run-controls root is opted into the durable Idempotency-Key
    middleware, so transport retries replay the stored 201 byte-for-byte and a
    mutated retry under the same header is the standard 409 mismatch."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        bearer = _bearer(_agent(client))
        _bind_run(client, bearer, "goal-9")
        headers = {**H1, "Idempotency-Key": "hdr-1"}
        body = {"run_id": "goal-9", "kind": "steer", "payload": "guidance"}
        first = client.post("/run-controls", json=body, headers=headers)
        replay = client.post("/run-controls", json=body, headers=headers)
        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.headers.get("idempotent-replay") == "true"
        assert replay.json() == first.json()
        mutated = client.post(
            "/run-controls", json={**body, "payload": "other"}, headers=headers
        )
        assert mutated.status_code == 409
        assert mutated.json()["code"] == "idempotency_mismatch"


def test_at_most_one_live_control_per_run_and_kind(tmp_path):
    """Duplicate live requests are refused so the agent's inbox cannot silt up
    with restatements — but a settled control frees the slot, and different
    kinds never contend."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        bearer = _bearer(_agent(client))
        _bind_run(client, bearer, "goal-10")

        first = _create(client, "goal-10").json()
        duplicate = _create(client, "goal-10", payload="restated")
        assert duplicate.status_code == 409
        assert "already exists" in duplicate.json()["detail"]
        assert (
            _create(client, "goal-10", kind="request_cancel", payload=None).status_code
            == 201
        )

        client.post(
            f"/run-controls/{first['id']}/decline",
            json={"reason": "busy"},
            headers=bearer,
        )
        assert _create(client, "goal-10", payload="second round").status_code == 201


# ---------------------------------------------------------------------------
# Races — concurrent settlement and creation, Barrier-coordinated.
# ---------------------------------------------------------------------------


def test_concurrent_settlements_produce_exactly_one_outcome(tmp_path):
    """Complete and decline race on one control: the CAS predicate lets exactly
    one land; the loser gets conflict, and exactly one settlement event exists.
    A control can never end two ways."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "race-run")
        control = _create(client, "race-run").json()
        actor = _actor(db_file, agent["token"]["token"])

    barrier = threading.Barrier(2)

    def settle(way):
        conn = db.connect(db_file)
        try:
            barrier.wait(timeout=5)
            try:
                if way == "complete":
                    run_control_commands.complete_control(
                        conn, actor=actor, control_id=control["id"], summary="done"
                    )
                else:
                    run_control_commands.decline_control(
                        conn, actor=actor, control_id=control["id"], reason="no"
                    )
                return "settled"
            except run_control_commands.RunControlCommandError as exc:
                return exc.kind
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            future.result(timeout=10)
            for future in (
                pool.submit(settle, "complete"),
                pool.submit(settle, "decline"),
            )
        )
    assert outcomes == ["conflict", "settled"]

    conn = db.connect(db_file)
    try:
        row = conn.execute(
            "SELECT settlement, settled_event_id FROM run_controls WHERE id = ?",
            (control["id"],),
        ).fetchone()
        assert row["settlement"] in ("completed", "declined")
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM activity WHERE verb IN (?, ?)",
            (
                run_control_commands.VERB_COMPLETED,
                run_control_commands.VERB_DECLINED,
            ),
        ).fetchone()
        assert events["n"] == 1
    finally:
        conn.close()


def test_concurrent_creates_with_one_key_record_one_control(tmp_path):
    """Two identical creates race under one idempotency key: one records, one
    replays (or loses the UNIQUE race and refuses), and exactly one row and one
    requested event exist afterwards."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        bearer = _bearer(_agent(client))
        _bind_run(client, bearer, "create-race")
        admin_actor = {"id": 1, "role": "admin", "is_agent": 0}

    barrier = threading.Barrier(2)

    def create():
        conn = db.connect(db_file)
        try:
            barrier.wait(timeout=5)
            try:
                result = run_control_commands.create_control(
                    conn,
                    actor=admin_actor,
                    run_id="create-race",
                    kind="steer",
                    payload="same ask",
                    idempotency_key="race-key",
                )
                return "replayed" if result["replayed"] else "created"
            except run_control_commands.RunControlCommandError as exc:
                return exc.kind
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            future.result(timeout=10)
            for future in (pool.submit(create), pool.submit(create))
        )
    assert outcomes[0] == "created"
    assert outcomes[1] in ("replayed", "conflict")

    conn = db.connect(db_file)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM run_controls").fetchone()
        assert count["n"] == 1
    finally:
        conn.close()
    assert len(_events(db_file, run_control_commands.VERB_REQUESTED)) == 1


# ---------------------------------------------------------------------------
# Expiry — the clock's verdict, derived at read time, refusing late answers.
# ---------------------------------------------------------------------------


def test_expiry_is_derived_and_late_answers_are_refused(tmp_path):
    """Time is injected (never slept on): past `expires_at` a control reads as
    expired without any write, late acknowledgement and settlement refuse, and
    NO event is ever recorded for expiry — it is nobody's action."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-11")
        # A one-hour TTL keeps the "before" reads deterministic no matter how
        # slowly a loaded xdist worker gets here; expiry is exercised purely
        # with the injected clock below, never by waiting.
        control = _create(client, "goal-11", ttl_seconds=3600).json()
        actor = _actor(db_file, agent["token"]["token"])

    later = datetime.now(UTC) + timedelta(seconds=7200)
    conn = db.connect(db_file)
    try:
        admin = {"id": 1, "role": "admin", "is_agent": 0}
        before = run_control_commands.visible_control(
            conn, actor=admin, control_id=control["id"]
        )
        assert before["state"] == "requested" and before["expired"] is False
        after = run_control_commands.visible_control(
            conn, actor=admin, control_id=control["id"], now=later
        )
        assert after["state"] == "expired"
        assert after["expired"] is True
        assert after["expires_in_seconds"] == 0

        for late_call in (
            lambda: run_control_commands.acknowledge_control(
                conn, actor=actor, control_id=control["id"], now=later
            ),
            lambda: run_control_commands.complete_control(
                conn, actor=actor, control_id=control["id"], summary="s", now=later
            ),
            lambda: run_control_commands.decline_control(
                conn, actor=actor, control_id=control["id"], reason="r", now=later
            ),
        ):
            with pytest.raises(run_control_commands.RunControlCommandError) as err:
                late_call()
            assert err.value.kind == "conflict"
            assert "expired" in err.value.detail

        # The stored row is untouched: expiry wrote nothing, and the whole
        # activity trail holds only the one requested event.
        row = conn.execute(
            "SELECT acknowledged_at, settled_at FROM run_controls WHERE id = ?",
            (control["id"],),
        ).fetchone()
        assert row["acknowledged_at"] is None and row["settled_at"] is None
        verbs = conn.execute(
            "SELECT verb, COUNT(*) AS n FROM activity "
            "WHERE target_kind = 'run_control' GROUP BY verb"
        ).fetchall()
        assert {row["verb"]: row["n"] for row in verbs} == {
            run_control_commands.VERB_REQUESTED: 1
        }

        # An expired-but-unsettled control does not block a fresh request of the
        # same kind: liveness includes the clock.
        replacement = run_control_commands.create_control(
            conn,
            actor=admin,
            run_id="goal-11",
            kind="steer",
            payload="second attempt",
            now=later,
        )
        assert replacement["state"] == "requested"
    finally:
        conn.close()


def test_expiration_race_inside_the_cas_predicate(tmp_path):
    """The settlement CAS re-checks expiry under the writer lock: a settlement
    evaluated at or after the exact `expires_at` instant is a 0-row no-op,
    while one instant earlier still lands. Both instants derive from the SAME
    injected creation clock, so the boundary is exercised deterministically."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-12")

    conn = db.connect(db_file)
    try:
        admin = {"id": 1, "role": "admin", "is_agent": 0}
        creation = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
        control = run_control_commands.create_control(
            conn,
            actor=admin,
            run_id="goal-12",
            kind="steer",
            payload="boundary",
            ttl_seconds=60,
            now=creation,
        )
        at_expiry = run_controls.cas_settle(
            conn,
            control_id=control["id"],
            settled_by=2,
            settlement="completed",
            result_summary="late",
            result_payload=None,
            now_stamp=run_controls.stamp(creation + timedelta(seconds=60)),
        )
        assert at_expiry is False
        conn.rollback()
        just_before = run_controls.cas_settle(
            conn,
            control_id=control["id"],
            settled_by=2,
            settlement="completed",
            result_summary="in time",
            result_payload=None,
            now_stamp=run_controls.stamp(creation + timedelta(seconds=59)),
        )
        assert just_before is True
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bounds — payloads, results, handoffs, TTLs.
# ---------------------------------------------------------------------------


def test_payload_and_result_bounds_are_enforced(tmp_path):
    """Every human-text field is bounded and control-character-free, steering
    without guidance is refused, and TTLs stay inside the documented window —
    the limits are the contract, not suggestions."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-13")

        too_long = _create(client, "goal-13", payload="x" * 4001)
        assert too_long.status_code == 422
        empty_steer = _create(client, "goal-13", payload=None)
        assert empty_steer.status_code == 422
        assert "payload is required" in empty_steer.json()["detail"]
        control_chars = _create(client, "goal-13", payload="a\x00b")
        assert control_chars.status_code == 422
        assert _create(client, "goal-13", ttl_seconds=59).status_code == 422
        assert _create(client, "goal-13", ttl_seconds=86_401).status_code == 422

        control = _create(client, "goal-13", payload="ok\nmultiline\tguidance").json()
        assert control["payload"] == "ok\nmultiline\tguidance"

        long_reason = client.post(
            f"/run-controls/{control['id']}/decline",
            json={"reason": "x" * 2001},
            headers=bearer,
        )
        assert long_reason.status_code == 422
        empty_reason = client.post(
            f"/run-controls/{control['id']}/decline",
            json={"reason": "   "},
            headers=bearer,
        )
        assert empty_reason.status_code == 422
        long_summary = client.post(
            f"/run-controls/{control['id']}/complete",
            json={"summary": "x" * 2001},
            headers=bearer,
        )
        assert long_summary.status_code == 422

        # An id beyond SQLite's integer range is a client error at the schema,
        # never a driver OverflowError surfacing as a 500.
        beyond = str(1 << 63)
        assert client.get(f"/run-controls/{beyond}", headers=H1).status_code == 422
        assert (
            client.post(
                f"/run-controls/{beyond}/acknowledge", headers=bearer
            ).status_code
            == 422
        )


def test_handoff_bounds_and_closed_envelope(tmp_path):
    """The fresh-context handoff refuses unknown fields, over-count lists,
    oversized items, and oversized encodings — nothing unbounded can ride
    along, by construction."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-14")

        def fresh_control():
            listing = client.get(
                "/run-controls?run_id=goal-14&state=open", headers=H1
            ).json()
            if listing:
                return listing[0]["id"]
            return _create(
                client, "goal-14", kind="request_fresh_context", payload=None
            ).json()["id"]

        def refuse(handoff, fragment):
            response = client.post(
                f"/run-controls/{fresh_control()}/complete",
                json={"handoff": handoff},
                headers=bearer,
            )
            assert response.status_code == 422, response.text
            assert fragment in response.json()["detail"]

        refuse({"summary": "s", "transcript": "full log"}, "handoff fields are")
        refuse({"summary": ""}, "handoff summary is required")
        refuse({"summary": "s", "unresolved_questions": ["q"] * 11}, "at most 10 items")
        refuse({"summary": "s", "athena_refs": ["x" * 201]}, "at most 200 characters")
        refuse(
            {"summary": "s", "unresolved_questions": "not-a-list"},
            "must be a list of strings",
        )
        refuse(
            {
                "summary": "s",
                "evidence_refs": [("x" * 500)] * 10,
                "athena_refs": ["y" * 200] * 20,
                "unresolved_questions": ["z" * 500] * 10,
            },
            "encoded handoff",
        )


# ---------------------------------------------------------------------------
# Visibility, filters, and pagination.
# ---------------------------------------------------------------------------


def test_visibility_isolation_and_state_filters(tmp_path):
    """Admins see the fleet; each agent sees exactly its own controls; everyone
    else sees empty lists and 404s. State filters are SQL predicates evaluated
    before LIMIT, so a filtered page is a real page."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent_a = _agent(client)
        bearer_a = _bearer(agent_a)
        agent_b = _agent(client, email="ray@e.com", name="Ray")
        bearer_b = _bearer(agent_b)
        _bind_run(client, bearer_a, "vis-a")
        _bind_run(client, bearer_b, "vis-b")

        control_a = _create(client, "vis-a").json()
        control_b = _create(client, "vis-b").json()
        client.post(
            f"/run-controls/{control_b['id']}/complete",
            json={"summary": "done"},
            headers=bearer_b,
        )

        assert {c["id"] for c in client.get("/run-controls", headers=H1).json()} == {
            control_a["id"],
            control_b["id"],
        }
        assert [
            c["id"] for c in client.get("/run-controls", headers=bearer_a).json()
        ] == [control_a["id"]]
        assert [
            c["id"] for c in client.get("/run-controls", headers=bearer_b).json()
        ] == [control_b["id"]]
        assert [
            c["id"]
            for c in client.get("/run-controls?state=completed", headers=H1).json()
        ] == [control_b["id"]]
        assert [
            c["id"]
            for c in client.get("/run-controls?state=requested", headers=H1).json()
        ] == [control_a["id"]]
        assert client.get("/run-controls?state=nonsense", headers=H1).status_code == 422

        # Newest-first and clamped: limit=1 returns only the newest control.
        assert [
            c["id"] for c in client.get("/run-controls?limit=1", headers=H1).json()
        ] == [control_b["id"]]

        client.post(
            "/users",
            json={"email": "v@e.com", "name": "View", "password": "pw"},
            headers=H1,
        )
        outsider = {"X-Athena-Actor": "4"}
        assert client.get("/run-controls", headers=outsider).json() == []
        assert (
            client.get(f"/run-controls/{control_a['id']}", headers=outsider).status_code
            == 404
        )

    # The expired filter needs a clock, so it is exercised below the transport
    # with an injected now — deterministically, without sleeping.
    conn = db.connect(db_file)
    try:
        later = datetime.now(UTC) + timedelta(days=2)
        admin = {"id": 1, "role": "admin", "is_agent": 0}
        expired = run_control_commands.readable_controls(
            conn, actor=admin, state="expired", now=later
        )
        assert [c["id"] for c in expired] == [control_a["id"]]
        still_completed = run_control_commands.readable_controls(
            conn, actor=admin, state="completed", now=later
        )
        assert [c["id"] for c in still_completed] == [control_b["id"]]
        assert (
            run_control_commands.readable_controls(
                conn, actor=admin, state="open", now=later
            )
            == []
        )
    finally:
        conn.close()


def test_control_events_flow_through_the_existing_feeds(tmp_path):
    """No second feed: control lifecycle events ride the same /events cursor as
    everything else, the operator's request never stamps the target run's
    replay, and an agent that tags its settlement joins its own run's trail."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "feed-run")
        control = _create(client, "feed-run").json()
        client.post(
            f"/run-controls/{control['id']}/complete",
            json={"summary": "followed the guidance"},
            headers={**bearer, "X-Athena-Run": "feed-run"},
        )

        # Drain the cursor feed and find both control events in order.
        events, after = [], None
        while True:
            params = {"limit": 50}
            if after is not None:
                params["after"] = after
            page = client.get("/events", params=params, headers=H1).json()
            events.extend(page["events"])
            if not page["has_more"]:
                break
            after = page["next_after"]
        control_events = [
            event for event in events if event["target_kind"] == "run_control"
        ]
        assert [event["verb"] for event in control_events] == [
            run_control_commands.VERB_REQUESTED,
            run_control_commands.VERB_COMPLETED,
        ]
        # The operator's request is untagged (their own ambient context, not the
        # target run's); the agent's tagged settlement joined its own run.
        assert control_events[0]["run_id"] is None
        assert control_events[1]["run_id"] == "feed-run"

        run_events = client.get(
            "/events", params={"run_id": "feed-run"}, headers=H1
        ).json()["events"]
        assert run_control_commands.VERB_COMPLETED in [
            event["verb"] for event in run_events
        ]


# ---------------------------------------------------------------------------
# The database refuses what the commands never do.
# ---------------------------------------------------------------------------


def test_database_triggers_refuse_tampering(tmp_path):
    """The schema is the last line: requests are immutable, claims write-once,
    settled rows frozen, controls undeletable, and trail pointers must name the
    native event that actually records the fact."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-15")
        open_control = _create(client, "goal-15").json()
        settled_control = _create(
            client, "goal-15", kind="request_cancel", payload=None
        ).json()
        client.post(
            f"/run-controls/{settled_control['id']}/complete",
            json={"summary": "wound down"},
            headers=bearer,
        )

    conn = db.connect(db_file)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="request is immutable"):
            conn.execute(
                "UPDATE run_controls SET payload = 'edited' WHERE id = ?",
                (open_control["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="settled run control"):
            conn.execute(
                "UPDATE run_controls SET result_summary = 'rewritten' WHERE id = ?",
                (settled_control["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="durable"):
            conn.execute("DELETE FROM run_controls WHERE id = ?", (open_control["id"],))
        # Rewriting a trail pointer trips whichever guard fires first — the
        # write-once rule or the native-event binding; both refuse the write.
        with pytest.raises(
            sqlite3.IntegrityError, match="write-once|request event required"
        ):
            conn.execute(
                "UPDATE run_controls SET requested_event_id = 1 WHERE id = ?",
                (open_control["id"],),
            )
        # A forged trail pointer: born unanswered, then pointed at an event that
        # does not record this control's request.
        conn.execute(
            "INSERT INTO run_controls (run_id, agent_id, kind, payload, "
            "requested_by, idempotency_key, expires_at) VALUES "
            "('goal-15', 2, 'steer', 'x', 1, 'forge-key', "
            "datetime('now', '+60 seconds'))"
        )
        forged = conn.execute("SELECT max(id) AS id FROM run_controls").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="request event required"):
            conn.execute(
                "UPDATE run_controls SET requested_event_id = "
                "(SELECT max(id) FROM activity) WHERE id = ?",
                (forged["id"],),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="start unanswered"):
            conn.execute(
                "INSERT INTO run_controls (run_id, agent_id, kind, payload, "
                "requested_by, idempotency_key, expires_at, settled_at, "
                "settled_by, settlement, result_summary) VALUES "
                "('goal-15', 2, 'steer', 'x', 1, 'born-settled', "
                "datetime('now', '+60 seconds'), datetime('now'), 2, "
                "'completed', 'faked')"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Commands hold below the transport (a fabricated adapter cannot bypass them).
# ---------------------------------------------------------------------------


def test_command_guards_below_the_transport(tmp_path):
    """The command re-checks everything the routes checked: no actor, no bearer,
    non-agent bearer, and malformed inputs are refused by kind even when no
    HTTP layer screened them first."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-16")
        control = _create(client, "goal-16").json()

    conn = db.connect(db_file)
    try:

        def kind_of(callable_):
            with pytest.raises(run_control_commands.RunControlCommandError) as err:
                callable_()
            return err.value.kind

        assert (
            kind_of(
                lambda: run_control_commands.create_control(
                    conn, actor=None, run_id="goal-16", kind="steer", payload="x"
                )
            )
            == "unauthorized"
        )
        assert (
            kind_of(
                lambda: run_control_commands.acknowledge_control(
                    conn, actor=None, control_id=control["id"]
                )
            )
            == "unauthorized"
        )
        session_actor = {"id": 2, "role": "member", "is_agent": 1}
        assert (
            kind_of(
                lambda: run_control_commands.acknowledge_control(
                    conn, actor=session_actor, control_id=control["id"]
                )
            )
            == "forbidden"
        )
        admin_actor = {"id": 1, "role": "admin", "is_agent": 0}
        assert (
            kind_of(
                lambda: run_control_commands.create_control(
                    conn, actor=admin_actor, run_id="goal-16", kind="unknown_kind"
                )
            )
            == "invalid"
        )
        assert (
            kind_of(
                lambda: run_control_commands.create_control(
                    conn,
                    actor=admin_actor,
                    run_id="goal-16",
                    kind="steer",
                    payload="x",
                    idempotency_key="bad key with spaces",
                )
            )
            == "invalid"
        )
        assert (
            kind_of(
                lambda: run_control_commands.readable_controls(
                    conn, actor=admin_actor, state="bogus"
                )
            )
            == "invalid"
        )
        assert (
            kind_of(
                lambda: run_control_commands.create_control(
                    conn, actor=admin_actor, run_id="\x07", kind="steer", payload="x"
                )
            )
            == "invalid"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCP parity — same rules, same refusals, through the client the tools use.
# ---------------------------------------------------------------------------


def test_mcp_client_shares_the_rest_rules_and_errors(tmp_path):
    """The MCP tools are one-line delegations to AthenaClient, so exercising the
    client against the real app covers tool -> HTTP -> command. The refusal a
    tool surfaces (status + detail) must be the same one REST gives."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as admin_http:
        admin_http.__enter__
        _bootstrap(admin_http)
        agent = _agent(admin_http)
        admin_token = admin_http.post(
            "/tokens", json={"name": "op", "scopes": ["admin"]}, headers=H1
        ).json()["token"]
        admin_http.headers.update({"Authorization": f"Bearer {admin_token}"})
        operator = AthenaClient(client=admin_http)

        agent_http = TestClient(app)
        agent_http.headers.update(
            {"Authorization": f"Bearer {agent['token']['token']}"}
        )
        worker = AthenaClient(client=agent_http)

        worker.heartbeat_agent_run("mcp-run")
        control = operator.create_run_control(
            run_id="mcp-run", kind="steer", payload="prefer the small diff"
        )
        assert control["state"] == "requested"

        inbox = worker.list_run_controls(state="open")
        assert [entry["id"] for entry in inbox] == [control["id"]]
        acked = worker.acknowledge_run_control(control["id"])
        assert acked["state"] == "acknowledged"
        done = worker.complete_run_control(control["id"], summary="kept it small")
        assert done["settlement"] == "completed"

        with pytest.raises(AthenaError) as err:
            worker.complete_run_control(control["id"], summary="again")
        assert err.value.status_code == 409
        assert err.value.detail == "control is already settled"

        with pytest.raises(AthenaError) as err:
            worker.create_run_control(run_id="mcp-run", kind="steer", payload="x")
        assert err.value.status_code == 403

        with pytest.raises(AthenaError) as err:
            operator.acknowledge_run_control(control["id"])
        # The operator's admin token is not the bound agent's credential.
        assert err.value.status_code == 403

        assert operator.get_run_control(control["id"])["state"] == "completed"
        fresh = operator.create_run_control(
            run_id="mcp-run", kind="request_fresh_context"
        )
        handed = worker.complete_run_control(fresh["id"], handoff=HANDOFF)
        assert handed["handoff"] == HANDOFF
        agent_http.close()


# ---------------------------------------------------------------------------
# The web panel — truthful wording, CSRF, admin-only form, no oracle.
# ---------------------------------------------------------------------------


def _login(client, email, password="pw"):
    response = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303


def _panel_form(page_text):
    import re

    csrf = re.search(r'name="csrf_token" value="([^"]*)"', page_text)
    key = re.search(r'name="idempotency_key" value="([^"]*)"', page_text)
    assert csrf and key
    return csrf.group(1), key.group(1)


def test_run_page_panel_records_requests_with_truthful_wording(tmp_path):
    """The panel is the operator's window: it renders recorded requests and
    answers with claim-honest wording, dedupes a double-submitted form through
    the minted idempotency key, and never suggests Athena changed a process."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "web-run")
        _login(client, "a@e.com")

        page = client.get("/aegis/activity/runs/web-run/lineage")
        assert page.status_code == 200
        assert "Run controls" in page.text
        assert "acknowledged means it reported receipt" in page.text
        csrf, minted_key = _panel_form(page.text)

        form = {
            "csrf_token": csrf,
            "idempotency_key": minted_key,
            "kind": "steer",
            "payload": "watch the flaky suite",
        }
        first = client.post(
            "/aegis/activity/runs/web-run/controls",
            data=form,
            follow_redirects=False,
        )
        assert first.status_code == 303
        assert "nothing+has+been+changed+or+stopped" in first.headers["location"]
        # A browser resubmit replays the same key: still exactly one control.
        client.post(
            "/aegis/activity/runs/web-run/controls",
            data=form,
            follow_redirects=False,
        )
        listed = client.get("/run-controls?run_id=web-run", headers=H1).json()
        assert len(listed) == 1

        client.post(f"/run-controls/{listed[0]['id']}/acknowledge", headers=bearer)
        shown = client.get("/aegis/activity/runs/web-run/lineage").text
        assert "receipt only — no outcome yet" in shown

        client.post(
            f"/run-controls/{listed[0]['id']}/complete",
            json={"summary": "quarantined the flaky test"},
            headers=bearer,
        )
        settled_page = client.get("/aegis/activity/runs/web-run/lineage").text
        assert "quarantined the flaky test" in settled_page

        no_csrf = client.post(
            "/aegis/activity/runs/web-run/controls",
            data={"kind": "steer", "payload": "x", "idempotency_key": "k"},
            follow_redirects=False,
        )
        assert no_csrf.status_code == 403

        # A ttl the operator typed but Athena cannot read is refused with an
        # error, never silently replaced by the default lifetime (the API 422s
        # the same input — the two surfaces must agree).
        bad_ttl = client.post(
            "/aegis/activity/runs/web-run/controls",
            data={
                "csrf_token": csrf,
                "idempotency_key": "bad-ttl-key",
                "kind": "request_fresh_context",
                "payload": "",
                "ttl_seconds": "2h",
            },
            follow_redirects=False,
        )
        assert bad_ttl.status_code == 303
        assert "whole+number" in bad_ttl.headers["location"]
        assert len(client.get("/run-controls?run_id=web-run", headers=H1).json()) == 1


def test_run_page_hides_controls_from_non_admin_viewers(tmp_path):
    """A signed-in viewer who is neither an admin nor the bound agent sees no
    panel and no payload text — operator intent is not run-page gossip — and a
    non-admin POST is refused by the command, not the template."""
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "web-run-2")
        assert (
            _create(client, "web-run-2", payload="secret guidance").status_code == 201
        )
        client.post(
            "/users",
            json={"email": "v@e.com", "name": "View", "password": "pw"},
            headers=H1,
        )

    viewer = TestClient(app)
    with viewer:
        _login(viewer, "v@e.com")
        page = viewer.get("/aegis/activity/runs/web-run-2/lineage")
        assert page.status_code == 200
        assert "Run controls" not in page.text
        assert "secret guidance" not in page.text

        # The command, not the missing form, is the enforcement: a hand-built
        # POST from a non-admin session is refused and changes nothing. The
        # viewer's CSRF token comes from the record-learning form, which the
        # lineage page renders for every signed-in viewer of this run.
        import re

        csrf = re.search(r'name="csrf_token" value="([^"]*)"', page.text)
        assert csrf is not None, "expected a CSRF-bearing form for a signed-in viewer"
        refused = viewer.post(
            "/aegis/activity/runs/web-run-2/controls",
            data={
                "csrf_token": csrf.group(1),
                "idempotency_key": "hand-built",
                "kind": "steer",
                "payload": "sneaky",
            },
            follow_redirects=False,
        )
        assert refused.status_code == 303
        assert "error=" in refused.headers["location"]

    with TestClient(app) as check:
        listed = check.get("/run-controls?run_id=web-run-2", headers=H1).json()
        assert len(listed) == 1  # still only the admin's control


def test_schema_version_and_result_payload_round_trip(tmp_path):
    """The stored record carries schema_version 1 and canonical JSON — the
    shape a future migration or export can rely on."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        agent = _agent(client)
        bearer = _bearer(agent)
        _bind_run(client, bearer, "goal-17")
        control = _create(
            client, "goal-17", kind="request_fresh_context", payload=None
        ).json()
        assert control["schema_version"] == 1
        client.post(
            f"/run-controls/{control['id']}/complete",
            json={"handoff": HANDOFF},
            headers=bearer,
        )

    conn = db.connect(db_file)
    try:
        row = conn.execute(
            "SELECT result_payload FROM run_controls WHERE id = ?",
            (control["id"],),
        ).fetchone()
        stored = json.loads(row["result_payload"])
        assert stored["summary"] == HANDOFF["summary"]
        assert json.dumps(stored, sort_keys=True) == json.dumps(
            {
                "summary": HANDOFF["summary"],
                "unresolved_questions": HANDOFF["unresolved_questions"],
                "athena_refs": HANDOFF["athena_refs"],
                "evidence_refs": HANDOFF["evidence_refs"],
            },
            sort_keys=True,
        )
    finally:
        conn.close()


def test_settlement_requires_a_write_scope_not_just_the_right_identity(tmp_path):
    """RUN_CONTROLS.md: settling takes "an agent account and a live write
    scope". The identity check alone passing would let a read-only credential
    of the RIGHT agent answer a control — this pins the scope half of the
    documented rule (review finding: documented, previously untested)."""
    app, db_file = _app(tmp_path, "write_scope.db")
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        _bind_run(client, bearer, "goal-ws")
        control = _create(client, "goal-ws").json()

        mint = db.connect(db_file)
        try:
            readonly = tokens.create_token(
                mint, user_id=sol["user"]["id"], name="ro", scopes=["read"]
            )
        finally:
            mint.close()
        ro_bearer = {"Authorization": f"Bearer {readonly['token']}"}

        refused = client.post(
            f"/run-controls/{control['id']}/acknowledge", headers=ro_bearer
        )
        assert refused.status_code == 403
        assert "write scope" in refused.json()["detail"]

        # Reading its instructions stays allowed on any live credential of the
        # bound agent — only ANSWERING is write-scoped — and the refused
        # attempt changed nothing.
        assert (
            client.get(f"/run-controls/{control['id']}", headers=ro_bearer).status_code
            == 200
        )
        current = client.get(f"/run-controls/{control['id']}", headers=bearer).json()
        assert current["state"] == "requested"
        assert current["acknowledged_at"] is None
