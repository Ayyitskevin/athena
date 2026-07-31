"""Handing work to an external executor, and hearing back.

Athena is the control plane; the executor is a separate system with its own store.
They share no database and neither imports the other. What Athena keeps is a record
of what it ASKED and what it was TOLD — and the tests below are mostly about
keeping those two things apart.

* `accepted` means "the executor said it accepted". It never means work is running,
  and no read here will imply otherwise.
* The outbound call happens AFTER the record commits, so the durable fact "Athena
  decided to dispatch this" survives a far side that never answers.
* Operator reads inherit issue visibility in SQL BEFORE their bound, so executor
  metadata cannot reveal private work or make hidden rows consume a visible page.
* A callback carries no Athena credential — it is authenticated by HMAC over the
  exact body — and can do exactly two things: attach evidence, and report an
  outcome.
* The policy digest is tamper-EVIDENT: a mismatch is recorded, not discarded,
  because destroying the evidence would defeat the point of computing it.
"""

import hashlib
import hmac
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import athena.main as athena_main
from athena import config
from athena.aegis import icarus_commands
from athena.core import access, activity, db, dispatch, webhooks
from athena.core.deps import get_conn
from athena.main import create_app
from athena.mcp.client import AthenaClient

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


def _pending_dispatch(db_file, *, actor_id=1, issue_id, **overrides):
    conn = db.connect(db_file)
    actor = dict(
        conn.execute("SELECT * FROM users WHERE id = ?", (actor_id,)).fetchone()
    )
    record = icarus_commands.request_dispatch(
        conn,
        actor=actor,
        work_item_id=issue_id,
        repo="git@example.com:acme/app.git",
        base_commit="abc123",
        capability="repo.edit",
        **overrides,
    )
    conn.close()
    return record


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
                # Terminal callbacks are cumulative: repeating the canonical
                # evidence makes progress-before-terminal and terminal-before-
                # progress converge even when the network reorders them.
                "evidence_ref": "https://ci.example.com/logs/9",
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


