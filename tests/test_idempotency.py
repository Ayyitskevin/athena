"""Durable Idempotency-Key single-flight and replay.

A bounded authenticated mutation first commits one ownership claim. Concurrent
exact retries wait for that owner, completed responses survive restarts, and only
the owner nonce may finalize the receipt. The suite also pins exact request
fingerprints, caller isolation, revocation and rate-limit enforcement, bounded
storage, secret-route exclusion, completed TTL, and conservative indeterminate
handling whenever Athena cannot prove whether a mutation committed.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from athena import config
from athena.core import db, idempotency
from athena.main import create_app
from athena.mentor import pages

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "B", "role": "member"}, headers=H1
    )


def _ids(client):
    return sorted(i["id"] for i in client.get("/issues", headers=H1).json())


def test_same_key_replays_one_create(tmp_path):
    with TestClient(create_app(tmp_path / "replay.db")) as client:
        _bootstrap(client)
        key = {"Idempotency-Key": "make-issue-1", **H1}
        first = client.post("/issues", json={"title": "retry me"}, headers=key)
        second = client.post("/issues", json={"title": "retry me"}, headers=key)

        assert first.status_code == 201 and second.status_code == 201
        # The retry replays the first response verbatim, flagged as a replay.
        assert second.json() == first.json()
        assert second.headers.get("idempotent-replay") == "true"
        assert first.headers.get("idempotent-replay") is None
        # Exactly ONE issue was created.
        assert _ids(client) == [first.json()["id"]]
        # The replay still carries the security headers (it's inside harden_http).
        assert second.headers.get("x-content-type-options") == "nosniff"


def test_key_reused_on_a_different_path_is_409(tmp_path):
    # WHY: an Idempotency-Key promises "this is a retry of the SAME write". If the client
    # reuses it on a DIFFERENT path, replaying the first response would silently drop this
    # second write behind a false 2xx. Refuse loudly (409) instead of replaying.
    with TestClient(create_app(tmp_path / "reuse.db")) as client:
        _bootstrap(client)
        key = {"Idempotency-Key": "shared-key", **H1}
        first = client.post("/issues", json={"title": "an issue"}, headers=key)
        assert first.status_code == 201
        # Same key, different path — the middleware refuses before the route runs.
        clash = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=key)
        assert clash.status_code == 409
        assert clash.json() == {
            "detail": "Idempotency-Key reused for a different request",
            "code": "idempotency_mismatch",
        }
        # The second write never happened; the first is intact.
        assert client.get("/spaces", headers=H1).json() == []
        assert _ids(client) == [first.json()["id"]]


def test_distinct_keys_create_distinctly(tmp_path):
    with TestClient(create_app(tmp_path / "distinct.db")) as client:
        _bootstrap(client)
        a = client.post(
            "/issues", json={"title": "a"}, headers={"Idempotency-Key": "k1", **H1}
        )
        b = client.post(
            "/issues", json={"title": "b"}, headers={"Idempotency-Key": "k2", **H1}
        )
        assert a.json()["id"] != b.json()["id"]
        assert _ids(client) == sorted([a.json()["id"], b.json()["id"]])


def test_no_header_is_normal(tmp_path):
    with TestClient(create_app(tmp_path / "nokey.db")) as client:
        _bootstrap(client)
        client.post("/issues", json={"title": "dup"}, headers=H1)
        client.post("/issues", json={"title": "dup"}, headers=H1)
        # No Idempotency-Key → two independent creates, untouched by the middleware.
        assert len(_ids(client)) == 2


def test_key_is_scoped_per_identity(tmp_path):
    with TestClient(create_app(tmp_path / "scope.db")) as client:
        _bootstrap(client)
        # The SAME key string from two different callers must not collide: each gets
        # its own create, and neither reads the other's stored response.
        mine = client.post(
            "/issues",
            json={"title": "mine"},
            headers={"Idempotency-Key": "shared", **H1},
        )
        theirs = client.post(
            "/issues",
            json={"title": "theirs"},
            headers={"Idempotency-Key": "shared", **H2},
        )
        assert mine.json()["id"] != theirs.json()["id"]
        assert theirs.headers.get("idempotent-replay") is None
        assert len(_ids(client)) == 2


def test_non_2xx_is_not_cached(tmp_path):
    with TestClient(create_app(tmp_path / "err.db")) as client:
        _bootstrap(client)
        key = {"Idempotency-Key": "first-failed", **H1}
        # A blank title is a 422 — a failed write, which must NOT be cached.
        bad = client.post("/issues", json={"title": "   "}, headers=key)
        assert bad.status_code == 422
        # Retrying the same key with a valid body actually creates (not a replay of
        # the error), because the failure was never stored.
        good = client.post("/issues", json={"title": "now valid"}, headers=key)
        assert good.status_code == 201
        assert good.headers.get("idempotent-replay") is None
        assert _ids(client) == [good.json()["id"]]


def test_unidentified_caller_is_passed_through(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "anon.db")) as client:
        _bootstrap(client)
        # With the actor header untrusted and no bearer token, there's no identity to
        # scope a key to — the request isn't deduped, it's just handled (401 here).
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        r = client.post(
            "/issues", json={"title": "x"}, headers={"Idempotency-Key": "k"}
        )
        assert r.status_code == 401


def _space_and_page(client, *, body=""):
    sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1).json()
    return client.post(
        f"/spaces/{sp['id']}/pages", json={"title": "Doc", "body": body}, headers=H1
    ).json()


def test_patch_replay_does_not_duplicate_a_page_version(tmp_path):
    # WHY: idempotency now covers PATCH, not just POST. pages.update_page snapshots a
    # version EVERY time it writes (no same-value guard), so a retried edit under the
    # same key would otherwise cut a DUPLICATE version into history — junk in a
    # load-bearing record. The retry must replay the first response and snapshot nothing.
    db_path = tmp_path / "patch.db"
    with TestClient(create_app(db_path)) as client:
        _bootstrap(client)
        pg = _space_and_page(client, body="")
        key = {"Idempotency-Key": "edit-1", **H1}
        first = client.patch(
            f"/pages/{pg['id']}", json={"body": "new text"}, headers=key
        )
        second = client.patch(
            f"/pages/{pg['id']}", json={"body": "new text"}, headers=key
        )
        assert first.status_code == 200 and second.status_code == 200
        assert second.json() == first.json()
        assert second.headers.get("idempotent-replay") == "true"
        assert first.headers.get("idempotent-replay") is None
    # Exactly ONE version was snapshotted (the pre-edit empty body), not two.
    conn = db.connect(db_path)
    assert len(pages.list_page_versions(conn, pg["id"])) == 1
    conn.close()


def test_delete_replays_success_instead_of_404(tmp_path):
    # WHY: DELETE is idempotent at the resource level, but a retried delete returns a
    # confusing 404 the second time. With the key, the retry replays the first 204 — a
    # clean success — and the page is deleted exactly once.
    with TestClient(create_app(tmp_path / "del.db")) as client:
        _bootstrap(client)
        pg = _space_and_page(client)
        key = {"Idempotency-Key": "del-1", **H1}
        first = client.delete(f"/pages/{pg['id']}", headers=key)
        second = client.delete(f"/pages/{pg['id']}", headers=key)
        assert first.status_code == 204
        assert (
            second.status_code == 204
        )  # a replay, not the 404 a real second delete gives
        assert second.headers.get("idempotent-replay") == "true"
        # Proof the replay MASKED a real 404: the same delete without the key is a 404.
        assert client.delete(f"/pages/{pg['id']}", headers=H1).status_code == 404


def test_key_reused_across_methods_is_409(tmp_path):
    # WHY: method is part of the fingerprint. The SAME key on the SAME path but a
    # DIFFERENT method is a different request — replaying the PATCH's 200 for a DELETE
    # would silently skip the delete behind a false success. Refuse loudly (409).
    with TestClient(create_app(tmp_path / "xmethod.db")) as client:
        _bootstrap(client)
        pg = _space_and_page(client)
        key = {"Idempotency-Key": "shared", **H1}
        patched = client.patch(f"/pages/{pg['id']}", json={"body": "x"}, headers=key)
        assert patched.status_code == 200
        clash = client.delete(f"/pages/{pg['id']}", headers=key)
        assert clash.status_code == 409
        assert (
            clash.json()["detail"] == "Idempotency-Key reused for a different request"
        )
        # The delete never ran — the page is still there.
        assert client.get(f"/pages/{pg['id']}").status_code == 200


def test_atomic_claim_has_one_owner_and_fenced_completion(tmp_path):
    db_path = tmp_path / "claim.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.close()
    barrier = threading.Barrier(2)

    def claim():
        local = db.connect(db_path)
        try:
            barrier.wait(timeout=2)
            return idempotency.claim_or_read(
                local,
                key="race",
                identity="actor:1",
                method="POST",
                path="/issues",
                request_fingerprint="same-fingerprint",
                lease_seconds=60,
                now=100,
            )
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result() for future in (pool.submit(claim), pool.submit(claim))
        ]

    assert sorted(result.kind for result in results) == ["in_progress", "owner"]
    owner = next(result for result in results if result.kind == "owner")
    owner_token = owner.record["owner_token"]

    conn = db.connect(db_path)
    try:
        assert (
            idempotency.complete(
                conn,
                key="race",
                identity="actor:1",
                owner_token="not-the-owner",
                status_code=201,
                content_type="application/json",
                response_headers="[]",
                response_body=b'{"id":999}',
                ttl_seconds=10,
                now=101,
            )
            == "lost"
        )
        assert (
            idempotency.complete(
                conn,
                key="race",
                identity="actor:1",
                owner_token=owner_token,
                status_code=201,
                content_type="application/json",
                response_headers='[["content-type","application/json"]]',
                response_body=b'{"id":1}',
                ttl_seconds=10,
                now=101,
            )
            == "completed"
        )
        replay = idempotency.claim_or_read(
            conn,
            key="race",
            identity="actor:1",
            method="POST",
            path="/issues",
            request_fingerprint="same-fingerprint",
            lease_seconds=60,
            now=102,
        )
        assert replay.kind == "replay"
        assert replay.record["response_body"] == b'{"id":1}'

        # Completed receipts expire, and only completed receipts expire.
        replacement = idempotency.claim_or_read(
            conn,
            key="race",
            identity="actor:1",
            method="POST",
            path="/issues",
            request_fingerprint="new-fingerprint",
            lease_seconds=60,
            now=112,
        )
        assert replacement.kind == "owner"
    finally:
        conn.close()


def test_expired_owner_is_never_taken_over_but_may_still_finalize(tmp_path):
    db_path = tmp_path / "stale.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    owner = idempotency.claim_or_read(
        conn,
        key="stale",
        identity="actor:1",
        method="POST",
        path="/issues",
        request_fingerprint="same",
        lease_seconds=10,
        now=100,
    )
    expired = idempotency.claim_or_read(
        conn,
        key="stale",
        identity="actor:1",
        method="POST",
        path="/issues",
        request_fingerprint="same",
        lease_seconds=10,
        now=111,
    )
    assert expired.kind == "indeterminate"
    record = idempotency.get_record(conn, key="stale", identity="actor:1")
    assert record["state"] == "executing"
    assert record["owner_token"] == owner.record["owner_token"]

    # A merely slow original worker remains fenced as the only possible finisher.
    assert (
        idempotency.complete(
            conn,
            key="stale",
            identity="actor:1",
            owner_token=owner.record["owner_token"],
            status_code=204,
            content_type=None,
            response_headers="[]",
            response_body=b"",
            ttl_seconds=60,
            now=112,
        )
        == "completed"
    )
    assert (
        idempotency.claim_or_read(
            conn,
            key="stale",
            identity="actor:1",
            method="POST",
            path="/issues",
            request_fingerprint="same",
            lease_seconds=10,
            now=113,
        ).kind
        == "replay"
    )
    conn.close()


def test_concurrent_same_key_executes_once_and_waiter_replays(tmp_path, monkeypatch):
    app = create_app(tmp_path / "concurrent.db", idempotency_wait_seconds=2)
    entered = threading.Event()
    release = threading.Event()
    waiter_seen = threading.Event()
    lock = threading.Lock()
    calls = {"count": 0}

    @app.post("/automation/idempotency-probe")
    def probe():
        with lock:
            calls["count"] += 1
        entered.set()
        assert release.wait(timeout=5)
        return JSONResponse(
            {"result": "once"},
            status_code=201,
            headers={
                "Location": "/automation/idempotency-probe/1",
                "ETag": '"probe-v1"',
                "Set-Cookie": "must-not-be-replayed=secret",
            },
        )

    real_claim = idempotency.claim_or_read

    def observe_claim(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        if result.kind == "in_progress":
            waiter_seen.set()
        return result

    monkeypatch.setattr(idempotency, "claim_or_read", observe_claim)

    with TestClient(app) as client:
        _bootstrap(client)
        headers = {"Idempotency-Key": "one-flight", **H1}
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner_future = pool.submit(
                client.post,
                "/automation/idempotency-probe",
                headers=headers,
            )
            assert entered.wait(timeout=2)
            waiter_future = pool.submit(
                client.post,
                "/automation/idempotency-probe",
                headers=headers,
            )
            assert waiter_seen.wait(timeout=2)
            release.set()
            owner_response = owner_future.result(timeout=5)
            waiter_response = waiter_future.result(timeout=5)

    assert owner_response.status_code == 201
    assert waiter_response.status_code == 201
    assert owner_response.json() == waiter_response.json() == {"result": "once"}
    assert owner_response.headers.get("idempotent-replay") is None
    assert waiter_response.headers["idempotent-replay"] == "true"
    assert waiter_response.headers["location"] == "/automation/idempotency-probe/1"
    assert waiter_response.headers["etag"] == '"probe-v1"'
    assert "set-cookie" in owner_response.headers
    assert "set-cookie" not in waiter_response.headers
    assert calls["count"] == 1


def test_concurrent_wait_timeout_never_runs_a_second_handler(tmp_path):
    app = create_app(tmp_path / "timeout.db", idempotency_wait_seconds=0.05)
    entered = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    @app.post("/automation/slow-probe")
    def probe():
        calls["count"] += 1
        entered.set()
        assert release.wait(timeout=5)
        return {"result": "done"}

    with TestClient(app) as client:
        _bootstrap(client)
        headers = {"Idempotency-Key": "slow", **H1}
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(client.post, "/automation/slow-probe", headers=headers)
            assert entered.wait(timeout=2)
            waiting = client.post("/automation/slow-probe", headers=headers)
            assert waiting.status_code == 409
            assert waiting.json()["code"] == "idempotency_in_progress"
            assert waiting.headers["retry-after"] == "1"
            assert calls["count"] == 1
            release.set()
            first = owner.result(timeout=5)

        replay = client.post("/automation/slow-probe", headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.headers["idempotent-replay"] == "true"
    assert calls["count"] == 1


def test_body_query_content_type_and_run_headers_are_fingerprinted(tmp_path):
    with TestClient(create_app(tmp_path / "fingerprint.db")) as client:
        _bootstrap(client)
        headers = {"Idempotency-Key": "exact-request", **H1}
        first = client.post("/issues", json={"title": "first"}, headers=headers)
        assert first.status_code == 201

        body_clash = client.post(
            "/issues", json={"title": "different"}, headers=headers
        )
        query_clash = client.post(
            "/issues?source=retry",
            content=first.request.content,
            headers={
                **headers,
                "Content-Type": "application/json",
            },
        )
        content_type_clash = client.post(
            "/issues",
            content=first.request.content,
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
        )
        run_clash = client.post(
            "/issues",
            content=first.request.content,
            headers={
                **headers,
                "Content-Type": "application/json",
                "X-Athena-Run": "another-run",
            },
        )

        for response in (
            body_clash,
            query_clash,
            content_type_clash,
            run_clash,
        ):
            assert response.status_code == 409
            assert response.json()["detail"] == (
                "Idempotency-Key reused for a different request"
            )
        assert _ids(client) == [first.json()["id"]]


def test_revoked_bearer_token_cannot_replay_completed_response(tmp_path):
    with TestClient(create_app(tmp_path / "revoked-replay.db")) as client:
        _bootstrap(client)
        created_token = client.post(
            "/tokens", json={"name": "agent", "scopes": ["admin"]}, headers=H1
        ).json()
        bearer = {"Authorization": f"Bearer {created_token['token']}"}
        keyed = {"Idempotency-Key": "before-revoke", **bearer}

        first = client.post("/issues", json={"title": "created"}, headers=keyed)
        assert first.status_code == 201
        assert (
            client.delete(f"/tokens/{created_token['id']}", headers=H1).status_code
            == 204
        )

        after = client.post("/issues", json={"title": "created"}, headers=keyed)

    assert after.status_code == 401
    assert after.json() == {"detail": "invalid or revoked token"}
    assert after.headers.get("idempotent-replay") is None


def test_keyed_bearer_request_and_replay_each_consume_rate_limit_once(tmp_path):
    app = create_app(
        tmp_path / "replay-rate.db",
        token_rate_limit_per_minute=2,
    )
    with TestClient(app) as client:
        _bootstrap(client)
        raw = client.post(
            "/tokens", json={"name": "agent", "scopes": ["admin"]}, headers=H1
        ).json()["token"]
        headers = {
            "Authorization": f"Bearer {raw}",
            "Idempotency-Key": "rate-safe",
        }

        first = client.post("/issues", json={"title": "once"}, headers=headers)
        replay = client.post("/issues", json={"title": "once"}, headers=headers)
        limited = client.post("/issues", json={"title": "once"}, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.headers["idempotent-replay"] == "true"
    assert limited.status_code == 429
    assert limited.json() == {"detail": "token rate limit exceeded"}


def test_secret_bearing_creation_routes_reject_keys_without_persisting_secret(tmp_path):
    db_path = tmp_path / "secret-routes.db"
    with TestClient(create_app(db_path)) as client:
        _bootstrap(client)
        headers = {"Idempotency-Key": "never-store-a-secret", **H1}
        responses = [
            client.post(
                "/tokens", json={"name": "unsafe", "scopes": ["admin"]}, headers=headers
            ),
            client.post(
                "/webhooks",
                json={"url": "https://example.com/events"},
                headers=headers,
            ),
            client.post("/settings/tokens", headers=headers),
            client.post("/admin/webhooks", headers=headers),
        ]

        for response in responses:
            assert response.status_code == 400
            assert "one-time secret" in response.json()["detail"]

    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] == 0
    finally:
        conn.close()


def test_actor_header_identity_is_canonicalized(tmp_path):
    with TestClient(create_app(tmp_path / "canonical-actor.db")) as client:
        _bootstrap(client)
        first = client.post(
            "/issues",
            json={"title": "one actor"},
            headers={"Idempotency-Key": "canonical", "X-Athena-Actor": "01"},
        )
        replay = client.post(
            "/issues",
            json={"title": "one actor"},
            headers={"Idempotency-Key": "canonical", "X-Athena-Actor": "1"},
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replay"] == "true"


def test_completed_replay_survives_application_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    headers = {"Idempotency-Key": "restart-safe", **H1}

    with TestClient(create_app(db_path)) as client:
        _bootstrap(client)
        first = client.post("/issues", json={"title": "survives"}, headers=headers)

    with TestClient(create_app(db_path)) as restarted:
        replay = restarted.post("/issues", json={"title": "survives"}, headers=headers)
        assert _ids(restarted) == [first.json()["id"]]

    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replay"] == "true"


def test_unhandled_owner_failure_becomes_indeterminate(tmp_path):
    db_path = tmp_path / "unknown.db"
    app = create_app(db_path)

    @app.post("/automation/explodes")
    def explodes():
        raise RuntimeError("simulated worker failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        _bootstrap(client)
        headers = {"Idempotency-Key": "unknown-outcome", **H1}
        first = client.post("/automation/explodes", headers=headers)
        retry = client.post("/automation/explodes", headers=headers)

    assert first.status_code == 500
    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_indeterminate"
    conn = db.connect(db_path)
    try:
        row = idempotency.get_record(conn, key="unknown-outcome", identity="actor:1")
        assert row["state"] == "indeterminate"
        assert row["failure_code"] == "execution_failed"
    finally:
        conn.close()


def test_keyed_oversized_body_is_rejected_before_claim(tmp_path):
    db_path = tmp_path / "bounded.db"
    app = create_app(db_path, max_request_body_bytes=256)
    with TestClient(app) as client:
        _bootstrap(client)
        response = client.post(
            "/issues",
            json={"title": "x" * 500},
            headers={"Idempotency-Key": "too-large", **H1},
        )

    assert response.status_code == 413
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()


def test_invalid_and_oversized_keys_are_rejected_without_claim(tmp_path):
    db_path = tmp_path / "key-bounds.db"
    with TestClient(create_app(db_path)) as client:
        _bootstrap(client)
        blank = client.post(
            "/issues",
            json={"title": "x"},
            headers={"Idempotency-Key": " ", **H1},
        )
        oversized = client.post(
            "/issues",
            json={"title": "x"},
            headers={"Idempotency-Key": "k" * 256, **H1},
        )

    assert blank.status_code == 400
    assert oversized.status_code == 400
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()


def test_v1_receipts_migrate_fail_closed_and_discard_cached_bytes(
    tmp_path, migration_inventory_through
):
    migration_inventory_through("0043_durable_idempotency.sql")
    db_path = tmp_path / "legacy.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute(
        "DELETE FROM schema_migrations WHERE version = '0043_durable_idempotency.sql'"
    )
    conn.execute("DROP TABLE idempotency_keys")
    for trigger in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND name LIKE 'idempotency_authz_%'"
    ).fetchall():
        conn.execute(f'DROP TRIGGER "{trigger["name"]}"')
    conn.execute("DROP TABLE idempotency_authorization_state")
    conn.execute(
        "CREATE TABLE idempotency_keys ("
        "idempotency_key TEXT NOT NULL, identity TEXT NOT NULL, "
        "method TEXT NOT NULL, path TEXT NOT NULL, "
        "status_code INTEGER NOT NULL, content_type TEXT, "
        "response_body BLOB NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (idempotency_key, identity))"
    )
    conn.execute(
        "INSERT INTO idempotency_keys "
        "(idempotency_key, identity, method, path, status_code, content_type, "
        "response_body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy",
            "actor:01",
            "POST",
            "/tokens",
            201,
            "application/json",
            b'{"token":"ath_should_not_survive"}',
        ),
    )
    conn.execute(
        "INSERT INTO idempotency_keys "
        "(idempotency_key, identity, method, path, status_code, content_type, "
        "response_body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy",
            "actor:+1",
            "POST",
            "/tokens",
            201,
            "application/json",
            b'{"token":"ath_also_must_not_survive"}',
        ),
    )
    conn.execute(
        "INSERT INTO idempotency_keys "
        "(idempotency_key, identity, method, path, status_code, content_type, "
        "response_body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy",
            "actor:1",
            "POST",
            "/tokens",
            201,
            "application/json",
            b'{"token":"ath_canonical_also_must_not_survive"}',
        ),
    )
    conn.commit()

    assert db.migrate(conn) == ["0043_durable_idempotency.sql"]
    result = idempotency.claim_or_read(
        conn,
        key="legacy",
        identity="actor:1",
        method="POST",
        path="/tokens",
        request_fingerprint="new-exact-fingerprint",
        lease_seconds=60,
    )
    assert result.kind == "indeterminate"
    row = idempotency.get_record(conn, key="legacy", identity="actor:1")
    assert row["state"] == "indeterminate"
    assert row["failure_code"] == "legacy_fingerprint_unavailable"
    assert row["response_body"] is None
    identities = conn.execute(
        "SELECT identity FROM idempotency_keys WHERE idempotency_key = 'legacy'"
    ).fetchall()
    assert [item["identity"] for item in identities] == ["actor:1"]
    conn.close()


def test_receipt_finalization_failure_never_allows_duplicate_mutation(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "finalize-failure.db"
    app = create_app(db_path)
    real_complete = idempotency.complete

    def fail_completion(*args, **kwargs):
        raise RuntimeError("simulated receipt write failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        _bootstrap(client)
        headers = {"Idempotency-Key": "finalize-once", **H1}
        monkeypatch.setattr(idempotency, "complete", fail_completion)
        first = client.post(
            "/issues", json={"title": "committed once"}, headers=headers
        )
        monkeypatch.setattr(idempotency, "complete", real_complete)

        retry = client.post(
            "/issues", json={"title": "committed once"}, headers=headers
        )
        issue_ids = _ids(client)

    assert first.status_code == 500
    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_indeterminate"
    assert len(issue_ids) == 1

    conn = db.connect(db_path)
    try:
        row = idempotency.get_record(conn, key="finalize-once", identity="actor:1")
        assert row["state"] == "indeterminate"
        assert row["failure_code"] == "finalization_failed"
    finally:
        conn.close()


def test_oversized_success_response_becomes_indeterminate(tmp_path):
    db_path = tmp_path / "response-bound.db"
    app = create_app(db_path, idempotency_max_response_bytes=8)

    @app.post("/automation/large-response")
    def large_response():
        return JSONResponse({"value": "larger than eight bytes"})

    with TestClient(app, raise_server_exceptions=False) as client:
        _bootstrap(client)
        headers = {"Idempotency-Key": "bounded-response", **H1}
        first = client.post("/automation/large-response", headers=headers)
        retry = client.post("/automation/large-response", headers=headers)

    assert first.status_code == 500
    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_indeterminate"
    conn = db.connect(db_path)
    try:
        row = idempotency.get_record(conn, key="bounded-response", identity="actor:1")
        assert row["failure_code"] == "execution_failed"
    finally:
        conn.close()


def test_key_on_non_api_endpoint_is_rejected_instead_of_silently_ignored(tmp_path):
    with TestClient(create_app(tmp_path / "unsupported.db")) as client:
        response = client.post(
            "/login",
            headers={"Idempotency-Key": "unsupported-surface"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Idempotency-Key is not supported for this endpoint"
    }


def test_membership_revocation_fences_private_response_replay(tmp_path):
    db_path = tmp_path / "authz-revision.db"
    with TestClient(create_app(db_path)) as client:
        _bootstrap(client)
        member_token = client.post(
            "/tokens", json={"name": "member-agent", "scopes": ["admin"]}, headers=H2
        ).json()["token"]
        bearer = {"Authorization": f"Bearer {member_token}"}

        project = client.post(
            "/projects",
            json={"name": "Private", "key": "PRV"},
            headers=H1,
        ).json()
        assert (
            client.put(
                f"/projects/{project['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/projects/{project['id']}/members",
                json={"user_id": 2},
                headers=H1,
            ).status_code
            == 201
        )
        issue = client.post(
            "/issues",
            json={"title": "private fact", "project_id": project["id"]},
            headers=bearer,
        ).json()

        keyed = {"Idempotency-Key": "private-edit", **bearer}
        first = client.patch(
            f"/issues/{issue['id']}",
            json={"body": "sensitive private body"},
            headers=keyed,
        )
        assert first.status_code == 200
        assert first.json()["body"] == "sensitive private body"

        assert (
            client.delete(
                f"/projects/{project['id']}/members/2", headers=H1
            ).status_code
            == 200
        )
        assert client.get(f"/issues/{issue['id']}", headers=bearer).status_code == 404

        retry = client.patch(
            f"/issues/{issue['id']}",
            json={"body": "sensitive private body"},
            headers=keyed,
        )

    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_authorization_changed"
    assert retry.headers.get("idempotent-replay") is None
    assert "sensitive private body" not in retry.text
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT state, status_code, response_headers, response_body, "
            "failure_code, expires_at FROM idempotency_keys "
            "WHERE idempotency_key = 'private-edit'"
        ).fetchone()
        assert row["state"] == "indeterminate"
        assert row["failure_code"] == "authorization_changed"
        assert row["status_code"] is None
        assert row["response_headers"] is None
        assert row["response_body"] is None
        assert row["expires_at"] is None
    finally:
        conn.close()


def test_target_expiry_is_enforced_beyond_cleanup_batch(tmp_path):
    db_path = tmp_path / "expiry-batch.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    revision = conn.execute(
        "SELECT revision FROM idempotency_authorization_state WHERE singleton = 1"
    ).fetchone()["revision"]
    conn.executemany(
        "INSERT INTO idempotency_keys ("
        "idempotency_key, identity, request_fingerprint, authorization_revision, "
        "method, path, state, status_code, response_headers, response_body, "
        "expires_at) VALUES (?, 'actor:1', 'same', ?, 'POST', '/issues', "
        "'completed', 201, '[]', X'7B7D', 100)",
        [(f"expired-{index:03d}", revision) for index in range(101)],
    )
    conn.commit()

    result = idempotency.claim_or_read(
        conn,
        key="expired-100",
        identity="actor:1",
        method="POST",
        path="/issues",
        request_fingerprint="new",
        lease_seconds=60,
        now=101,
    )

    assert result.kind == "owner"
    assert (
        conn.execute(
            "SELECT state FROM idempotency_keys WHERE idempotency_key = 'expired-100'"
        ).fetchone()["state"]
        == "executing"
    )
    conn.close()


def test_cookie_session_secret_forms_reject_keys_before_mutating(tmp_path):
    db_path = tmp_path / "session-secret.db"
    with TestClient(create_app(db_path)) as client:
        created = client.post(
            "/users",
            json={
                "email": "admin@example.com",
                "name": "Admin",
                "password": "correct horse",
            },
        )
        assert created.status_code == 201
        login = client.post(
            "/login",
            data={"email": "admin@example.com", "password": "correct horse"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(config.CSRF_COOKIE)
        headers = {
            "Idempotency-Key": "session-secret",
            "X-CSRF-Token": csrf,
        }

        token_response = client.post(
            "/settings/tokens",
            data={"name": "must-not-exist", "scope_admin": "1"},
            headers=headers,
        )
        webhook_response = client.post(
            "/admin/webhooks",
            data={"url": "https://example.com/events"},
            headers=headers,
        )

    for response in (token_response, webhook_response):
        assert response.status_code == 400
        assert "one-time secret" in response.json()["detail"]
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] == 0
    finally:
        conn.close()


def test_first_user_bootstrap_cannot_silently_ignore_key(tmp_path):
    db_path = tmp_path / "anonymous-key.db"
    with TestClient(create_app(db_path)) as client:
        response = client.post(
            "/users",
            json={"email": "admin@example.com", "name": "Admin"},
            headers={"Idempotency-Key": "bootstrap"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        conn.close()


def test_sprint_mutation_supports_durable_replay(tmp_path):
    with TestClient(create_app(tmp_path / "sprint-replay.db")) as client:
        _bootstrap(client)
        project = client.post(
            "/projects", json={"name": "Ops", "key": "OPS"}, headers=H1
        ).json()
        sprint = client.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "Original"},
            headers=H1,
        ).json()
        headers = {"Idempotency-Key": "rename-sprint", **H1}

        first = client.patch(
            f"/sprints/{sprint['id']}",
            json={"name": "Renamed"},
            headers=headers,
        )
        replay = client.patch(
            f"/sprints/{sprint['id']}",
            json={"name": "Renamed"},
            headers=headers,
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replay"] == "true"


def test_role_downgrade_fences_admin_response_replay(tmp_path):
    with TestClient(create_app(tmp_path / "role-fence.db")) as client:
        _bootstrap(client)
        third_admin = client.post(
            "/users",
            json={"email": "other-admin@example.com", "name": "Other", "role": "admin"},
            headers=H1,
        ).json()
        admin_token = client.post(
            "/tokens", json={"name": "admin-agent", "scopes": ["admin"]}, headers=H1
        ).json()["token"]
        bearer = {"Authorization": f"Bearer {admin_token}"}
        webhook = client.post(
            "/webhooks",
            json={"url": "https://93.184.216.34/events"},
            headers=H1,
        ).json()
        keyed = {"Idempotency-Key": "admin-webhook-edit", **bearer}

        first = client.patch(
            f"/webhooks/{webhook['id']}",
            json={"active": False},
            headers=keyed,
        )
        assert first.status_code == 200
        assert (
            client.put(
                "/users/1/role",
                json={"role": "member"},
                headers={"X-Athena-Actor": str(third_admin["id"])},
            ).status_code
            == 200
        )

        normal = client.patch(
            f"/webhooks/{webhook['id']}",
            json={"active": False},
            headers=bearer,
        )
        retry = client.patch(
            f"/webhooks/{webhook['id']}",
            json={"active": False},
            headers=keyed,
        )

    assert normal.status_code == 403
    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_authorization_changed"
    assert "example.com" not in retry.text


def test_private_project_move_fences_old_issue_response(tmp_path):
    with TestClient(create_app(tmp_path / "move-fence.db")) as client:
        _bootstrap(client)
        member_token = client.post(
            "/tokens", json={"name": "member", "scopes": ["admin"]}, headers=H2
        ).json()["token"]
        bearer = {"Authorization": f"Bearer {member_token}"}
        source = client.post(
            "/projects", json={"name": "Source", "key": "SRC"}, headers=H1
        ).json()
        destination = client.post(
            "/projects", json={"name": "Destination", "key": "DST"}, headers=H1
        ).json()
        for project in (source, destination):
            assert (
                client.put(
                    f"/projects/{project['id']}/visibility",
                    json={"visibility": "private"},
                    headers=H1,
                ).status_code
                == 200
            )
        assert (
            client.post(
                f"/projects/{source['id']}/members",
                json={"user_id": 2},
                headers=H1,
            ).status_code
            == 201
        )
        issue = client.post(
            "/issues",
            json={"title": "source-only fact", "project_id": source["id"]},
            headers=H1,
        ).json()
        assert (
            client.put(
                f"/issues/{issue['id']}/assignee",
                json={"assignee_id": 2},
                headers=H1,
            ).status_code
            == 200
        )
        keyed = {"Idempotency-Key": "source-edit", **bearer}

        first = client.patch(
            f"/issues/{issue['id']}",
            json={"body": "source private body"},
            headers=keyed,
        )
        assert first.status_code == 200
        assert (
            client.patch(
                f"/issues/{issue['id']}",
                json={"project_id": destination["id"]},
                headers=H1,
            ).status_code
            == 200
        )

        assert client.get(f"/issues/{issue['id']}", headers=bearer).status_code == 404
        retry = client.patch(
            f"/issues/{issue['id']}",
            json={"body": "source private body"},
            headers=keyed,
        )

    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_authorization_changed"
    assert "source private body" not in retry.text


def test_assignee_handoff_succeeds_once_then_fences_replay(tmp_path):
    with TestClient(create_app(tmp_path / "assignee-fence.db")) as client:
        _bootstrap(client)
        member_token = client.post(
            "/tokens", json={"name": "assignee", "scopes": ["admin"]}, headers=H2
        ).json()["token"]
        bearer = {"Authorization": f"Bearer {member_token}"}
        issue = client.post("/issues", json={"title": "handoff"}, headers=H1).json()
        assert (
            client.put(
                f"/issues/{issue['id']}/assignee",
                json={"assignee_id": 2},
                headers=H1,
            ).status_code
            == 200
        )
        keyed = {"Idempotency-Key": "handoff-once", **bearer}

        first = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": 1},
            headers=keyed,
        )
        normal = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": 1},
            headers=bearer,
        )
        retry = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": 1},
            headers=keyed,
        )

    assert first.status_code == 200
    assert first.json()["assignee_id"] == 1
    assert normal.status_code == 403
    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_authorization_changed"


async def _raw_streamed_keyed_request(app, body: bytes) -> tuple[int, bytes]:
    messages = []
    chunks = [body[: len(body) // 2], body[len(body) // 2 :]]
    receive_index = 0

    async def receive():
        nonlocal receive_index
        if receive_index < len(chunks):
            index = receive_index
            receive_index += 1
            return {
                "type": "http.request",
                "body": chunks[index],
                "more_body": receive_index < len(chunks),
            }
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "path": "/issues",
            "raw_path": b"/issues",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"host", b"testserver"),
                (b"idempotency-key", b"streamed-too-large"),
                (b"x-athena-actor", b"1"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, response_body


def test_streamed_keyed_body_is_bounded_before_claim(tmp_path):
    db_path = tmp_path / "streamed-keyed-limit.db"
    app = create_app(db_path, max_request_body_bytes=128)
    body = json.dumps({"title": "x" * 300}).encode()

    with TestClient(app) as client:
        _bootstrap(client)
        status, response_body = asyncio.run(_raw_streamed_keyed_request(app, body))

    assert status == 413
    assert json.loads(response_body) == {"detail": "request body too large"}
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()


def test_authorization_change_does_not_steal_live_owner(tmp_path):
    db_path = tmp_path / "live-owner-authz.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    owner = idempotency.claim_or_read(
        conn,
        key="live-owner",
        identity="actor:1",
        method="PATCH",
        path="/issues/1",
        request_fingerprint="same",
        lease_seconds=60,
        now=100,
    )
    conn.execute(
        "UPDATE idempotency_authorization_state SET revision = revision + 1 "
        "WHERE singleton = 1"
    )
    conn.commit()

    waiter = idempotency.claim_or_read(
        conn,
        key="live-owner",
        identity="actor:1",
        method="PATCH",
        path="/issues/1",
        request_fingerprint="same",
        lease_seconds=60,
        now=101,
    )
    assert waiter.kind == "authorization_changed"
    still_owned = idempotency.get_record(conn, key="live-owner", identity="actor:1")
    assert still_owned["state"] == "executing"
    assert still_owned["owner_token"] == owner.record["owner_token"]

    completion = idempotency.complete(
        conn,
        key="live-owner",
        identity="actor:1",
        owner_token=owner.record["owner_token"],
        status_code=200,
        content_type="application/json",
        response_headers='[["content-type","application/json"]]',
        response_body=b'{"private":"body"}',
        ttl_seconds=60,
        now=102,
    )
    assert completion == "authorization_changed"
    fenced = idempotency.get_record(conn, key="live-owner", identity="actor:1")
    assert fenced["state"] == "indeterminate"
    assert fenced["failure_code"] == "authorization_changed"
    assert fenced["response_body"] is None
    conn.close()


def test_revoked_token_on_users_keeps_normal_401_after_bootstrap(tmp_path):
    db_path = tmp_path / "revoked-users.db"
    with TestClient(create_app(db_path)) as client:
        _bootstrap(client)
        created_token = client.post(
            "/tokens", json={"name": "revoked", "scopes": ["admin"]}, headers=H1
        ).json()
        assert (
            client.delete(f"/tokens/{created_token['id']}", headers=H1).status_code
            == 204
        )
        invalid_bearer = {"Authorization": f"Bearer {created_token['token']}"}
        normal = client.post(
            "/users",
            json={"email": "blocked@example.com", "name": "Blocked"},
            headers=invalid_bearer,
        )
        response = client.post(
            "/users",
            json={"email": "blocked@example.com", "name": "Blocked"},
            headers={
                **invalid_bearer,
                "Idempotency-Key": "revoked-user-create",
            },
        )

    assert response.status_code == normal.status_code == 401
    assert response.json() == normal.json()
    conn = db.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE email = 'blocked@example.com'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM idempotency_keys "
                "WHERE idempotency_key = 'revoked-user-create'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()
