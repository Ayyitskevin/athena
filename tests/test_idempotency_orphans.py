"""A key left executing by a process that is gone, and what an admin can do
about a key whose outcome is unknown (H14).

WHY (OPS-6): after a hard kill, a keyed write in flight answered
``idempotency_in_progress`` for about a minute with no owner, then
``idempotency_indeterminate`` for ever. No route or tool listed such keys or
settled one.

Now a process that starts serving a database no other process is serving
marks every executing key indeterminate at once. An admin lists the keys
whose outcome is unknown, with the request each belongs to and who sent it,
and resolves one as applied or not applied; the trail records the decision.
"""

from __future__ import annotations

import logging
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from support import ADMIN, as_user, bearer, bootstrap, events, make_app, onboard_agent

from athena import background
from athena.core import db, idempotency, idempotency_commands
from athena.main import create_app


def _claim(
    db_file, key: str, identity: str, *, path: str = "/labels", lease=60
) -> None:
    """A keyed request that began and never finished: its key is executing."""
    conn = db.connect(db_file)
    try:
        claimed = idempotency.claim_or_read(
            conn,
            key=key,
            identity=identity,
            method="POST",
            path=path,
            request_fingerprint="f",
            lease_seconds=lease,
        )
        assert claimed.kind == "owner"
    finally:
        conn.close()


def _state(db_file, key: str, identity: str) -> tuple[str, str | None] | None:
    conn = db.connect(db_file)
    try:
        record = idempotency.get_record(conn, key=key, identity=identity)
    finally:
        conn.close()
    return None if record is None else (record["state"], record["failure_code"])