def test_callback_authentication_precedes_json_parsing_and_database(tmp_path):
    # HMAC is the callback's entire identity boundary. Malformed JSON must not
    # reveal that it is malformed until the raw bytes authenticate, and neither
    # refusal may spend a SQLite connection.
    app, _ = _app(tmp_path)

    def database_must_not_open():
        raise AssertionError("callback perimeter should have refused first")

    app.dependency_overrides[get_conn] = database_must_not_open
    malformed = b'{"not":'
    wrong_headers = {
        "Content-Type": "application/json",
        "X-Athena-Signature": "sha256=" + "0" * 64,
    }
    good_headers = {
        "Content-Type": "application/json",
        "X-Athena-Signature": webhooks.sign(SECRET, malformed),
    }
    with TestClient(app) as c:
        unsigned = c.post(
            "/callbacks/icarus",
            content=malformed,
            headers={"Content-Type": "application/json"},
        )
        wrong = c.post("/callbacks/icarus", content=malformed, headers=wrong_headers)
        authenticated = c.post(
            "/callbacks/icarus", content=malformed, headers=good_headers
        )
        invalid_utf8 = b"\xff"
        authenticated_invalid_utf8 = c.post(
            "/callbacks/icarus",
            content=invalid_utf8,
            headers={
                "Content-Type": "application/json",
                "X-Athena-Signature": webhooks.sign(SECRET, invalid_utf8),
            },
        )
        non_ascii = c.post(
            "/callbacks/icarus",
            content=b"{}",
            headers=[
                (b"content-type", b"application/json"),
                (b"x-athena-signature", b"sha256=\xff"),
            ],
        )

        assert unsigned.status_code == wrong.status_code == 401
        assert unsigned.json() == wrong.json() == {"detail": "invalid signature"}
        assert authenticated.status_code == 422
        assert authenticated.json() == {"detail": "invalid callback payload"}
        assert authenticated_invalid_utf8.status_code == 422
        assert authenticated_invalid_utf8.json() == {
            "detail": "invalid callback payload"
        }
        assert non_ascii.status_code == 401

        # Raw parsing would otherwise make FastAPI lose the body contract.
        operation = c.get("/openapi.json").json()["paths"]["/callbacks/icarus"]["post"]
        assert operation["requestBody"]["required"] is True
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert set(schema["required"]) == {"icarus_run_id", "policy_digest"}
        signature = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "x-athena-signature"
        )
        assert signature["required"] is True
        assert operation["responses"]["202"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/CallbackOut"}
        error_schema = operation["responses"]["422"]["content"]["application/json"][
            "schema"
        ]
        assert error_schema == {"$ref": "#/components/schemas/CallbackErrorOut"}
        assert (
            c.get("/openapi.json").json()["components"]["schemas"]["CallbackErrorOut"][
                "properties"
            ]["detail"]["type"]
            == "string"
        )


def test_bad_callback_headers_cannot_trigger_pre_hmac_sqlite(tmp_path, monkeypatch):
    # Browser sessions and durable REST idempotency do not apply to a machine
    # callback. Attacker-supplied versions of both headers must be ignored until
    # the raw-body HMAC has authenticated, rather than opening SQLite first.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        with monkeypatch.context() as patch:

            def database_must_not_open(*args, **kwargs):
                raise AssertionError("unsigned callback reached SQLite")

            patch.setattr(athena_main.db, "connect", database_must_not_open)
            refused = c.post(
                "/callbacks/icarus",
                content=b'{"not":',
                headers={
                    "Content-Type": "application/json",
                    "Cookie": f"{config.SESSION_COOKIE}=attacker-controlled",
                    "Idempotency-Key": "attacker-controlled",
                    "X-Athena-Signature": "sha256=" + "0" * 64,
                },
            )
        assert refused.status_code == 401
        assert refused.json() == {"detail": "invalid signature"}


def test_callback_attempts_charge_the_anonymous_perimeter_limit(tmp_path):
    # Invalid signatures have no actor to charge. The direct peer-IP budget is
    # consumed before body work or a DB dependency, so a bad-signature flood is
    # bounded just like the forge inbound path.
    app = create_app(tmp_path / "callback-burst.db", anon_rate_limit_per_minute=2)

    def database_must_not_open():
        raise AssertionError("invalid callbacks must not open SQLite")

    app.dependency_overrides[get_conn] = database_must_not_open
    headers = {
        "Content-Type": "application/json",
        "X-Athena-Signature": "sha256=" + "0" * 64,
    }
    with TestClient(app) as c:
        assert (
            c.post("/callbacks/icarus", content=b"{", headers=headers).status_code
            == 401
        )
        assert (
            c.post("/callbacks/icarus", content=b"{", headers=headers).status_code
            == 401
        )
        limited = c.post("/callbacks/icarus", content=b"{", headers=headers)

    assert limited.status_code == 429
    assert limited.json() == {"detail": "anonymous rate limit exceeded"}
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    assert limited.headers["x-ratelimit-limit"] == "2"
    assert limited.headers["x-ratelimit-remaining"] == "0"


@pytest.mark.parametrize(
    ("path", "root_path"),
    [
        ("/callbacks/icarus", ""),
        ("/callbacks/icarus/", ""),
        ("/athena/callbacks/icarus", "/athena"),
        ("/athena/forge/gh", "/athena"),
    ],
)
def test_exhausted_signed_inbound_limit_refuses_before_reading_the_body(
    tmp_path, path, root_path
):
    # The limiter is outer ASGI middleware, not a route check. Once the peer has
    # spent its unit, Athena must produce 429 without invoking receive(), even if
    # the request carries cookie/idempotency headers that normally engage global
    # middleware.
    import asyncio

    app = create_app(tmp_path / "callback-outer-limit.db", anon_rate_limit_per_minute=1)
    assert app.state.anon_rate_limiter.check("testclient").allowed is True
    messages = []

    async def receive():  # pragma: no cover - invocation is the failure
        raise AssertionError("exhausted signed-inbound peer body was read")

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"content-length", b"1048576"),
            (b"cookie", f"{config.SESSION_COOKIE}=attacker".encode()),
            (b"idempotency-key", b"attacker"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": root_path,
    }
    with TestClient(app):
        asyncio.run(app(scope, receive, send))

    started = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert started["status"] == 429
    headers = dict(started["headers"])
    assert headers[b"retry-after"]
    assert headers[b"cache-control"] == b"private, no-store"


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


def test_non_ascii_policy_digest_is_flagged_without_crashing(tmp_path):
    # compare_digest rejects non-ASCII str operands. A signed executor report with
    # one is still evidence of policy divergence: accept the report, mark false,
    # and write a safe audit label instead of leaking malformed text into SQLite.
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
                "policy_digest": "ÿ",
                "evidence_ref": "example://evidence/canonical",
                "completion_ref": "example://result/complete",
                "outcome": "completed",
            },
        )
        assert answered.status_code == 202
        assert answered.json()["policy_digest_matches"] is False
        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()
        assert final["state"] == "completed"
        assert final["evidence_ref"] == "example://evidence/canonical"

        # JSON permits an escaped lone surrogate, but SQLite cannot encode it.
        # The authenticated malformed field is a 422, never another 500.
        surrogate = _callback(
            c,
            {"icarus_run_id": "unknown", "policy_digest": "\ud800"},
        )
        assert surrogate.status_code == 422

    mismatch = _events(db_file, dispatch.VERB_DIGEST_MISMATCH)
    assert len(mismatch) == 1
    assert "reported <malformed>" in mismatch[0]["detail"]


