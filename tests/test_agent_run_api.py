"""REST contract for cooperative, server-timed agent-run heartbeats."""

from fastapi.testclient import TestClient

from athena import config
from athena.core import agent_run_commands, db
from athena.main import create_app

_ADMIN_HEADER = {"X-Athena-Actor": "1"}
_SAFE_HEARTBEAT_KEYS = {
    "agent_id",
    "run_id",
    "first_seen_at",
    "last_seen_at",
    "age_seconds",
    "reporting_state",
}
_SAFE_CHECKIN_KEYS = {
    "agent_id",
    "agent_name",
    "agent_email",
    "run_id",
    "first_seen_at",
    "last_seen_at",
    "age_seconds",
    "reporting_state",
}


def _bootstrap_admin(client: TestClient) -> dict:
    response = client.post("/users", json={"email": "admin@e.com", "name": "Admin"})
    assert response.status_code == 201, response.text
    return response.json()


def _create_user(
    client: TestClient,
    *,
    email: str,
    name: str,
    is_agent: bool,
) -> dict:
    response = client.post(
        "/users",
        json={"email": email, "name": name, "is_agent": is_agent},
        headers=_ADMIN_HEADER,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mint(
    client: TestClient,
    *,
    actor_id: int,
    scopes: list[str],
    name: str,
) -> str:
    response = client.post(
        "/tokens",
        json={"name": name, "scopes": scopes},
        headers={"X-Athena-Actor": str(actor_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_heartbeat_requires_agent_bearer_with_write_scope(tmp_path):
    app = create_app(tmp_path / "heartbeat-auth.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(
            client,
            email="bot@e.com",
            name="Bot",
            is_agent=True,
        )
        human = _create_user(
            client,
            email="human@e.com",
            name="Human",
            is_agent=False,
        )
        agent_reader = _mint(
            client,
            actor_id=agent["id"],
            scopes=["read"],
            name="agent-reader",
        )
        agent_writer = _mint(
            client,
            actor_id=agent["id"],
            scopes=["issue:write"],
            name="agent-writer",
        )
        human_writer = _mint(
            client,
            actor_id=human["id"],
            scopes=["issue:write"],
            name="human-writer",
        )

        anonymous = client.put("/agent-runs/heartbeat", json={"run_id": "run-1"})
        assert anonymous.status_code == 401

        trusted_header = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-1"},
            headers={"X-Athena-Actor": str(agent["id"])},
        )
        assert trusted_header.status_code == 403
        assert trusted_header.json()["detail"] == "bearer token required"

        trusted_human_header = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-1"},
            headers={"X-Athena-Actor": str(human["id"])},
        )
        assert trusted_human_header.status_code == 403
        assert trusted_human_header.json()["detail"] == "bearer token required"

        read_only = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-1"},
            headers=_bearer(agent_reader),
        )
        assert read_only.status_code == 403

        human_response = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-1"},
            headers=_bearer(human_writer),
        )
        assert human_response.status_code == 403
        assert human_response.json()["detail"] == "agent account required"

        accepted = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "  run-1  "},
            headers=_bearer(agent_writer),
        )
        assert accepted.status_code == 200, accepted.text
        payload = accepted.json()
        assert set(payload) == _SAFE_HEARTBEAT_KEYS
        assert payload["agent_id"] == agent["id"]
        assert payload["run_id"] == "run-1"
        assert payload["age_seconds"] >= 0
        assert payload["reporting_state"] == "reporting_recently"


def test_heartbeat_body_accepts_only_a_strict_run_id(tmp_path):
    app = create_app(tmp_path / "heartbeat-body.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(
            client,
            email="bot@e.com",
            name="Bot",
            is_agent=True,
        )
        token = _mint(
            client,
            actor_id=agent["id"],
            scopes=["issue:write"],
            name="writer",
        )
        headers = _bearer(token)

        for run_id in (
            "",
            "   ",
            "x" * 201,
            "line\nbreak",
            "trailing\n",
            "delete\x7fme",
            "bidi-\u202espoof",
            "zero-\u200bwidth",
        ):
            response = client.put(
                "/agent-runs/heartbeat",
                json={"run_id": run_id},
                headers=headers,
            )
            assert response.status_code == 422, (run_id, response.text)

        # httpx correctly refuses to encode a lone surrogate passed via ``json``;
        # send its legal JSON escape form to exercise the server's parser boundary.
        surrogate = client.put(
            "/agent-runs/heartbeat",
            content=b'{"run_id":"\\ud800"}',
            headers={**headers, "Content-Type": "application/json"},
        )
        assert surrogate.status_code == 422, surrogate.text

        assert (
            client.put("/agent-runs/heartbeat", json={}, headers=headers).status_code
            == 422
        )
        for forbidden_field, value in (
            ("agent_id", agent["id"]),
            ("actor", {"id": agent["id"]}),
            ("reported_at", "2099-01-01 00:00:00"),
            ("last_seen_at", "2099-01-01 00:00:00"),
        ):
            response = client.put(
                "/agent-runs/heartbeat",
                json={"run_id": "safe-run", forbidden_field: value},
                headers=headers,
            )
            assert response.status_code == 422, (forbidden_field, response.text)