@pytest.fixture
def instance(tmp_path):
    """A stopped instance with an admin (1) and an agent with a token."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, scopes=("read", "issue:write"))
    return db_file, agent


def _token_identity(agent) -> str:
    return idempotency.token_identity(agent["token"]["token"])


# --- at start ---------------------------------------------------------------------


def test_a_lone_process_orphans_executing_keys_at_start_and_says_so(instance, caplog):
    db_file, agent = instance
    identity = _token_identity(agent)
    _claim(db_file, "mid-flight", identity)
    caplog.set_level(logging.INFO, logger="athena")

    with TestClient(create_app(db_file)) as client:
        assert _state(db_file, "mid-flight", identity) == (
            "indeterminate",
            "owner_lost",
        )
        # The retry is told at once, where it waited out the claim's lease.
        retry = client.post(
            "/labels",
            json={"name": "x"},
            headers={**bearer(agent), "Idempotency-Key": "mid-flight"},
        )
        assert (retry.status_code, retry.json()["code"]) == (
            409,
            "idempotency_indeterminate",
        )
        read = client.get("/idempotency/mid-flight", headers=bearer(agent)).json()
        assert read["state"] == "indeterminate"

    said = [r.getMessage() for r in caplog.records if "idempotency:" in r.getMessage()]
    assert said == [
        "idempotency: 1 key(s) left executing by a process that no longer exists "
        "now answer idempotency_indeterminate; an admin lists them with "
        "GET /idempotency-keys/indeterminate"
    ]


def test_a_start_with_nothing_orphaned_says_nothing(instance, caplog):
    db_file, _agent = instance
    caplog.set_level(logging.INFO, logger="athena")

    with TestClient(create_app(db_file)):
        pass

    assert not [r for r in caplog.records if "idempotency:" in r.getMessage()]


def test_a_start_on_a_schema_with_no_key_table_marks_nothing(tmp_path, monkeypatch):
    # WHY: startup runs the orphan step on whatever schema the migrations gave
    # it. The first version assumed Athena's own tables and failed the start of
    # any app built on another migration set (the migration integrity tests).
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_only.sql").write_text(
        "CREATE TABLE only_this (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    with TestClient(create_app(tmp_path / "bare.db")) as client:
        assert client.get("/healthz").status_code == 200

    conn = db.connect(tmp_path / "bare.db")
    try:
        assert idempotency_commands.orphan_executing(conn) == 0
    finally:
        conn.close()


def test_a_second_process_assumes_nothing_about_the_firsts_keys(instance):
    db_file, agent = instance
    identity = _token_identity(agent)

    with TestClient(create_app(db_file)):
        _claim(db_file, "the-firsts", identity)  # in flight in the first process
        with TestClient(create_app(db_file)):
            assert _state(db_file, "the-firsts", identity) == ("executing", None)
        assert _state(db_file, "the-firsts", identity) == ("executing", None)

    # Both are gone: the next to start is alone again.
    with TestClient(create_app(db_file)):
        assert _state(db_file, "the-firsts", identity) == (
            "indeterminate",
            "owner_lost",
        )


def test_completed_and_resolved_keys_are_left_alone_at_start(instance):
    db_file, agent = instance
    with TestClient(create_app(db_file)) as client:
        made = client.post(
            "/labels",
            json={"name": "kept"},
            headers={**bearer(agent), "Idempotency-Key": "done"},
        )
        assert made.status_code == 201, made.text

    with TestClient(create_app(db_file)) as client:
        again = client.post(
            "/labels",
            json={"name": "kept"},
            headers={**bearer(agent), "Idempotency-Key": "done"},
        )
        assert again.status_code == 201
        assert again.json() == made.json()


def test_the_serving_lock_is_private_and_a_lock_that_cannot_be_held_claims_nothing(
    tmp_path,
):
    db_file = tmp_path / "athena.db"
    descriptor, alone = background.join_serving(db_file)
    assert alone and descriptor is not None
    lock = Path(f"{db_file}.serving.lock")
    assert background.serving_lock_path(db_file) == lock
    assert lock.stat().st_mode & 0o777 == 0o600
    # While it is still exclusive, a second process waits; once shared, it joins.
    background.keep_serving(descriptor)
    second, second_alone = background.join_serving(db_file)
    assert second is not None and not second_alone
    import os

    os.close(second)
    os.close(descriptor)

    blocked = tmp_path / "blocked.db"
    Path(f"{blocked}.serving.lock").mkdir()
    assert background.join_serving(blocked) == (None, False)
    linked = tmp_path / "linked.db"
    Path(f"{linked}.serving.lock").symlink_to(tmp_path / "elsewhere")
    assert background.join_serving(linked) == (None, False)
    assert not (tmp_path / "elsewhere").exists()


def test_a_process_that_cannot_hold_the_lock_still_serves_and_orphans_nothing(
    instance,
):
    db_file, agent = instance
    identity = _token_identity(agent)
    _claim(db_file, "mid-flight", identity)
    lock = Path(f"{db_file}.serving.lock")
    lock.unlink()
    lock.mkdir()

    with TestClient(create_app(db_file)) as client:
        assert client.get("/readyz").status_code == 200
        assert _state(db_file, "mid-flight", identity) == ("executing", None)


# --- the list ---------------------------------------------------------------------


def test_the_list_names_each_unknown_outcome_its_request_and_its_sender(instance):
    db_file, agent = instance
    _claim(db_file, "by-token", _token_identity(agent), path="/issues/7/comments")
    _claim(db_file, "by-header", idempotency.actor_identity(1))
    _claim(db_file, "by-nobody-now", "tok:" + "0" * 64)

    with TestClient(create_app(db_file)) as client:
        # Claimed after the start, by this process: in flight, not listed.
        _claim(db_file, "live", idempotency.actor_identity(1))
        # Its lease ran out with no answer recorded: listed.
        _claim(db_file, "lapsed", idempotency.actor_identity(1), lease=1)
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE idempotency_keys SET lease_expires_at = ? "
            "WHERE idempotency_key = 'lapsed'",
            (int(time.time()) - 5,),
        )
        conn.commit()
        conn.close()

        listed = client.get("/idempotency-keys/indeterminate", headers=ADMIN)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert (body["has_more"], body["next_cursor"]) == (False, None)
    by_key = {item["key"]: item for item in body["items"]}
    assert list(by_key) == ["by-token", "by-header", "by-nobody-now", "lapsed"]
    token = by_key["by-token"]
    assert {k: token[k] for k in ("method", "path", "issue_id", "reason")} == {
        "method": "POST",
        "path": "/issues/7/comments",
        "issue_id": 7,
        "reason": "owner_lost",
    }
    assert token["sent_by"] == {
        "user_id": agent["user"]["id"],
        "user_name": agent["user"]["name"],
        "token_id": agent["token"]["id"],
        "token_name": agent["token"]["name"],
    }
    assert by_key["by-header"]["sent_by"] == {
        "user_id": 1,
        "user_name": "Ann",
        "token_id": None,
        "token_name": None,
    }
    assert by_key["by-nobody-now"]["sent_by"] is None
    assert by_key["lapsed"]["reason"] == "lease_expired"
    for item in body["items"]:
        assert item["since"].endswith("Z") and item["claimed_at"].endswith("Z")
        assert isinstance(item["id"], int)


def test_only_an_admin_reads_the_list_or_resolves(instance):
    db_file, agent = instance
    _claim(db_file, "mid-flight", _token_identity(agent))

    with TestClient(create_app(db_file)) as client:
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "Mo", "password": "pw"},
            headers=ADMIN,
        ).json()
        for headers in (bearer(agent), as_user(member["id"])):
            listed = client.get("/idempotency-keys/indeterminate", headers=headers)
            resolved = client.post(
                "/idempotency-keys/1/resolve",
                json={"key": "mid-flight", "outcome": "not_applied"},
                headers=headers,
            )
            assert (listed.status_code, resolved.status_code) == (403, 403)
        assert client.get("/idempotency-keys/indeterminate").status_code == 401

    assert _state(db_file, "mid-flight", _token_identity(agent)) is not None


# --- resolving --------------------------------------------------------------------


def _listed(client) -> dict[str, dict]:
    body = client.get("/idempotency-keys/indeterminate", headers=ADMIN).json()
    return {item["key"]: item for item in body["items"]}


def test_not_applied_forgets_the_key_and_the_same_request_then_runs(instance):
    db_file, agent = instance
    _claim(db_file, "retry-me", _token_identity(agent))

    with TestClient(create_app(db_file)) as client:
        item = _listed(client)["retry-me"]
        resolved = client.post(
            f"/idempotency-keys/{item['id']}/resolve",
            json={"key": "retry-me", "outcome": "not_applied"},
            headers=ADMIN,
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json() == {
            "id": item["id"],
            "key": "retry-me",
            "method": "POST",
            "path": "/labels",
            "outcome": "not_applied",
            "event_id": resolved.json()["event_id"],
        }
        assert _listed(client) == {}
        assert (
            client.get("/idempotency/retry-me", headers=bearer(agent)).json()["state"]
            == "unused"
        )
        ran = client.post(
            "/labels",
            json={"name": "now-made"},
            headers={**bearer(agent), "Idempotency-Key": "retry-me"},
        )
        assert ran.status_code == 201, ran.text

    (event,) = events(db_file, "resolved_idempotency_key")
    assert event["id"] == resolved.json()["event_id"]
    assert (event["actor_id"], event["target_kind"], event["target_id"]) == (
        1,
        "user",
        agent["user"]["id"],
    )
    assert event["detail"] == "not_applied: POST /labels (key retry-me)"


def test_applied_keeps_the_key_refusing_and_says_it_was_applied(instance):
    db_file, agent = instance
    _claim(db_file, "landed", _token_identity(agent))

    with TestClient(create_app(db_file)) as client:
        item = _listed(client)["landed"]
        resolved = client.post(
            f"/idempotency-keys/{item['id']}/resolve",
            json={"key": "landed", "outcome": "applied"},
            headers=ADMIN,
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["outcome"] == "applied"
        assert _listed(client) == {}

        retry = client.post(
            "/labels",
            json={"name": "x"},
            headers={**bearer(agent), "Idempotency-Key": "landed"},
        )
        assert retry.status_code == 409
        assert retry.json() == {
            "detail": (
                "An operator confirmed the earlier request under this "
                "Idempotency-Key was applied; do not send it again"
            ),
            "code": "idempotency_applied",
        }
        read = client.get("/idempotency/landed", headers=bearer(agent)).json()
        assert read["state"] == "applied"
        assert read["retry"].startswith("refused 409 idempotency_applied")
        # Nothing ran.
        assert client.get("/labels", headers=ADMIN).json() == []

    # It is still applied after a restart: a lone start orphans executing keys only.
    with TestClient(create_app(db_file)):
        assert _state(db_file, "landed", _token_identity(agent)) == (
            "indeterminate",
            "resolved_applied",
        )
    (event,) = events(db_file, "resolved_idempotency_key")
    assert event["detail"] == "applied: POST /labels (key landed)"


def test_a_key_is_resolved_once_and_only_by_its_id_and_text_together(instance):
    db_file, agent = instance
    _claim(db_file, "one", _token_identity(agent))
    _claim(db_file, "two", _token_identity(agent))

    with TestClient(create_app(db_file)) as client:
        listed = _listed(client)

        def resolve(key_id, key, outcome="applied"):
            return client.post(
                f"/idempotency-keys/{key_id}/resolve",
                json={"key": key, "outcome": outcome},
                headers=ADMIN,
            )

        # The id of one key with the text of another names neither.
        crossed = resolve(listed["one"]["id"], "two")
        assert (crossed.status_code, crossed.json()["code"]) == (404, "not_found")
        assert resolve(99_999, "one").status_code == 404
        assert set(_listed(client)) == {"one", "two"}

        assert resolve(listed["one"]["id"], "one").status_code == 200
        again = resolve(listed["one"]["id"], "one", "not_applied")
        assert again.status_code == 404
        assert set(_listed(client)) == {"two"}

        assert resolve(listed["two"]["id"], "two", "maybe").status_code == 422
        extra = client.post(
            f"/idempotency-keys/{listed['two']['id']}/resolve",
            json={"key": "two", "outcome": "applied", "note": "x"},
            headers=ADMIN,
        )
        assert extra.status_code == 422
        assert set(_listed(client)) == {"two"}

    assert len(events(db_file, "resolved_idempotency_key")) == 1


def test_a_key_fenced_by_an_authorization_change_is_not_an_unknown_outcome(instance):
    db_file, agent = instance
    conn = db.connect(db_file)
    try:
        with pytest.raises(ValueError, match="outcome must be one of"):
            idempotency_commands.resolve(
                conn, actor={"id": 1}, row_id=1, key="k", outcome="maybe"
            )
    finally:
        conn.close()

    with TestClient(create_app(db_file)) as client:
        made = client.post(
            "/labels",
            json={"name": "kept"},
            headers={**bearer(agent), "Idempotency-Key": "fenced"},
        )
        assert made.status_code == 201
        # A role change anywhere fences every stored receipt.
        client.put("/users/1/role", json={"role": "admin"}, headers=ADMIN)
        client.post(
            "/users",
            json={"email": "n@e.com", "name": "N", "password": "pw"},
            headers=ADMIN,
        )
        client.put("/users/3/role", json={"role": "viewer"}, headers=ADMIN)
        retry = client.post(
            "/labels",
            json={"name": "kept"},
            headers={**bearer(agent), "Idempotency-Key": "fenced"},
        )
        assert retry.json()["code"] == "idempotency_authorization_changed"
        assert _listed(client) == {}


def test_the_list_pages_by_cursor_and_refuses_one_it_did_not_hand_out(instance):
    db_file, agent = instance
    for number in range(5):
        _claim(db_file, f"key-{number}", _token_identity(agent))

    with TestClient(create_app(db_file)) as client:

        def page(**params):
            return client.get(
                "/idempotency-keys/indeterminate", params=params, headers=ADMIN
            )

        seen, cursor = [], None
        while True:
            params = {"limit": 2} if cursor is None else {"limit": 2, "cursor": cursor}
            body = page(**params).json()
            seen.append([item["key"] for item in body["items"]])
            assert body["has_more"] is (body["next_cursor"] is not None)
            if not body["has_more"]:
                break
            cursor = body["next_cursor"]
        assert seen == [["key-0", "key-1"], ["key-2", "key-3"], ["key-4"]]

        # A key resolved between two pages shifts nothing after it.
        first = page(limit=2).json()
        client.post(
            f"/idempotency-keys/{first['items'][0]['id']}/resolve",
            json={"key": "key-0", "outcome": "not_applied"},
            headers=ADMIN,
        )
        second = page(limit=2, cursor=first["next_cursor"]).json()
        assert [item["key"] for item in second["items"]] == ["key-2", "key-3"]

        for bad in ("nonsense", first["next_cursor"][:-2] + "zz"):
            refused = page(cursor=bad)
            assert (refused.status_code, refused.json()["code"]) == (
                422,
                "validation_error",
            )
        assert page(limit=0).status_code == 422
        assert page(limit=201).status_code == 422
        assert len(page().json()["items"]) == 4


def test_an_admin_lists_and_resolves_through_the_tools_and_a_seat_has_neither(
    instance,
):
    import asyncio
    import json

    from athena.mcp import server as mcp_server
    from athena.mcp.client import AthenaClient

    db_file, agent = instance
    _claim(db_file, "by-tool", _token_identity(agent), path="/issues/3/comments")
    app = create_app(db_file)

    def call(server, name, arguments):
        content, _ = asyncio.run(server.call_tool(name, arguments))
        return json.loads(content[0].text)

    with TestClient(app) as client:
        admin_token = client.post(
            "/tokens", json={"name": "ops", "scopes": ["read", "admin"]}, headers=ADMIN
        ).json()
        api = AthenaClient(client=TestClient(app, headers=bearer(admin_token)))
        server = mcp_server.build_server(api)

        listed = call(server, "list_indeterminate_keys", {})
        (row,) = listed["items"]
        assert (row["key"], row["path"], row["issue_id"]) == (
            "by-tool",
            "/issues/3/comments",
            3,
        )
        resolved = call(
            server,
            "resolve_idempotency_key",
            {
                "key_id": row["id"],
                "key": "by-tool",
                "outcome": "applied",
                "idempotency_key": "settle-1",
            },
        )
        assert resolved["outcome"] == "applied"
        # The resolution is itself keyed: its retry replays, where a second
        # resolution would find nothing waiting.
        again = call(
            server,
            "resolve_idempotency_key",
            {
                "key_id": row["id"],
                "key": "by-tool",
                "outcome": "applied",
                "idempotency_key": "settle-1",
            },
        )
        assert again["event_id"] == resolved["event_id"]
        assert again["_replayed"] is True
        assert call(server, "list_indeterminate_keys", {})["items"] == []

        seat = mcp_server.build_server(api, profile=mcp_server.PROFILES["conductor"])
        names = {tool.name for tool in asyncio.run(seat.list_tools())}
        assert not names & {"list_indeterminate_keys", "resolve_idempotency_key"}

    assert len(events(db_file, "resolved_idempotency_key")) == 1


def test_the_doctor_counts_the_serving_lock_as_athenas_own(instance):
    from athena import ops

    db_file, _agent = instance
    assert Path(f"{db_file}.serving.lock").exists()
    assert f"{db_file.name}.serving.lock" in ops._athena_entries(db_file, None)