def test_evidence_is_immutable_and_callback_replays_are_audited_once(tmp_path):
    # Callback v1 carries no sequence. Athena can prove equality, not recency, so
    # the first evidence pointer is canonical: exact retry is a no-op, a different
    # open-state pointer conflicts, and terminal state absorbs anything late.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
        progress = {
            "icarus_run_id": "icarus-run-1",
            "policy_digest": record["policy_digest"],
            "evidence_ref": "example://evidence/canonical",
        }
        assert _callback(c, progress).status_code == 202
        assert _callback(c, progress).status_code == 202

        conflict = _callback(
            c,
            {
                **progress,
                "policy_digest": "wrong-on-conflict",
                "evidence_ref": "example://evidence/other",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json() == {
            "detail": "evidence_ref is immutable for this dispatch"
        }

        terminal = {
            "icarus_run_id": "icarus-run-1",
            "policy_digest": record["policy_digest"],
            "evidence_ref": "example://evidence/canonical",
            "completion_ref": "example://result/complete",
            "outcome": "completed",
        }
        assert _callback(c, terminal).status_code == 202
        # Different evidence and another mismatch after terminal are both absorbed.
        late = _callback(
            c,
            {
                **terminal,
                "policy_digest": "another-mismatch",
                "evidence_ref": "example://evidence/late",
                "outcome": "failed",
            },
        )
        assert late.status_code == 202
        assert late.json()["policy_digest_matches"] is False

        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()
        assert final["state"] == "completed"
        assert final["evidence_ref"] == "example://evidence/canonical"
        assert final["completion_ref"] == "example://result/complete"

    assert len(_events(db_file, dispatch.VERB_EVIDENCE)) == 1
    assert len(_events(db_file, dispatch.VERB_TERMINAL)) == 1
    assert len(_events(db_file, dispatch.VERB_DIGEST_MISMATCH)) == 1


def test_racing_evidence_callbacks_choose_one_canonical_pointer(tmp_path):
    # BEGIN IMMEDIATE serializes the decision: two different first reports cannot
    # both observe NULL and overwrite each other. One lands; the other sees the
    # canonical pointer and conflicts without a second audit event.
    import threading

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
        barrier = threading.Barrier(2)
        statuses: list[int] = []

        def report(ref):
            client = TestClient(app)
            barrier.wait(timeout=5)
            statuses.append(
                _callback(
                    client,
                    {
                        "icarus_run_id": "icarus-run-1",
                        "policy_digest": record["policy_digest"],
                        "evidence_ref": ref,
                    },
                ).status_code
            )

        refs = ("example://evidence/one", "example://evidence/two")
        threads = [threading.Thread(target=report, args=(ref,)) for ref in refs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(statuses) == [202, 409]
        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()
        assert final["evidence_ref"] in refs

    evidence = _events(db_file, dispatch.VERB_EVIDENCE)
    assert len(evidence) == 1
    assert evidence[0]["detail"] == final["evidence_ref"]


def test_cumulative_terminal_is_safe_when_it_arrives_before_progress(tmp_path):
    # A well-behaved terminal callback repeats the canonical evidence pointer.
    # Whichever callback arrives first, the durable row and audit trail converge.
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
            "evidence_ref": "example://evidence/canonical",
        }
        terminal = _callback(
            c,
            {
                **payload,
                "completion_ref": "example://result/complete",
                "outcome": "completed",
            },
        )
        assert terminal.status_code == 202
        assert _callback(c, payload).status_code == 202
        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()

    assert final["state"] == "completed"
    assert final["evidence_ref"] == "example://evidence/canonical"
    assert len(_events(db_file, dispatch.VERB_EVIDENCE)) == 1
    assert len(_events(db_file, dispatch.VERB_TERMINAL)) == 1


def test_legacy_terminal_then_progress_fills_only_missing_evidence(tmp_path):
    # evidence_ref remains optional in callback v1. A legacy terminal report may
    # arrive before its progress twin, so terminal state may fill a NULL pointer
    # once while still refusing every overwrite.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
        terminal = {
            "icarus_run_id": "icarus-run-1",
            "policy_digest": record["policy_digest"],
            "completion_ref": "example://result/complete",
            "outcome": "completed",
        }
        assert _callback(c, terminal).status_code == 202
        assert (
            _callback(
                c,
                {
                    "icarus_run_id": "icarus-run-1",
                    "policy_digest": record["policy_digest"],
                    "evidence_ref": "example://evidence/delayed",
                },
            ).status_code
            == 202
        )
        # A second, different late pointer cannot replace the first.
        assert (
            _callback(
                c,
                {
                    "icarus_run_id": "icarus-run-1",
                    "policy_digest": record["policy_digest"],
                    "evidence_ref": "example://evidence/other",
                },
            ).status_code
            == 202
        )
        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()

    assert final["state"] == "completed"
    assert final["evidence_ref"] == "example://evidence/delayed"
    assert len(_events(db_file, dispatch.VERB_EVIDENCE)) == 1
    assert len(_events(db_file, dispatch.VERB_TERMINAL)) == 1


def test_first_late_policy_mismatch_is_still_audited_once(tmp_path):
    # Terminal state absorbs outcome/evidence mutation, not security evidence. A
    # first mismatch reported afterward must remain visible, and its replay must
    # not spam the trail.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
        terminal = {
            "icarus_run_id": "icarus-run-1",
            "policy_digest": record["policy_digest"],
            "completion_ref": "example://result/complete",
            "outcome": "completed",
        }
        assert _callback(c, terminal).status_code == 202
        mismatch = {
            "icarus_run_id": "icarus-run-1",
            "policy_digest": "late-mismatch",
        }
        first = _callback(c, mismatch)
        replay = _callback(c, mismatch)
        assert first.status_code == replay.status_code == 202
        assert first.json()["policy_digest_matches"] is False

    assert len(_events(db_file, dispatch.VERB_DIGEST_MISMATCH)) == 1


def test_evidence_and_audit_roll_back_together(tmp_path, monkeypatch):
    # The command owns both writes. If audit insertion fails, no evidence pointer
    # may survive without the event that explains where it came from.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )

    original_record = activity.record

    def fail_evidence_event(conn, **kwargs):
        if kwargs["verb"] == dispatch.VERB_EVIDENCE:
            raise RuntimeError("audit unavailable")
        return original_record(conn, **kwargs)

    monkeypatch.setattr(activity, "record", fail_evidence_event)
    conn = db.connect(db_file)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        icarus_commands.apply_callback(
            conn,
            icarus_run_id="icarus-run-1",
            policy_digest=record["policy_digest"],
            evidence_ref="example://evidence/canonical",
        )
    conn.close()

    conn = db.connect(db_file)
    final = dispatch.get_dispatch(conn, record["id"])
    conn.close()
    assert final is not None
    assert final["evidence_ref"] is None
    assert len(_events(db_file, dispatch.VERB_EVIDENCE)) == 0


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"icarus_run_id": 7}, "icarus_run_id must be a string"),
        ({"icarus_run_id": ""}, "icarus_run_id must be 1-500 characters"),
        ({"icarus_run_id": "x" * 501}, "icarus_run_id must be 1-500 characters"),
        (
            {"icarus_run_id": "bad\nrun"},
            "icarus_run_id must not contain control characters",
        ),
        ({"policy_digest": "\ud800"}, "policy_digest must be valid Unicode text"),
        ({"evidence_ref": 7}, "evidence_ref must be a string"),
        (
            {"completion_ref": "example://result"},
            "completion_ref requires a terminal outcome",
        ),
        ({"outcome": "running"}, "outcome must be one of: completed, failed"),
    ],
)
def test_callback_command_validates_every_database_bound_field(
    tmp_path, overrides, detail
):
    # The command is a real boundary, not a route helper: a future adapter calling
    # it directly cannot bind malformed text to SQLite or bypass the wire model.
    app, db_file = _app(tmp_path)
    with TestClient(app):
        pass
    payload = {
        "icarus_run_id": "unknown",
        "policy_digest": "digest",
        **overrides,
    }
    conn = db.connect(db_file)
    with pytest.raises(icarus_commands.IcarusCommandError) as refused:
        icarus_commands.apply_callback(conn, **payload)
    conn.close()
    assert refused.value.kind == "invalid"
    assert refused.value.detail == detail


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


