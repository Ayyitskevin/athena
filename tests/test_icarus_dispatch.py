"""Handing work to an external executor, and hearing back.

Athena is the control plane; the executor is a separate system with its own store.
They share no database and neither imports the other. What Athena keeps is a record
of what it ASKED and what it was TOLD — and the tests below are mostly about
keeping those two things apart.

* `accepted` means "the executor said it accepted". It never means work is running,
  and no read here will imply otherwise.
* The outbound call happens AFTER the record commits, so the durable fact "Athena
  decided to dispatch this" survives a far side that never answers.
* A callback carries no Athena credential — it is authenticated by HMAC over the
  exact body — and can do exactly two things: attach evidence, and report an
  outcome.
* The policy digest is tamper-EVIDENT: a mismatch is recorded, not discarded,
  because destroying the evidence would defeat the point of computing it.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from athena import config
from athena.aegis import icarus_commands
from athena.core import db, dispatch, webhooks
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
SECRET = "icarus-test-secret"
# A literal public address, like the webhook tests use: the SSRF guard resolves the
# host, and a sandbox without DNS must not turn every dispatch into a refusal.
ICARUS_URL = "https://93.184.216.34"


@pytest.fixture(autouse=True)
def _configured_executor(monkeypatch):
    # Dispatch is unavailable unless BOTH are set; every test here wants it on.
    monkeypatch.setattr(config, "ICARUS_URL", ICARUS_URL)
    monkeypatch.setattr(config, "ICARUS_SECRET", SECRET)


def _app(tmp_path, name="dispatch.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", scopes=("read", "issue:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _issue(client, title="ship it", headers=H1):
    return client.post("/issues", json={"title": title}, headers=headers).json()


def _accepting_poster(run_id="icarus-run-1", record=None):
    """A stub executor that accepts. Mirrors how the webhook tests inject one."""

    def poster(url, body, headers):
        if record is not None:
            record.append((url, body, headers))
        return True, json.dumps({"icarus_run_id": run_id})

    return poster


def _callback(client, payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/callbacks/icarus",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Athena-Signature": signature,
        },
    )


def _dispatch_via_command(db_file, *, actor_id=1, issue_id, poster, **overrides):
    """Drive the command directly so the stub poster can be injected."""
    conn = db.connect(db_file)
    actor = dict(
        conn.execute("SELECT * FROM users WHERE id = ?", (actor_id,)).fetchone()
    )
    payload = {
        "repo": "git@example.com:acme/app.git",
        "base_commit": "abc123",
        "capability": "repo.edit",
        **overrides,
    }
    record = icarus_commands.request_dispatch(
        conn, actor=actor, work_item_id=issue_id, **payload
    )
    delivered = icarus_commands.deliver_dispatch(
        conn, dispatch_id=record["id"], poster=poster
    )
    conn.close()
    return delivered


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, target_id, detail, run_id FROM activity WHERE verb = ? "
        "ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_a_dispatch_records_what_athena_asked_and_what_it_was_told(tmp_path):
    app, db_file = _app(tmp_path)
    sent: list = []
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)

    record = _dispatch_via_command(
        db_file, issue_id=issue["id"], poster=_accepting_poster(record=sent)
    )
    assert record["state"] == dispatch.ACCEPTED
    assert record["icarus_run_id"] == "icarus-run-1"
    assert record["run_id"].startswith(dispatch.RUN_PREFIX)
    assert record["policy_digest"]

    # The envelope is exactly the adapter contract — no free-form payload, so a
    # reader can enumerate everything that crosses the boundary.
    url, body, headers = sent[0]
    assert url == f"{ICARUS_URL}/dispatch"
    envelope = json.loads(body)
    assert envelope["schema"] == "athena.icarus_dispatch.v1"
    assert set(envelope) == {
        "schema",
        "dispatch_id",
        "work_item_id",
        "run_id",
        "parent_run_id",
        "fork_run_ids",
        "icarus_run_id",
        "repo",
        "base_commit",
        "capability",
        "policy_digest",
        "approval_state",
        "idempotency_key",
        "evidence_ref",
        "completion_ref",
    }
    # Signed with the shared secret, and carrying the key that makes a retry
    # single-flight on both sides.
    assert headers["X-Athena-Signature"] == webhooks.sign(SECRET, body)
    assert headers["Idempotency-Key"] == record["idempotency_key"]

    assert len(_events(db_file, dispatch.VERB_REQUESTED)) == 1
    assert len(_events(db_file, dispatch.VERB_ACCEPTED)) == 1


def test_the_record_survives_an_executor_that_never_answers(tmp_path):
    # The durable fact is "Athena decided to dispatch this". A far side that is
    # down must not erase it — an operator needs to see that Athena tried.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)

    def refusing_poster(url, body, headers):
        return False, "connection refused"

    record = _dispatch_via_command(
        db_file, issue_id=issue["id"], poster=refusing_poster
    )
    assert record["state"] == dispatch.UNDELIVERABLE
    assert record["last_error"] == "connection refused"
    assert record["icarus_run_id"] is None
    assert len(_events(db_file, dispatch.VERB_REQUESTED)) == 1
    assert len(_events(db_file, dispatch.VERB_UNDELIVERABLE)) == 1


def test_a_callback_attaches_evidence_and_a_terminal_outcome(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )

        progress = _callback(
            c,
            {
                "icarus_run_id": "icarus-run-1",
                "policy_digest": record["policy_digest"],
                "evidence_ref": "https://ci.example.com/logs/9",
            },
        )
        assert progress.status_code == 202
        assert progress.json()["policy_digest_matches"] is True

        terminal = _callback(
            c,
            {
                "icarus_run_id": "icarus-run-1",
                "policy_digest": record["policy_digest"],
                "completion_ref": "https://example.com/pr/12",
                "outcome": "completed",
            },
        )
        assert terminal.status_code == 202

        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()
        assert final["state"] == "completed"
        assert final["evidence_ref"] == "https://ci.example.com/logs/9"
        assert final["completion_ref"] == "https://example.com/pr/12"

    # The executor's reports join the control-plane trail as events of the run
    # Athena minted — so they appear in that run's replay and lineage.
    evidence = _events(db_file, dispatch.VERB_EVIDENCE)
    assert [row["run_id"] for row in evidence] == [record["run_id"]]
    assert len(_events(db_file, dispatch.VERB_TERMINAL)) == 1


def test_a_callback_without_a_valid_signature_is_refused_before_any_lookup(tmp_path):
    # The executor holds no Athena credential, so HMAC is the whole gate. Refusing
    # before the lookup means this endpoint cannot be used to probe which
    # dispatches exist.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
        payload = {
            "icarus_run_id": "icarus-run-1",
            "policy_digest": record["policy_digest"],
            "evidence_ref": "sneaky",
        }
        unsigned = c.post("/callbacks/icarus", json=payload)
        assert unsigned.status_code == 401

        wrong = c.post(
            "/callbacks/icarus",
            json=payload,
            headers={"X-Athena-Signature": "sha256=" + "0" * 64},
        )
        assert wrong.status_code == 401

        # A correctly signed callback naming a dispatch that does not exist is a
        # 404 — the same answer an unknown run gets from any other read.
        unknown = _callback(
            c,
            {"icarus_run_id": "nobody", "policy_digest": "x"},
        )
        assert unknown.status_code == 404
        assert (
            c.get(f"/dispatches/{record['id']}", headers=H1).json()["evidence_ref"]
            is None
        )


def test_a_policy_digest_mismatch_is_recorded_not_discarded(tmp_path):
    # The digest exists to NOTICE a divergence. Dropping the callback would destroy
    # exactly the evidence it was computed to produce.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
        answered = _callback(
            c,
            {
                "icarus_run_id": "icarus-run-1",
                "policy_digest": "a-different-digest",
                "evidence_ref": "https://ci.example.com/logs/9",
                "outcome": "completed",
            },
        )
        assert answered.status_code == 202
        assert answered.json()["policy_digest_matches"] is False
        # The evidence is still recorded, and flagged alongside it.
        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()
        assert final["evidence_ref"] == "https://ci.example.com/logs/9"
        assert final["state"] == "completed"
    assert len(_events(db_file, dispatch.VERB_DIGEST_MISMATCH)) == 1


def test_a_replayed_callback_does_not_fork_the_record(tmp_path):
    # Executors retry. A retry must not produce a second terminal outcome.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
        payload = {
            "icarus_run_id": "icarus-run-1",
            "policy_digest": record["policy_digest"],
            "completion_ref": "https://example.com/pr/12",
            "outcome": "completed",
        }
        assert _callback(c, payload).status_code == 202
        assert _callback(c, payload).status_code == 202
        # A LATER, different outcome cannot overwrite a settled one either.
        assert (
            _callback(
                c, {**payload, "outcome": "failed", "completion_ref": "nope"}
            ).status_code
            == 202
        )
        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()
        assert final["state"] == "completed"
        assert final["completion_ref"] == "https://example.com/pr/12"
    assert len(_events(db_file, dispatch.VERB_TERMINAL)) == 1


def test_the_same_intent_dispatched_twice_is_one_dispatch(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)

    conn = db.connect(db_file)
    actor = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
    payload = {
        "work_item_id": issue["id"],
        "repo": "git@example.com:acme/app.git",
        "base_commit": "abc123",
        "capability": "repo.edit",
        "idempotency_key": "same-intent",
    }
    first = icarus_commands.request_dispatch(conn, actor=actor, **payload)
    second = icarus_commands.request_dispatch(conn, actor=actor, **payload)
    assert first["id"] == second["id"]
    assert len(dispatch.list_dispatches(conn)) == 1
    conn.close()
    assert len(_events(db_file, dispatch.VERB_REQUESTED)) == 1


def test_dispatching_is_metered_and_gated_like_any_other_write(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        issue = _issue(c)

        # Dispatch is gated under its OWN action kind. It borrowed issue.close's
        # policy row when it first shipped, which conflated two intents the
        # operator decides separately; the decoupling has its own test below.
        c.put(
            f"/approvals/policies/{agent_id}",
            json={"action_kind": "dispatch.request"},
            headers=H1,
        )
        gated = c.post(
            f"/issues/{issue['id']}/dispatch",
            json={
                "repo": "git@example.com:acme/app.git",
                "base_commit": "abc123",
                "capability": "repo.edit",
            },
            headers=_bearer(agent),
        )
        assert gated.status_code == 202
        assert gated.json()["code"] == "approval_required"

        # Approved, it goes through — and the dispatch records that it was.
        request_id = c.get("/approvals?state=pending", headers=H1).json()[0]["id"]
        c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=H1,
        )
        # Budget is charged by the same command, so a spent ceiling refuses it too.
        c.put(
            f"/users/{agent_id}/budget",
            json={"window": "day", "action_limit": 0},
            headers=H1,
        )
        broke = c.post(
            f"/issues/{issue['id']}/dispatch",
            json={
                "repo": "git@example.com:acme/app.git",
                "base_commit": "abc123",
                "capability": "repo.edit",
            },
            headers=_bearer(agent),
        )
        assert broke.status_code == 429
        assert broke.json()["code"] == "agent_budget_exhausted"
    assert _events(db_file, dispatch.VERB_REQUESTED) == []


def test_the_rest_route_dispatches_and_lists(tmp_path, monkeypatch):
    # The command tests inject a poster directly; this one exercises the real route
    # wiring, with the transport stubbed so no test ever reaches the network.
    monkeypatch.setattr(webhooks, "urllib_poster", lambda timeout: _accepting_poster())
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        created = c.post(
            f"/issues/{issue['id']}/dispatch",
            json={
                "repo": "git@example.com:acme/app.git",
                "base_commit": "abc123",
                "capability": "ci.run",
            },
            headers=H1,
        )
        assert created.status_code == 201, created.text
        assert created.json()["state"] == "accepted"
        assert created.json()["approval_state"] == "not_required"

        listed = c.get(f"/dispatches?work_item_id={issue['id']}", headers=H1).json()
        assert [row["id"] for row in listed] == [created.json()["id"]]
        assert c.get("/dispatches?state=accepted", headers=H1).json()
        assert c.get("/dispatches?state=failed", headers=H1).json() == []


def test_an_approved_gate_is_recorded_on_the_dispatch(tmp_path, monkeypatch):
    # The dispatch carries the approval state it was authorized under, and that
    # state is part of the digest — so "this ran with an approval" is checkable
    # later rather than being a story told after the fact.
    monkeypatch.setattr(webhooks, "urllib_poster", lambda timeout: _accepting_poster())
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue = _issue(c)
        c.put(
            f"/approvals/policies/{agent['user']['id']}",
            json={"action_kind": "dispatch.request"},
            headers=H1,
        )
        payload = {
            "repo": "git@example.com:acme/app.git",
            "base_commit": "abc123",
            "capability": "repo.edit",
        }
        assert (
            c.post(
                f"/issues/{issue['id']}/dispatch",
                json=payload,
                headers=_bearer(agent),
            ).status_code
            == 202
        )
        request_id = c.get("/approvals?state=pending", headers=H1).json()[0]["id"]
        c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=H1,
        )
        approved = c.post(
            f"/issues/{issue['id']}/dispatch", json=payload, headers=_bearer(agent)
        )
        assert approved.status_code == 201
        assert approved.json()["approval_state"] == "approved"


def test_a_close_gate_and_a_dispatch_gate_are_separate_intents(tmp_path, monkeypatch):
    """The decoupling proof, in both directions.

    Dispatch originally borrowed issue.close's policy row. That meant gating an
    agent's closes silently gated its dispatches — and, worse, an approval the
    operator granted for CLOSING an issue could be SPENT by a dispatch of that
    issue instead. The operator approved one intent; the agent performed another
    on its authority. These pin the fix: each kind gates only its own action, and
    an approval is only spendable by the intent the operator actually read.
    """
    # This test dispatches twice, and icarus_run_id is UNIQUE — a real executor
    # names each run distinctly, so the stub must too.
    runs = iter(range(1, 100))

    def _distinct_run_poster(url, body, headers):
        return True, json.dumps({"icarus_run_id": f"icarus-run-{next(runs)}"})

    monkeypatch.setattr(webhooks, "urllib_poster", lambda timeout: _distinct_run_poster)
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        # The agent's own issue, so it may close it as creator.
        issue = _issue(c, headers=_bearer(agent))
        payload = {
            "repo": "git@example.com:acme/app.git",
            "base_commit": "abc123",
            "capability": "repo.edit",
        }

        # Gate the agent's CLOSES only.
        c.put(
            f"/approvals/policies/{agent_id}",
            json={"action_kind": "issue.close"},
            headers=H1,
        )

        # Direction one: the close gate does not touch dispatch.
        r = c.post(
            f"/issues/{issue['id']}/dispatch", json=payload, headers=_bearer(agent)
        )
        assert r.status_code == 201, r.text
        assert r.json()["approval_state"] == "not_required"

        # The sharp case. The agent asks to close; the operator approves THAT.
        refused = c.patch(
            f"/issues/{issue['id']}", json={"status": "done"}, headers=_bearer(agent)
        )
        assert refused.status_code == 202
        request_id = c.get("/approvals?state=pending", headers=H1).json()[0]["id"]
        c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=H1,
        )

        # A dispatch of the same issue must NOT spend the close approval. (Before
        # the fix it did: the operator approved "close #N" and the agent's
        # dispatch of #N consumed it.)
        again = c.post(
            f"/issues/{issue['id']}/dispatch",
            json={**payload, "base_commit": "def456"},
            headers=_bearer(agent),
        )
        assert again.status_code == 201
        assert again.json()["approval_state"] == "not_required"
        approvals_view = {a["id"]: a for a in c.get("/approvals", headers=H1).json()}
        assert approvals_view[request_id]["state"] == "approved"
        assert approvals_view[request_id]["consumed_at"] is None

        # ...so the intent the operator actually approved still goes through,
        # consuming the approval it was granted for.
        closed = c.patch(
            f"/issues/{issue['id']}", json={"status": "done"}, headers=_bearer(agent)
        )
        assert closed.status_code == 200, closed.text
        spent = {a["id"]: a for a in c.get("/approvals", headers=H1).json()}
        assert spent[request_id]["consumed_at"] is not None

        # Direction two: a dispatch gate does not touch an ordinary close.
        other = c.post(
            "/issues", json={"title": "again"}, headers=_bearer(agent)
        ).json()
        c.delete(f"/approvals/policies/{agent_id}/issue.close", headers=H1)
        c.put(
            f"/approvals/policies/{agent_id}",
            json={"action_kind": "dispatch.request"},
            headers=H1,
        )
        assert (
            c.patch(
                f"/issues/{other['id']}",
                json={"status": "done"},
                headers=_bearer(agent),
            ).status_code
            == 200
        )
        # And the cockpit vocabulary names both kinds for the operator.
        assert c.get("/users/me", headers=_bearer(agent)).json()[
            "approval_required"
        ] == ["dispatch.request"]


def test_redelivering_an_answered_dispatch_is_a_no_op(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
    record = _dispatch_via_command(
        db_file, issue_id=issue["id"], poster=_accepting_poster()
    )

    conn = db.connect(db_file)

    def must_not_run(url, body, headers):  # pragma: no cover - asserted below
        raise AssertionError("an accepted dispatch must not be sent twice")

    again = icarus_commands.deliver_dispatch(
        conn, dispatch_id=record["id"], poster=must_not_run
    )
    assert again["state"] == dispatch.ACCEPTED
    conn.close()


def test_an_executor_that_answers_with_junk_is_still_correlated(tmp_path):
    # A well-behaved executor returns its run id. One that does not still gets
    # correlated by the idempotency key both sides already share — better than
    # dropping the dispatch over a missing field.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)

    def vague_poster(url, body, headers):
        return True, "OK"

    record = _dispatch_via_command(db_file, issue_id=issue["id"], poster=vague_poster)
    assert record["state"] == dispatch.ACCEPTED
    assert record["icarus_run_id"] == record["idempotency_key"]


def test_dispatch_needs_the_write_role_and_the_issue_scope(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        viewer = c.post(
            "/users",
            json={
                "email": "v@e.com",
                "name": "Vee",
                "password": "pw",
                "role": "viewer",
            },
            headers=H1,
        ).json()
        payload = {
            "repo": "git@example.com:acme/app.git",
            "base_commit": "abc123",
            "capability": "repo.edit",
        }
        assert (
            c.post(
                f"/issues/{issue['id']}/dispatch",
                json=payload,
                headers={"X-Athena-Actor": str(viewer["id"])},
            ).status_code
            == 403
        )
        docs_only = _agent(c, email="d@e.com", scopes=("read", "docs:write"))
        assert (
            c.post(
                f"/issues/{issue['id']}/dispatch",
                json=payload,
                headers=_bearer(docs_only),
            ).status_code
            == 403
        )


def test_dispatch_requires_a_configured_executor(tmp_path, monkeypatch):
    # Half-working is worse than absent: a deployment with no execution fleet
    # should hear so plainly rather than accumulate undeliverable rows.
    monkeypatch.setattr(config, "ICARUS_URL", "")
    monkeypatch.setattr(config, "ICARUS_SECRET", "")
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        refused = c.post(
            f"/issues/{issue['id']}/dispatch",
            json={
                "repo": "git@example.com:acme/app.git",
                "base_commit": "abc123",
                "capability": "repo.edit",
            },
            headers=H1,
        )
        assert refused.status_code == 503
        assert "no execution fleet is configured" in refused.json()["detail"]
        # And the callback is closed too, rather than verifying against an empty
        # secret.
        # A well-formed callback still gets 503 rather than being verified
        # against an empty secret.
        assert (
            c.post(
                "/callbacks/icarus",
                json={"icarus_run_id": "x", "policy_digest": "y"},
                headers={"X-Athena-Signature": "sha256=" + "0" * 64},
            ).status_code
            == 503
        )


def test_egress_reuses_the_ssrf_guard(tmp_path, monkeypatch):
    # A control plane that can be made to POST anywhere is a probe. The dispatch
    # URL goes through the same validation webhooks use, and a blocked target is
    # recorded as undeliverable rather than attempted.
    monkeypatch.setattr(config, "ICARUS_URL", "http://127.0.0.1:9")
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)

    def never_called(url, body, headers):  # pragma: no cover - must not run
        raise AssertionError("the SSRF guard should have refused before posting")

    record = _dispatch_via_command(db_file, issue_id=issue["id"], poster=never_called)
    assert record["state"] == dispatch.UNDELIVERABLE
    assert "internal" in (record["last_error"] or "")


def test_an_executor_run_id_cannot_be_forged_by_a_client(tmp_path):
    # Athena mints `icarus:` runs. A client that could stamp its own writes with
    # one would be able to forge control-plane evidence of what an executor did.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.patch(
            f"/issues/{issue['id']}",
            json={"body": "forged"},
            headers={**H1, "X-Athena-Run": "icarus:pretend"},
        )
    conn = db.connect(db_file)
    stamped = conn.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE run_id LIKE 'icarus:%'"
    ).fetchone()["n"]
    conn.close()
    assert stamped == 0


def test_dispatch_input_and_reads_are_bounded(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        for payload in (
            {"repo": "  ", "base_commit": "abc", "capability": "repo.edit"},
            {"repo": "r", "base_commit": "  ", "capability": "repo.edit"},
            {"repo": "r", "base_commit": "abc", "capability": "rm -rf"},
        ):
            assert (
                c.post(
                    f"/issues/{issue['id']}/dispatch", json=payload, headers=H1
                ).status_code
                == 422
            )
        assert c.get("/dispatches?limit=0", headers=H1).status_code == 422
        assert c.get("/dispatches/999999", headers=H1).status_code == 404

    conn = db.connect(db_file)
    actor = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
    with pytest.raises(dispatch.DispatchError) as anonymous:
        icarus_commands.request_dispatch(
            conn,
            actor=None,
            work_item_id=issue["id"],
            repo="r",
            base_commit="c",
            capability="repo.edit",
        )
    assert anonymous.value.kind == "unauthorized"
    with pytest.raises(dispatch.DispatchError) as missing:
        icarus_commands.request_dispatch(
            conn,
            actor=actor,
            work_item_id=999999,
            repo="r",
            base_commit="c",
            capability="repo.edit",
        )
    assert missing.value.kind == "not_found"
    with pytest.raises(dispatch.DispatchError) as unknown:
        icarus_commands.deliver_dispatch(conn, dispatch_id=999999)
    assert unknown.value.kind == "not_found"
    with pytest.raises(ValueError):
        dispatch.list_dispatches(conn, limit=0)
    conn.close()


def test_the_digest_covers_the_authorization_that_was_in_force(tmp_path):
    # Two dispatches that differ only in the authorization behind them must not
    # produce the same digest, or the digest proves nothing.
    facts = dispatch.PolicyFacts(
        actor_id=1,
        scopes=("issue:write",),
        work_item_id=7,
        repo="r",
        base_commit="c",
        capability="repo.edit",
        approval_state="not_required",
        budget_window=None,
        budget_limit=None,
    )
    import dataclasses

    assert (
        facts.digest() == dataclasses.replace(facts, scopes=("issue:write",)).digest()
    )
    assert facts.digest() != dataclasses.replace(facts, scopes=("admin",)).digest()
    assert (
        facts.digest() != dataclasses.replace(facts, approval_state="approved").digest()
    )
    assert facts.digest() != dataclasses.replace(facts, budget_limit=50).digest()
    # Scope ORDER must not change the digest — it is a set, and a digest that
    # depended on ordering would produce false mismatches.
    assert (
        dataclasses.replace(facts, scopes=("a", "b")).digest()
        == dataclasses.replace(facts, scopes=("b", "a")).digest()
    )