def test_heartbeat_returns_conflict_when_agent_capacity_is_full(tmp_path, monkeypatch):
    app = create_app(tmp_path / "heartbeat-capacity.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(
            client,
            email="bot@e.com",
            name="Bot",
            is_agent=True,
        )
        token = _mint(
            client,
            actor_id=agent["id"],
            scopes=["issue:write"],
            name="writer",
        )
        monkeypatch.setattr(config, "AGENT_RUN_MAX_CHECKINS_PER_AGENT", 1)
        headers = _bearer(token)
        assert (
            client.put(
                "/agent-runs/heartbeat", json={"run_id": "run-1"}, headers=headers
            ).status_code
            == 200
        )

        full = client.put(
            "/agent-runs/heartbeat", json={"run_id": "run-2"}, headers=headers
        )
        assert full.status_code == 409
        assert full.json()["detail"] == (
            "agent run check-in capacity reached; refresh an existing run_id"
        )
        assert (
            client.put(
                "/agent-runs/heartbeat", json={"run_id": "run-1"}, headers=headers
            ).status_code
            == 200
        )


def test_repeated_heartbeat_puts_are_not_durable_idempotency_replays(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path / "heartbeat-repeat.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(
            client,
            email="bot@e.com",
            name="Bot",
            is_agent=True,
        )
        token = _mint(
            client,
            actor_id=agent["id"],
            scopes=["issue:write"],
            name="writer",
        )
        calls: list[tuple[int, str]] = []

        def record_heartbeat(conn, *, actor, run_id):
            del conn
            calls.append((actor["id"], run_id))
            return {
                "agent_id": actor["id"],
                "run_id": run_id,
                "first_seen_at": "2026-07-13 00:00:00",
                "last_seen_at": "2026-07-13 00:00:00",
                "age_seconds": 0,
                "reporting_state": "reporting_recently",
            }

        monkeypatch.setattr(agent_run_commands, "heartbeat", record_heartbeat)
        bearer = _bearer(token)
        rejected = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-repeat"},
            headers={**bearer, "Idempotency-Key": "unsupported-heartbeat-key"},
        )
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == (
            "Idempotency-Key is not supported for this endpoint"
        )
        assert calls == []

        for _ in range(2):
            response = client.put(
                "/agent-runs/heartbeat",
                json={"run_id": "run-repeat"},
                headers=bearer,
            )
            assert response.status_code == 200, response.text

        assert calls == [
            (agent["id"], "run-repeat"),
            (agent["id"], "run-repeat"),
        ]


def test_admin_agent_run_health_includes_safe_checkins_and_totals(tmp_path):
    app = create_app(tmp_path / "heartbeat-health.db")
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(
            client,
            email="bot@e.com",
            name="Bot",
            is_agent=True,
        )
        token = _mint(
            client,
            actor_id=agent["id"],
            scopes=["issue:write"],
            name="writer",
        )
        old_heartbeat = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "old-run"},
            headers=_bearer(token),
        )
        assert old_heartbeat.status_code == 200, old_heartbeat.text
        conn = db.connect(app.state.db_path)
        try:
            conn.execute(
                "UPDATE agent_run_checkins "
                "SET first_seen_at = '2000-01-01 00:00:00', "
                "last_seen_at = '2000-01-01 00:00:00' "
                "WHERE agent_id = ? AND run_id = 'old-run'",
                (agent["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        heartbeat = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "health-run"},
            headers=_bearer(token),
        )
        assert heartbeat.status_code == 200, heartbeat.text

        response = client.get("/activity/agent-runs", headers=_ADMIN_HEADER)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert len(payload["checkins"]) == 2
        checkin = payload["latest_checkins"][0]
        assert set(checkin) == _SAFE_CHECKIN_KEYS
        assert checkin["agent_id"] == agent["id"]
        assert checkin["agent_name"] == "Bot"
        assert checkin["agent_email"] == "bot@e.com"
        assert checkin["run_id"] == "health-run"
        assert checkin["reporting_state"] == "reporting_recently"
        assert payload["totals"]["reporting_recently_count"] == 1
        assert payload["totals"]["stale_checkin_count"] == 1
        assert payload["totals"]["total_checkin_count"] == 2
        assert payload["totals"]["latest_reporting_recently_count"] == 1
        assert payload["totals"]["latest_stale_checkin_count"] == 0
        assert payload["totals"]["latest_checkin_count"] == 1