def test_record_terminal_never_resettles_a_dispatch(tmp_path):
    # The predicated UPDATE is the backstop for the callback race: a terminal
    # write against an already-settled dispatch is a 0-row no-op (False), never
    # an overwrite — so a late conflicting callback cannot flip the outcome even
    # if its view of the row was stale.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )
    conn = db.connect(db_file)
    try:
        assert (
            dispatch.record_terminal(
                conn,
                dispatch_id=record["id"],
                state="completed",
                completion_ref="ref-first",
            )
            is True
        )
        conn.commit()
        assert (
            dispatch.record_terminal(
                conn,
                dispatch_id=record["id"],
                state="failed",
                completion_ref="ref-second",
            )
            is False
        )
        conn.commit()
        final = dispatch.get_dispatch(conn, record["id"])
        assert final["state"] == "completed"
        assert final["completion_ref"] == "ref-first"
    finally:
        conn.close()


def test_racing_terminal_callbacks_settle_exactly_once(tmp_path):
    # Two workers' callbacks can arrive together under a multi-worker deployment.
    # The first to commit wins; the loser is accepted (202) and ignored — no
    # flipped outcome, no duplicate terminal event. Regression test for the row
    # being read BEFORE BEGIN IMMEDIATE with an unconditional terminal UPDATE:
    # the loser's stale snapshot said "open" and its write flipped the winner's
    # outcome.
    import threading

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file, issue_id=issue["id"], poster=_accepting_poster()
        )

        barrier = threading.Barrier(2)
        statuses: list[int] = []

        def settle(outcome):
            client = TestClient(app)
            barrier.wait(timeout=5)
            statuses.append(
                _callback(
                    client,
                    {
                        "icarus_run_id": "icarus-run-1",
                        "policy_digest": record["policy_digest"],
                        "completion_ref": f"ref-{outcome}",
                        "outcome": outcome,
                    },
                ).status_code
            )

        threads = [
            threading.Thread(target=settle, args=(outcome,))
            for outcome in ("completed", "failed")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(statuses) == [202, 202]
        final = c.get(f"/dispatches/{record['id']}", headers=H1).json()
        # Whichever won, the record is coherent: the ref belongs to the outcome.
        assert final["state"] in ("completed", "failed")
        assert final["completion_ref"] == f"ref-{final['state']}"

    events = _events(db_file, dispatch.VERB_TERMINAL)
    assert len(events) == 1
    assert events[0]["detail"].startswith(final["state"])


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
    assert len(dispatch.list_dispatches(conn, actor=actor)) == 1
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


def test_dispatch_visibility_and_record_share_one_write_transaction(
    tmp_path, monkeypatch
):
    """WHY: membership revocation must not land after authorization but before
    the dispatch and its audit fact are recorded."""

    app, db_file = _app(tmp_path, "dispatch-visibility-race.db")
    with TestClient(app) as c:
        _bootstrap(c)
        member = c.post(
            "/users",
            json={
                "email": "member@e.com",
                "name": "Member",
                "password": "pw",
                "role": "member",
            },
            headers=H1,
        ).json()
        project = c.post(
            "/projects",
            json={"name": "Private", "key": "RACE"},
            headers=H1,
        ).json()
        issue = c.post(
            "/issues",
            json={"title": "Authorized once", "project_id": project["id"]},
            headers=H1,
        ).json()
        assert (
            c.put(
                f"/projects/{project['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )
        assert (
            c.post(
                f"/projects/{project['id']}/members",
                json={"user_id": member["id"]},
                headers=H1,
            ).status_code
            == 201
        )

    command_conn = db.connect(db_file)
    actor = dict(
        command_conn.execute(
            "SELECT * FROM users WHERE id = ?", (member["id"],)
        ).fetchone()
    )
    original_can_see = access.can_see_issue
    revocation_blocked = False

    def check_while_revocation_races(conn, checked_actor, issue_id):
        nonlocal revocation_blocked
        visible = original_can_see(conn, checked_actor, issue_id)
        assert visible

        revoker = db.connect(db_file)
        revoker.execute("PRAGMA busy_timeout = 0")
        try:
            access.remove_project_member(revoker, project["id"], member["id"])
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
            revoker.rollback()
            revocation_blocked = True
        finally:
            revoker.close()
        return visible

    monkeypatch.setattr(access, "can_see_issue", check_while_revocation_races)
    record = icarus_commands.request_dispatch(
        command_conn,
        actor=actor,
        work_item_id=issue["id"],
        repo="git@example.com:acme/app.git",
        base_commit="abc123",
        capability="repo.edit",
    )
    command_conn.close()

    assert revocation_blocked
    assert record["work_item_id"] == issue["id"]

    # Once the command commits, the policy change can be retried and governs the
    # next command. The first decision remains an auditable serialization,
    # not a stale authorization applied after revocation.
    monkeypatch.setattr(access, "can_see_issue", original_can_see)
    revoker = db.connect(db_file)
    assert access.remove_project_member(revoker, project["id"], member["id"])
    assert not access.can_see_issue(revoker, actor, issue["id"])
    revoker.close()


def test_dispatch_reads_cannot_bypass_private_issue_visibility(tmp_path, monkeypatch):
    """WHY: a dispatch repeats repository, commit, run, policy, error, and evidence
    metadata from its issue. Gating the issue while exposing its dispatch therefore
    discloses the private work, and filtering only after LIMIT makes page fullness an
    existence oracle."""

    hidden_failed_id: int | None = None

    def poster(url, body, headers):
        envelope = json.loads(body)
        if envelope["work_item_id"] == hidden_failed_id:
            return False, "private executor failure"
        return True, json.dumps(
            {"icarus_run_id": f"visible-test-{envelope['dispatch_id']}"}
        )

    monkeypatch.setattr(webhooks, "urllib_poster", lambda timeout: poster)
    app, _ = _app(tmp_path, "dispatch-visibility.db")
    with TestClient(app) as c:
        _bootstrap(c)
        creator = c.post(
            "/users",
            json={
                "email": "creator@e.com",
                "name": "Creator",
                "password": "pw",
                "role": "member",
            },
            headers=H1,
        ).json()
        assert creator["role"] == "member"
        creator_headers = {"X-Athena-Actor": str(creator["id"])}
        outsider = _agent(c, email="outsider@e.com", scopes=("read",))
        worker = _agent(c, email="worker@e.com", scopes=("issue:write",))
        docs_only = _agent(c, email="docs@e.com", scopes=("docs:write",))
        outsider_headers = _bearer(outsider)

        public_project = c.post(
            "/projects",
            json={"name": "Public", "key": "PUB"},
            headers=H1,
        ).json()
        public_issue = c.post(
            "/issues",
            json={"title": "Public project work", "project_id": public_project["id"]},
            headers=H1,
        ).json()
        backlog_issue = _issue(c, "Shared backlog work")
        private_project = c.post(
            "/projects",
            json={"name": "Private", "key": "PRV"},
            headers=creator_headers,
        ).json()
        hidden_issue = c.post(
            "/issues",
            json={"title": "Secret work", "project_id": private_project["id"]},
            headers=creator_headers,
        ).json()
        hidden_failed = c.post(
            "/issues",
            json={"title": "Secret failed work", "project_id": private_project["id"]},
            headers=creator_headers,
        ).json()
        hidden_failed_id = hidden_failed["id"]
        assert (
            c.put(
                f"/projects/{private_project['id']}/visibility",
                json={"visibility": "private"},
                headers=creator_headers,
            ).status_code
            == 200
        )

        dispatch_payload = {
            "repo": "git@example.com:acme/private.git",
            "base_commit": "abc123",
            "capability": "repo.edit",
        }

        def send(issue_id, headers):
            response = c.post(
                f"/issues/{issue_id}/dispatch",
                json=dispatch_payload,
                headers=headers,
            )
            assert response.status_code == 201, response.text
            return response.json()

        # Visible rows are deliberately oldest. A post-LIMIT Python filter would
        # inspect only the two newest hidden rows and return an empty page.
        public_record = send(public_issue["id"], H1)
        backlog_record = send(backlog_issue["id"], H1)
        hidden_record = send(hidden_issue["id"], creator_headers)
        hidden_failed_record = send(hidden_failed["id"], creator_headers)
        assert hidden_failed_record["state"] == dispatch.UNDELIVERABLE

        assert (
            c.get(f"/issues/{hidden_issue['id']}", headers=outsider_headers).status_code
            == 404
        )
        visible = c.get("/dispatches?limit=2", headers=outsider_headers)
        assert visible.status_code == 200
        assert [row["id"] for row in visible.json()] == [
            backlog_record["id"],
            public_record["id"],
        ]
        assert (
            c.get(
                f"/dispatches?work_item_id={hidden_issue['id']}",
                headers=outsider_headers,
            ).json()
            == []
        )
        assert (
            c.get(
                f"/dispatches?work_item_id={hidden_issue['id'] + 1_000_000}",
                headers=outsider_headers,
            ).json()
            == []
        )
        assert (
            c.get("/dispatches?state=undeliverable", headers=outsider_headers).json()
            == []
        )

        hidden_response = c.get(
            f"/dispatches/{hidden_record['id']}", headers=outsider_headers
        )
        missing_response = c.get("/dispatches/999999", headers=outsider_headers)
        assert hidden_response.status_code == missing_response.status_code == 404
        assert (
            hidden_response.json()
            == missing_response.json()
            == {"detail": "no such dispatch"}
        )
        assert c.get(f"/dispatches/{hidden_record['id']}").status_code == 401
        assert c.get("/dispatches", headers=_bearer(worker)).status_code == 200
        docs_denied = c.get("/dispatches", headers=_bearer(docs_only))
        assert docs_denied.status_code == 403
        assert (
            docs_denied.json()["detail"] == "token scope required: read or issue:write"
        )
        admin_token = c.post(
            "/tokens",
            json={"name": "dispatch-admin", "scopes": ["admin"]},
            headers=H1,
        ).json()
        assert (
            c.get(
                "/dispatches",
                headers={"Authorization": f"Bearer {admin_token['token']}"},
            ).status_code
            == 200
        )

        # The MCP client is a REST client; pin the same scoped-token behavior so
        # the agent surface cannot drift into a second visibility model.
        agent_http = TestClient(app)
        agent_http.headers.update(outsider_headers)
        with agent_http:
            through_mcp = AthenaClient(client=agent_http).list_dispatches(limit=2)
        assert [row["id"] for row in through_mcp] == [
            backlog_record["id"],
            public_record["id"],
        ]

        # Admins and the private project's non-admin creator retain access.
        assert len(c.get("/dispatches", headers=H1).json()) == 4
        assert (
            c.get(
                f"/dispatches/{hidden_record['id']}", headers=creator_headers
            ).status_code
            == 200
        )

        # Membership changes take effect on the next read; a read-scoped agent may
        # read a project it is a member of but still gains no write scope.
        granted = c.post(
            f"/projects/{private_project['id']}/members",
            json={"user_id": outsider["user"]["id"]},
            headers=creator_headers,
        )
        assert granted.status_code == 201, granted.text
        assert (
            c.get(
                f"/dispatches/{hidden_record['id']}", headers=outsider_headers
            ).status_code
            == 200
        )
        assert {
            hidden_record["id"],
            hidden_failed_record["id"],
        }.issubset(
            {row["id"] for row in c.get("/dispatches", headers=outsider_headers).json()}
        )
        assert (
            c.delete(
                f"/projects/{private_project['id']}/members/{outsider['user']['id']}",
                headers=creator_headers,
            ).status_code
            == 200
        )
        assert (
            c.get(
                f"/dispatches/{hidden_record['id']}", headers=outsider_headers
            ).status_code
            == 404
        )

        # Visibility changes are live too; no dispatch row caches project access.
        assert (
            c.put(
                f"/projects/{private_project['id']}/visibility",
                json={"visibility": "public"},
                headers=creator_headers,
            ).status_code
            == 200
        )
        assert (
            c.get(
                f"/dispatches/{hidden_record['id']}", headers=outsider_headers
            ).status_code
            == 200
        )


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


def test_concurrent_delivery_acceptances_are_first_writer_wins(tmp_path):
    # The outbound idempotency key requires the executor to single-flight repeated
    # POSTs. Athena still defends its own side if a broken executor answers two
    # concurrent attempts with different run ids: one state and one audit fact win.
    import threading

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
    record = _pending_dispatch(db_file, issue_id=issue["id"])
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def deliver(run_id):
        conn = db.connect(db_file)

        def poster(url, body, headers):
            barrier.wait(timeout=5)
            return True, json.dumps({"icarus_run_id": run_id})

        try:
            results.append(
                icarus_commands.deliver_dispatch(
                    conn,
                    dispatch_id=record["id"],
                    poster=poster,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=deliver, args=("run-A",)),
        threading.Thread(target=deliver, args=("run-B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    conn = db.connect(db_file)
    final = dispatch.get_dispatch(conn, record["id"])
    conn.close()
    assert final is not None
    assert final["state"] == dispatch.ACCEPTED
    assert final["icarus_run_id"] in {"run-A", "run-B"}
    accepted = _events(db_file, dispatch.VERB_ACCEPTED)
    assert len(accepted) == 1
    assert accepted[0]["detail"] == f"executor run {final['icarus_run_id']}"


@pytest.mark.parametrize(
    ("poster", "failed_verb"),
    [
        (_accepting_poster(), dispatch.VERB_ACCEPTED),
        (
            lambda url, body, headers: (False, "executor unavailable"),
            dispatch.VERB_UNDELIVERABLE,
        ),
    ],
)
def test_delivery_state_and_audit_roll_back_together(
    tmp_path, monkeypatch, poster, failed_verb
):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
    record = _pending_dispatch(db_file, issue_id=issue["id"])
    original_record = activity.record

    def fail_delivery_event(conn, **kwargs):
        if kwargs["verb"] == failed_verb:
            raise RuntimeError("audit unavailable")
        return original_record(conn, **kwargs)

    monkeypatch.setattr(activity, "record", fail_delivery_event)
    conn = db.connect(db_file)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        icarus_commands.deliver_dispatch(
            conn,
            dispatch_id=record["id"],
            poster=poster,
        )
    conn.close()

    conn = db.connect(db_file)
    final = dispatch.get_dispatch(conn, record["id"])
    conn.close()
    assert final is not None
    assert final["state"] == dispatch.PENDING_DELIVERY
    assert final["icarus_run_id"] is None
    assert final["last_error"] is None
    assert len(_events(db_file, failed_verb)) == 0


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


def test_executor_run_id_round_trips_without_transformation(tmp_path):
    # The run id is the executor's opaque correlation key. Trimming it on
    # acceptance would store a value the executor was never told and make its
    # natural echo return 404.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        record = _dispatch_via_command(
            db_file,
            issue_id=issue["id"],
            poster=_accepting_poster(run_id=" run with spaces "),
        )
        assert record["icarus_run_id"] == " run with spaces "
        callback = _callback(
            c,
            {
                "icarus_run_id": " run with spaces ",
                "policy_digest": record["policy_digest"],
            },
        )
        assert callback.status_code == 202


@pytest.mark.parametrize(
    "run_id",
    [None, 7, "", "   ", "bad\nrun", "x" * 501, "\ud800"],
)
def test_invalid_executor_run_id_marks_delivery_undeliverable(tmp_path, run_id):
    # Acceptance and callback vocabularies are one contract. Never store a key
    # the callback boundary must reject; retain the dispatch as a visible
    # delivery failure instead.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
    record = _dispatch_via_command(
        db_file,
        issue_id=issue["id"],
        poster=_accepting_poster(run_id=run_id),
    )
    assert record["state"] == dispatch.UNDELIVERABLE
    assert record["icarus_run_id"] is None
    assert record["last_error"] == "executor returned an invalid icarus_run_id"
    assert len(_events(db_file, dispatch.VERB_ACCEPTED)) == 0
    assert len(_events(db_file, dispatch.VERB_UNDELIVERABLE)) == 1


def test_duplicate_executor_run_id_marks_only_the_second_delivery_undeliverable(
    tmp_path,
):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        first_issue = _issue(c, "first")
        second_issue = _issue(c, "second")
    first = _dispatch_via_command(
        db_file,
        issue_id=first_issue["id"],
        poster=_accepting_poster(run_id="shared-run"),
    )
    second = _dispatch_via_command(
        db_file,
        issue_id=second_issue["id"],
        poster=_accepting_poster(run_id="shared-run"),
    )
    assert first["state"] == dispatch.ACCEPTED
    assert second["state"] == dispatch.UNDELIVERABLE
    assert second["last_error"] == "executor returned a duplicate icarus_run_id"


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
        oversized = dispatch.MAX_SQLITE_INTEGER + 1
        assert c.get(f"/dispatches/{oversized}", headers=H1).status_code == 422
        assert (
            c.get(f"/dispatches?work_item_id={oversized}", headers=H1).status_code
            == 422
        )
        assert (
            c.post(
                f"/issues/{oversized}/dispatch",
                json={"repo": "r", "base_commit": "c", "capability": "repo.edit"},
                headers=H1,
            ).status_code
            == 422
        )

    conn = db.connect(db_file)
    actor = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
    with pytest.raises(icarus_commands.IcarusCommandError) as anonymous:
        icarus_commands.request_dispatch(
            conn,
            actor=None,
            work_item_id=issue["id"],
            repo="r",
            base_commit="c",
            capability="repo.edit",
        )
    assert anonymous.value.kind == "unauthorized"
    with pytest.raises(icarus_commands.IcarusCommandError) as missing:
        icarus_commands.request_dispatch(
            conn,
            actor=actor,
            work_item_id=999999,
            repo="r",
            base_commit="c",
            capability="repo.edit",
        )
    assert missing.value.kind == "not_found"
    with pytest.raises(icarus_commands.IcarusCommandError) as unknown:
        icarus_commands.deliver_dispatch(conn, dispatch_id=999999)
    assert unknown.value.kind == "not_found"
    with pytest.raises(ValueError):
        dispatch.list_dispatches(conn, actor=actor, limit=0)
    with pytest.raises(ValueError):
        dispatch.list_dispatches(
            conn, actor=actor, work_item_id=dispatch.MAX_SQLITE_INTEGER + 1
        )
    assert (
        dispatch.get_visible_dispatch(
            conn, dispatch.MAX_SQLITE_INTEGER + 1, actor=actor
        )
        is None
    )
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
