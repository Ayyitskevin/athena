"""Fleet active-work projection: claims become truthful operator decisions.

The load-bearing contract is that a durable issue lease, its exact tagged claim
run, a cooperative check-in, blockers, and replay evidence remain separate facts.
Athena may surface those facts together, but must not infer that an OS process is
alive or that work is executing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from athena.aegis import (
    dependencies,
    fleet_work,
    issue_commands,
    issue_etags,
    issues,
    lease_commands,
    projects,
)
from athena.aegis.fleet_work_api import ActiveWorkOut
from athena.core import (
    access,
    agent_commands,
    agent_run_checkins,
    db,
    run_context,
    tokens,
    users,
)
from athena.main import create_app
from athena.mcp.client import AthenaClient


NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _migrated(path):
    conn = db.connect(path)
    db.migrate(conn)
    return conn


def _user(conn, user_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def _issue_tag(conn, issue_id: int) -> str:
    issue = issues.get_issue(conn, issue_id)
    assert issue is not None
    return issue_etags.current_etag(conn, issue, actor=_user(conn, 1))


def _seed_claim(
    conn,
    *,
    agent_email: str = "agent@example.com",
    agent_name: str = "Agent",
    run_id: str | None = "run-claim",
    issue_title: str = "Implement the slice",
    project_id: int | None = None,
) -> dict:
    if conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone() is None:
        conn.execute(
            "INSERT INTO users (email, name, role, is_agent) "
            "VALUES ('operator@example.com', 'Operator', 'admin', 0)"
        )
        conn.commit()
    cur = conn.execute(
        "INSERT INTO users (email, name, role, is_agent) VALUES (?, ?, 'member', 1)",
        (agent_email, agent_name),
    )
    agent_id = int(cur.lastrowid)
    conn.commit()
    issue = issues.create_issue(
        conn,
        title=issue_title,
        body="SECRET_SHOULD_NOT_BE_IN_THE_FLEET_PROJECTION",
        created_by=1,
        project_id=project_id,
    )
    issue_commands.add_contributor(
        conn,
        actor=_user(conn, 1),
        issue_id=issue["id"],
        user_id=agent_id,
        require_agent=True,
    )
    token = tokens.create_token(
        conn,
        user_id=agent_id,
        name=f"{agent_name} token",
        scopes=[tokens.ISSUE_WRITE_SCOPE],
    )
    context_token = run_context.set_run_id(run_id)
    try:
        lease = lease_commands.claim_issue(
            conn,
            actor=_user(conn, agent_id),
            issue_id=issue["id"],
            if_match=[_issue_tag(conn, issue["id"])],
            lease_seconds=3600,
        )
    finally:
        run_context.reset_run_id(context_token)
    return {
        "agent_id": agent_id,
        "issue_id": int(issue["id"]),
        "token_id": int(token["id"]),
        "raw_token": token["token"],
        "run_id": run_id,
        "generation": lease["generation"],
    }


def _set_claim_window(
    conn,
    seeded: dict,
    *,
    claimed_at: str = "2026-07-21 11:00:00",
    expires_at: str = "2026-07-21 13:00:00",
) -> None:
    conn.execute(
        "UPDATE issue_leases SET claimed_at = ?, expires_at = ? WHERE issue_id = ?",
        (claimed_at, expires_at, seeded["issue_id"]),
    )
    conn.execute(
        "UPDATE activity SET created_at = ? WHERE id = ("
        "SELECT id FROM activity WHERE target_kind = 'issue' AND target_id = ? "
        "AND actor_id = ? AND verb IN ('claimed', 'lease_renewed') "
        "ORDER BY id DESC LIMIT 1)",
        (claimed_at, seeded["issue_id"], seeded["agent_id"]),
    )
    conn.commit()


def _check_in(conn, seeded: dict, *, last_seen_at: str) -> None:
    agent_run_checkins.upsert_checkin(
        conn,
        agent_id=seeded["agent_id"],
        run_id=seeded["run_id"],
        token_id=seeded["token_id"],
    )
    conn.execute(
        "UPDATE agent_run_checkins SET first_seen_at = ?, last_seen_at = ? "
        "WHERE agent_id = ? AND run_id = ?",
        (
            last_seen_at,
            last_seen_at,
            seeded["agent_id"],
            seeded["run_id"],
        ),
    )
    conn.commit()


def test_projection_joins_exact_run_checkin_blockers_and_replay(tmp_path):
    conn = _migrated(tmp_path / "fleet.db")
    seeded = _seed_claim(conn)
    _set_claim_window(conn, seeded)
    _check_in(conn, seeded, last_seen_at="2026-07-21 11:59:30")
    blocker = issues.create_issue(
        conn, title="Operator decision", body="", created_by=1
    )
    reason, inserted = dependencies.add_link(
        conn,
        from_id=blocker["id"],
        to_id=seeded["issue_id"],
        relation="blocks",
        created_by=1,
    )
    assert (reason, inserted) == (None, True)

    result = fleet_work.build_active_work(conn, now=NOW)

    assert result["schema"] == "athena.fleet_active_work.v1"
    assert result["scope"] == "admin_fleet_view"
    assert result["observed_at"] == "2026-07-21 12:00:00"
    assert result["semantics"]["does_not_assert"] == [
        "process_alive",
        "work_executing",
        "issue_unblocked",
        "approval_granted",
    ]
    assert result["visible_total"] == 1
    assert result["summary"]["scope"] == "returned_items"
    item = result["items"][0]
    assert item["issue"]["id"] == seeded["issue_id"]
    assert item["holder"]["id"] == seeded["agent_id"]
    assert item["lease"]["claim_state"] == "active"
    assert item["run"] == {
        "claim_event_id": item["run"]["claim_event_id"],
        "run_id": "run-claim",
        "reporting_state": "reporting_recently",
        "last_seen_at": "2026-07-21 11:59:30",
        "age_seconds": 30,
        "replay_ready": True,
        "evidence_links_available": True,
    }
    assert isinstance(item["run"]["claim_event_id"], int)
    assert item["open_blockers"]["visible_total"] == 1
    assert item["open_blockers"]["items"][0]["id"] == blocker["id"]
    assert item["attention_state"] == "needs_attention"
    assert item["attention_reasons"] == ["visible_open_blockers"]
    assert seeded["raw_token"] not in json.dumps(result)
    assert "SECRET_SHOULD_NOT_BE_IN_THE_FLEET_PROJECTION" not in json.dumps(result)


def test_stale_boundary_and_expired_claim_never_look_observed(tmp_path):
    conn = _migrated(tmp_path / "stale.db")
    seeded = _seed_claim(conn)
    _set_claim_window(conn, seeded)
    boundary = NOW - timedelta(seconds=90)
    _check_in(conn, seeded, last_seen_at=boundary.strftime("%Y-%m-%d %H:%M:%S"))

    exact = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert exact["run"]["reporting_state"] == "reporting_recently"
    assert exact["attention_state"] == "observed"

    one_second_later = fleet_work.build_active_work(
        conn, now=NOW + timedelta(seconds=1)
    )["items"][0]
    assert one_second_later["run"]["reporting_state"] == "stale"
    assert one_second_later["attention_reasons"] == ["checkin_stale"]

    _set_claim_window(conn, seeded, expires_at="2026-07-21 12:00:00")
    expired = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert expired["lease"]["claim_state"] == "expired"
    assert expired["attention_state"] == "needs_attention"
    assert "lease_expired" in expired["attention_reasons"]


@pytest.mark.parametrize(
    ("run_id", "expected_state", "reason"),
    [
        (None, "untagged", "run_untagged"),
        ("never-checked-in", "not_reported", "checkin_missing"),
    ],
)
def test_missing_run_evidence_is_explicit(tmp_path, run_id, expected_state, reason):
    conn = _migrated(tmp_path / f"missing-{expected_state}.db")
    seeded = _seed_claim(conn, run_id=run_id)
    _set_claim_window(conn, seeded)
    item = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert item["run"]["reporting_state"] == expected_state
    assert item["attention_reasons"] == [reason]
    assert item["attention_state"] == "needs_attention"


def test_unavailable_holder_states_are_explicit(tmp_path):
    conn = _migrated(tmp_path / "holder-access.db")

    def prepared(name: str, run_id: str) -> dict:
        seeded = _seed_claim(
            conn,
            agent_email=f"{name.lower()}@example.com",
            agent_name=name,
            run_id=run_id,
        )
        _set_claim_window(conn, seeded)
        _check_in(conn, seeded, last_seen_at="2026-07-21 11:59:30")
        return seeded

    paused = prepared("Paused", "run-paused")
    agent_commands.set_user_paused(
        conn, actor=_user(conn, 1), target_user_id=paused["agent_id"], paused=True
    )
    paused_item = fleet_work.build_active_work(
        conn, agent_id=paused["agent_id"], now=NOW
    )["items"][0]
    assert paused_item["holder"]["paused_at"] is not None
    assert paused_item["attention_reasons"] == ["holder_paused"]

    offboarded = prepared("Offboarded", "run-offboarded")
    agent_commands.offboard_user(
        conn, actor=_user(conn, 1), target_user_id=offboarded["agent_id"]
    )
    offboarded_item = fleet_work.build_active_work(
        conn, agent_id=offboarded["agent_id"], now=NOW
    )["items"][0]
    assert offboarded_item["holder"]["role"] == "viewer"
    assert offboarded_item["holder"]["live_issue_write_token_count"] == 0
    assert offboarded_item["attention_reasons"] == [
        "holder_read_only",
        "no_live_issue_write_token",
    ]

    ineligible = prepared("Ineligible", "run-ineligible")
    issue_commands.remove_contributor(
        conn,
        actor=_user(conn, 1),
        issue_id=ineligible["issue_id"],
        user_id=ineligible["agent_id"],
    )
    ineligible_item = fleet_work.build_active_work(
        conn, agent_id=ineligible["agent_id"], now=NOW
    )["items"][0]
    assert ineligible_item["holder"]["eligible_for_issue"] is False
    assert ineligible_item["attention_reasons"] == ["holder_ineligible"]

    tokenless = prepared("Tokenless", "run-tokenless")
    assert (
        tokens.revoke_token(
            conn, user_id=tokenless["agent_id"], token_id=tokenless["token_id"]
        )
        is True
    )
    tokenless_item = fleet_work.build_active_work(
        conn, agent_id=tokenless["agent_id"], now=NOW
    )["items"][0]
    assert tokenless_item["holder"]["live_issue_write_token_count"] == 0
    assert tokenless_item["attention_reasons"] == ["no_live_issue_write_token"]


def test_project_visibility_drift_changes_current_holder_eligibility(tmp_path):
    conn = _migrated(tmp_path / "holder-visibility.db")
    seeded = _seed_claim(conn)
    project = projects.create_project(
        conn,
        name="Private work",
        key="PRV",
        created_by=1,
    )
    conn.execute(
        "UPDATE issues SET project_id = ? WHERE id = ?",
        (project["id"], seeded["issue_id"]),
    )
    conn.commit()
    _set_claim_window(conn, seeded)
    _check_in(conn, seeded, last_seen_at="2026-07-21 11:59:30")

    projects.set_visibility(conn, project["id"], "private")
    holder = _user(conn, seeded["agent_id"])
    assert access.can_see_issue(conn, holder, seeded["issue_id"]) is False
    hidden = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert hidden["holder"]["eligible_for_issue"] is False
    assert hidden["attention_reasons"] == ["holder_ineligible"]

    assert (
        access.add_project_member(conn, project["id"], seeded["agent_id"], added_by=1)
        is True
    )
    assert access.can_see_issue(conn, holder, seeded["issue_id"]) is True
    restored = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert restored["holder"]["eligible_for_issue"] is True
    assert restored["attention_state"] == "observed"


def test_done_or_archived_issue_claim_cannot_look_observed(tmp_path):
    conn = _migrated(tmp_path / "closed-issue.db")
    seeded = _seed_claim(conn)
    _set_claim_window(conn, seeded)
    _check_in(conn, seeded, last_seen_at="2026-07-21 11:59:30")

    issue_commands.update_issue(
        conn,
        actor=_user(conn, 1),
        issue_id=seeded["issue_id"],
        status="done",
    )
    done = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert done["issue"]["status_category"] == "done"
    assert done["issue"]["archived_at"] is None
    assert done["attention_reasons"] == ["issue_done"]

    issue_commands.set_issue_archived(
        conn,
        actor=_user(conn, 1),
        issue_id=seeded["issue_id"],
        archived=True,
    )
    archived = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert archived["issue"]["archived_at"] is not None
    assert archived["attention_reasons"] == ["issue_archived", "issue_done"]


def test_response_contract_is_strict_and_rejects_extra_fields(tmp_path):
    conn = _migrated(tmp_path / "strict-response.db")
    payload = fleet_work.build_active_work(conn, now=NOW)
    assert ActiveWorkOut.model_validate(payload).summary.scope == "returned_items"

    with pytest.raises(ValidationError):
        ActiveWorkOut.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError):
        ActiveWorkOut.model_validate(
            {
                **payload,
                "summary": {
                    **payload["summary"],
                    "returned_count": "0",
                },
            }
        )


def test_live_token_lookup_uses_partial_holder_index(tmp_path):
    conn = _migrated(tmp_path / "token-plan.db")
    seeded = _seed_claim(conn)
    statement, params = fleet_work._live_issue_write_token_statement(
        [seeded["agent_id"]]
    )
    plan = " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + statement, params)
    )
    assert "idx_api_tokens_live_user" in plan
    assert "SCAN token" not in plan


def test_projection_is_read_only_and_one_wal_snapshot(tmp_path, monkeypatch):
    db_file = tmp_path / "snapshot.db"
    seed_conn = _migrated(db_file)
    seeded = _seed_claim(seed_conn)
    project = projects.create_project(
        seed_conn,
        name="Snapshot project",
        key="SNP",
        created_by=1,
    )
    seed_conn.execute(
        "UPDATE issues SET project_id = ? WHERE id = ?",
        (project["id"], seeded["issue_id"]),
    )
    seed_conn.commit()
    _set_claim_window(seed_conn, seeded)
    _check_in(seed_conn, seeded, last_seen_at="2026-07-21 11:59:30")
    seed_conn.close()

    reader = db.connect(db_file)
    writer = db.connect(db_file)
    tables = (
        "issue_leases",
        "activity",
        "agent_run_checkins",
        "api_tokens",
    )
    before_counts = tuple(
        reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tables
    )
    before_changes = reader.total_changes
    first = fleet_work.build_active_work(reader, now=NOW)
    second = fleet_work.build_active_work(reader, now=NOW)
    assert second == first
    assert reader.total_changes == before_changes
    assert (
        tuple(
            reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        )
        == before_counts
    )

    original = fleet_work._live_issue_write_token_counts
    interleaved = False

    def revoke_between_component_queries(conn, holder_ids):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            writer.execute(
                "UPDATE projects SET visibility = 'private' WHERE id = ?",
                (project["id"],),
            )
            writer.execute(
                "UPDATE api_tokens SET revoked_at = datetime('now') WHERE id = ?",
                (seeded["token_id"],),
            )
            writer.execute(
                "DELETE FROM agent_run_checkins WHERE agent_id = ? AND run_id = ?",
                (seeded["agent_id"], seeded["run_id"]),
            )
            writer.commit()
        return original(conn, holder_ids)

    monkeypatch.setattr(
        fleet_work,
        "_live_issue_write_token_counts",
        revoke_between_component_queries,
    )
    coherent = fleet_work.build_active_work(reader, now=NOW)["items"][0]
    assert interleaved is True
    assert coherent["holder"]["eligible_for_issue"] is True
    assert coherent["holder"]["live_issue_write_token_count"] == 1
    assert coherent["run"]["reporting_state"] == "reporting_recently"

    changed = fleet_work.build_active_work(reader, now=NOW)["items"][0]
    assert changed["holder"]["eligible_for_issue"] is False
    assert changed["holder"]["live_issue_write_token_count"] == 0
    assert changed["run"]["reporting_state"] == "not_reported"
    assert changed["attention_reasons"] == [
        "holder_ineligible",
        "no_live_issue_write_token",
        "checkin_missing",
    ]
    reader.close()
    writer.close()


def test_repeated_claim_is_one_item_and_restart_safe(tmp_path):
    db_file = tmp_path / "restart.db"
    conn = _migrated(db_file)
    seeded = _seed_claim(conn)
    context_token = run_context.set_run_id(seeded["run_id"])
    try:
        lease_commands.claim_issue(
            conn,
            actor=_user(conn, seeded["agent_id"]),
            issue_id=seeded["issue_id"],
            if_match=[_issue_tag(conn, seeded["issue_id"])],
            generation=seeded["generation"],
            lease_seconds=3600,
        )
    finally:
        run_context.reset_run_id(context_token)
    assert conn.execute("SELECT COUNT(*) AS n FROM issue_leases").fetchone()["n"] == 1
    before = fleet_work.build_active_work(conn)
    assert len(before["items"]) == 1
    conn.close()

    reopened = db.connect(db_file)
    after = fleet_work.build_active_work(reopened)
    assert len(after["items"]) == 1
    assert after["items"][0]["run"]["run_id"] == seeded["run_id"]
    assert after["items"][0]["run"]["replay_ready"] is True


def test_newest_same_timestamp_renewal_selects_new_run(tmp_path):
    conn = _migrated(tmp_path / "renewal-run.db")
    seeded = _seed_claim(conn, run_id="run-old")
    _set_claim_window(
        conn,
        seeded,
        claimed_at="2026-07-21 11:00:00",
        # Keep the real command-time lease active; the second call below resets
        # the projection clock after renewal.
        expires_at="2099-07-21 23:59:59",
    )

    run_token = run_context.set_run_id("run-new")
    try:
        lease_commands.claim_issue(
            conn,
            actor=_user(conn, seeded["agent_id"]),
            issue_id=seeded["issue_id"],
            if_match=[_issue_tag(conn, seeded["issue_id"])],
            generation=seeded["generation"],
            lease_seconds=3600,
        )
    finally:
        run_context.reset_run_id(run_token)
    _set_claim_window(
        conn,
        seeded,
        claimed_at="2026-07-21 11:00:00",
        expires_at="2026-07-21 23:59:59",
    )
    renewed = {**seeded, "run_id": "run-new"}
    _check_in(conn, renewed, last_seen_at="2026-07-21 11:59:30")

    claim_rows = conn.execute(
        "SELECT created_at, run_id FROM activity "
        "WHERE target_kind = 'issue' AND target_id = ? "
        "AND verb IN ('claimed', 'lease_renewed') ORDER BY id",
        (seeded["issue_id"],),
    ).fetchall()
    assert [row["created_at"] for row in claim_rows] == [
        "2026-07-21 11:00:00",
        "2026-07-21 11:00:00",
    ]
    assert [row["run_id"] for row in claim_rows] == ["run-old", "run-new"]

    item = fleet_work.build_active_work(conn, now=NOW)["items"][0]
    assert item["run"]["run_id"] == "run-new"
    assert item["run"]["reporting_state"] == "reporting_recently"


@pytest.mark.parametrize("run_id", ("run/with?reserved#characters", ".", ".."))
def test_reserved_run_id_suppresses_broken_path_links(tmp_path, run_id):
    conn = _migrated(tmp_path / "reserved-run.db")
    seeded = _seed_claim(conn, run_id=run_id)
    _set_claim_window(conn, seeded)

    run = fleet_work.build_active_work(conn, now=NOW)["items"][0]["run"]
    assert run["replay_ready"] is True
    assert run["evidence_links_available"] is False


def test_projection_is_bounded_filterable_and_rejects_coercion(tmp_path):
    conn = _migrated(tmp_path / "bounds.db")
    first = _seed_claim(conn, agent_email="a@e.com", agent_name="A", run_id="a")
    second = _seed_claim(conn, agent_email="b@e.com", agent_name="B", run_id="b")
    limited = fleet_work.build_active_work(conn, limit=1)
    assert limited["visible_total"] == 2
    assert len(limited["items"]) == 1
    assert limited["clipped"] is True
    filtered = fleet_work.build_active_work(conn, agent_id=second["agent_id"])
    assert [item["holder"]["id"] for item in filtered["items"]] == [second["agent_id"]]
    for invalid in (True, "1", 1.0, 0, fleet_work.MAX_LIMIT + 1):
        with pytest.raises(ValueError):
            fleet_work.build_active_work(conn, limit=invalid)
    assert first["agent_id"] != second["agent_id"]


def test_rest_and_web_are_admin_only_and_share_projection(tmp_path):
    db_file = tmp_path / "surfaces.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        conn = db.connect(db_file)
        seeded = _seed_claim(conn)
        _set_claim_window(conn, seeded)
        _check_in(conn, seeded, last_seen_at="2026-07-21 11:59:30")
        for number in range(fleet_work.MAX_BLOCKER_PREVIEW + 1):
            blocker = issues.create_issue(
                conn,
                title=f"Visible blocker {number}",
                body="",
                created_by=1,
            )
            reason, inserted = dependencies.add_link(
                conn,
                from_id=blocker["id"],
                to_id=seeded["issue_id"],
                relation="blocks",
                created_by=1,
            )
            assert (reason, inserted) == (None, True)
        users.set_password(conn, 1, "secret")
        unsafe = _seed_claim(
            conn,
            agent_email="unsafe@example.com",
            agent_name="Unsafe path agent",
            run_id="run/unsafe?#",
        )
        _set_claim_window(conn, unsafe)
        read_only_admin = tokens.create_token(
            conn,
            user_id=1,
            name="read-only admin",
            scopes=[tokens.READ_SCOPE],
        )
        scoped_admin = tokens.create_token(
            conn,
            user_id=1,
            name="scoped admin",
            scopes=[tokens.ADMIN_SCOPE],
        )
        conn.close()

        admin_headers = {"X-Athena-Actor": "1"}
        response = client.get("/fleet/active-work", headers=admin_headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["vary"] == "Authorization, X-Athena-Actor"
        assert response.json()["items"][0]["issue"]["id"] == seeded["issue_id"]
        blocker_view = response.json()["items"][0]["open_blockers"]
        assert blocker_view["visible_total"] == fleet_work.MAX_BLOCKER_PREVIEW + 1
        assert len(blocker_view["items"]) == fleet_work.MAX_BLOCKER_PREVIEW
        assert blocker_view["clipped"] is True
        denied_scope = client.get(
            "/fleet/active-work",
            headers={"Authorization": f"Bearer {read_only_admin['token']}"},
        )
        assert denied_scope.status_code == 403
        assert denied_scope.headers["cache-control"] == "private, no-store"
        bearer_response = client.get(
            "/fleet/active-work",
            headers={"Authorization": f"Bearer {scoped_admin['token']}"},
        )
        assert bearer_response.status_code == 200
        assert bearer_response.json()["items"][0]["issue"]["id"] == seeded["issue_id"]
        member_response = client.get(
            "/fleet/active-work",
            headers={"X-Athena-Actor": str(seeded["agent_id"])},
        )
        assert member_response.status_code == 403
        assert member_response.headers["cache-control"] == "private, no-store"
        anonymous = client.get("/fleet/active-work")
        assert anonymous.status_code in (401, 403)
        assert anonymous.headers["cache-control"] == "private, no-store"
        invalid_bearer = client.get(
            "/fleet/active-work",
            headers={"Authorization": "Bearer invalid"},
        )
        assert invalid_bearer.status_code == 401
        assert invalid_bearer.headers["cache-control"] == "private, no-store"

        overflow = issues.MAX_SQLITE_INTEGER + 1
        invalid_queries = (
            "limit=0",
            "limit=1.0",
            "limit=%2B1",
            "limit=1&limit=2",
            "agent_id=&agent_id=1",
            "unknown=1",
            "agent_id=1.0",
            f"agent_id={overflow}",
            "agent_id=" + ("9" * 100),
        )
        for query in invalid_queries:
            invalid = client.get(f"/fleet/active-work?{query}", headers=admin_headers)
            assert invalid.status_code == 422
            assert invalid.headers["cache-control"] == "private, no-store"

        signed_out_page = client.get("/admin/agents/runs")
        assert signed_out_page.status_code == 401
        assert signed_out_page.headers["cache-control"] == "private, no-store"
        assert signed_out_page.headers["vary"] == "Cookie"

        login = client.post(
            "/login",
            data={"email": "operator@example.com", "password": "secret"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        for query in invalid_queries:
            invalid_page = client.get(f"/admin/agents/runs?{query}")
            assert invalid_page.status_code == 400
            assert invalid_page.headers["cache-control"] == "private, no-store"
            assert invalid_page.headers["vary"] == "Cookie"
        all_agents_page = client.get("/admin/agents/runs?agent_id=")
        assert all_agents_page.status_code == 200
        assert "Active claimed work" in all_agents_page.text
        page = client.get("/admin/agents/runs")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "private, no-store"
        assert page.headers["vary"] == "Cookie"
        assert "Active claimed work" in page.text
        assert "Showing 5 of 6 visible blockers." in " ".join(page.text.split())
        assert "Implement the slice" in page.text
        assert "run-claim" in page.text
        assert (
            "Evidence exists, but current path routes cannot address this run ID."
            in page.text
        )
        active_work_html = page.text.split('<h2 id="fleet-active-work-heading">', 1)[
            1
        ].split('<section class="dashboard-card agent-checkins">', 1)[0]
        assert "/admin/agents/runs/run/unsafe" not in active_work_html
        assert "SECRET_SHOULD_NOT_BE_IN_THE_FLEET_PROJECTION" not in page.text
        assert seeded["raw_token"] not in page.text


def test_mcp_admin_tool_calls_the_real_rest_projection(tmp_path):
    from athena.mcp.server import build_server

    db_file = tmp_path / "mcp.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        conn = db.connect(db_file)
        seeded = _seed_claim(conn)
        _set_claim_window(conn, seeded)
        admin_token = tokens.create_token(
            conn,
            user_id=1,
            name="MCP admin",
            scopes=[tokens.ADMIN_SCOPE],
        )
        conn.close()
        client.headers["Authorization"] = f"Bearer {admin_token['token']}"
        server = build_server(AthenaClient(client=client))
        result = asyncio.run(
            server.call_tool(
                "get_fleet_active_work",
                {"agent_id": seeded["agent_id"], "limit": 10},
            )
        )
        payload = json.loads(result[0].text)
        assert payload["schema"] == "athena.fleet_active_work.v1"
        assert payload["items"][0]["holder"]["id"] == seeded["agent_id"]

        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        schema = tools["get_fleet_active_work"].inputSchema
        assert set(schema["properties"]) == {
            "agent_id",
            "limit",
            "attention_state",
        }
        assert schema["properties"]["limit"]["default"] == fleet_work.DEFAULT_LIMIT

        from mcp.server.fastmcp.exceptions import ToolError

        for invalid in (True, "10", 10.0, 0, fleet_work.MAX_LIMIT + 1):
            with pytest.raises(ToolError):
                asyncio.run(
                    server.call_tool("get_fleet_active_work", {"limit": invalid})
                )

        invalid_agent_ids = (
            True,
            "1",
            1.0,
            0,
            issues.MAX_SQLITE_INTEGER + 1,
        )
        for invalid in invalid_agent_ids:
            with pytest.raises(ToolError):
                asyncio.run(
                    server.call_tool("get_fleet_active_work", {"agent_id": invalid})
                )
