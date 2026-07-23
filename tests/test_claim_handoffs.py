"""Lease-generation fencing and typed claim-handoff lifecycle invariants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import shutil
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from athena.aegis import (
    claim_handoffs,
    issue_commands,
    issue_etags,
    issues,
    lease_commands,
    leases,
)
from athena.core import activity, db
from athena.main import create_app


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_users(conn):
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) "
        "VALUES ('owner@e.com', 'Owner', 'admin', 0)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) "
        "VALUES ('a@e.com', 'Agent A', 1)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) "
        "VALUES ('b@e.com', 'Agent B', 1)"
    )
    conn.commit()


def _actor(conn, user_id):
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def _delegated_issue(conn):
    issue = issues.create_issue(
        conn,
        title="generation-fenced work",
        body="continue safely",
        created_by=1,
    )
    for user_id in (2, 3):
        issue_commands.add_contributor(
            conn,
            actor=_actor(conn, 1),
            issue_id=issue["id"],
            user_id=user_id,
            require_agent=True,
        )
    return issue


def _issue_tag(conn, issue_id):
    issue = issues.get_issue(conn, issue_id)
    assert issue is not None
    return issue_etags.current_etag(conn, issue)


def _claim(conn, user_id, issue_id, *, generation=None):
    return lease_commands.claim_issue(
        conn,
        actor=_actor(conn, user_id),
        issue_id=issue_id,
        if_match=[_issue_tag(conn, issue_id)],
        generation=generation,
    )


def _handoff_payload(generation, **overrides):
    payload = {
        "generation": generation,
        "reason": "blocked",
        "note": "Waiting for an operator decision.",
        "attempted_work": "Reproduced the failure and isolated the boundary.",
        "evidence": ["focused test reproduces the failure"],
        "blocking_question": "Which recovery path should be used?",
        "resume_instructions": "Choose the recovery path, then rerun the focused test.",
    }
    payload.update(overrides)
    return payload


def _raw_handoff_insert(
    conn,
    *,
    issue_id,
    event_id,
    generation,
    token,
    reason="blocked",
    question="Which recovery path should be used?",
    evidence_json='["evidence"]',
    attempted_work="Attempted work",
    resume_instructions="Resume carefully",
):
    conn.execute(
        "INSERT INTO issue_claim_handoffs "
        "(handoff_token, issue_id, yield_event_id, lease_generation, reason, "
        "attempted_work, evidence_json, blocking_question, resume_instructions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token,
            issue_id,
            event_id,
            generation,
            reason,
            attempted_work,
            evidence_json,
            question,
            resume_instructions,
        ),
    )


def test_upgrade_from_0056_preserves_leases_and_enforces_generations(
    tmp_path, monkeypatch
):
    source_migrations = db.MIGRATIONS_DIR
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(source_migrations.glob("*.sql")):
        if migration.name > "0056_api_tokens_live_user_index.sql":
            continue
        shutil.copy(migration, staged_migrations / migration.name)

    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged_migrations)
    conn = db.connect(tmp_path / "upgrade.db")
    assert db.migrate(conn)[-1] == "0056_api_tokens_live_user_index.sql"
    _seed_users(conn)
    issue_a = issues.create_issue(conn, title="legacy A", body="", created_by=1)
    issue_b = issues.create_issue(conn, title="legacy B", body="", created_by=1)
    legacy_rows = [
        (issue_a["id"], 2, "2025-01-02 03:04:05", "2025-01-02 04:04:05"),
        (issue_b["id"], 3, "2025-02-03 04:05:06", "2099-02-03 05:05:06"),
    ]
    conn.executemany(
        "INSERT INTO issue_leases "
        "(issue_id, holder_id, claimed_at, expires_at) VALUES (?, ?, ?, ?)",
        legacy_rows,
    )
    conn.commit()

    for name in (
        "0057_issue_lease_generations.sql",
        "0058_issue_claim_handoffs.sql",
    ):
        shutil.copy(source_migrations / name, staged_migrations / name)
    assert db.migrate(conn) == [
        "0057_issue_lease_generations.sql",
        "0058_issue_claim_handoffs.sql",
    ]

    upgraded = [
        dict(row)
        for row in conn.execute(
            "SELECT issue_id, holder_id, claimed_at, expires_at, generation "
            "FROM issue_leases ORDER BY issue_id"
        )
    ]
    assert [
        (row["issue_id"], row["holder_id"], row["claimed_at"], row["expires_at"])
        for row in upgraded
    ] == legacy_rows
    generations = [row["generation"] for row in upgraded]
    assert all(leases.is_valid_generation(value) for value in generations)
    assert len(set(generations)) == 2

    with pytest.raises(sqlite3.IntegrityError, match="valid issue lease generation"):
        conn.execute(
            "UPDATE issue_leases SET generation = 'INVALID' WHERE issue_id = ?",
            (issue_a["id"],),
        )
    conn.rollback()
    with pytest.raises(
        sqlite3.IntegrityError, match="active issue lease generation is immutable"
    ):
        conn.execute(
            "UPDATE issue_leases SET generation = ? WHERE issue_id = ?",
            ("a" * 32, issue_b["id"]),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="issue lease holder"):
        conn.execute(
            "UPDATE issue_leases SET holder_id = ? WHERE issue_id = ?",
            (2, issue_b["id"]),
        )
    conn.rollback()
    with pytest.raises(
        sqlite3.IntegrityError,
        match="reactivated issue lease requires a new generation",
    ):
        conn.execute(
            "UPDATE issue_leases SET expires_at = '2099-01-01 00:00:00' "
            "WHERE issue_id = ?",
            (issue_a["id"],),
        )
    conn.rollback()
    with pytest.raises(
        sqlite3.IntegrityError, match="holder change requires a new generation"
    ):
        conn.execute(
            "UPDATE issue_leases SET holder_id = ? WHERE issue_id = ?",
            (3, issue_a["id"]),
        )
    conn.rollback()

    replacement = leases.upsert_lease(conn, issue_a["id"], 3, 600)
    assert replacement["generation"] != generations[0]
    assert replacement["holder_id"] == 3
    assert leases.delete_lease(conn, issue_a["id"], generations[0]) is False
    assert leases.get_lease(conn, issue_a["id"])["generation"] == replacement["generation"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_handoff_database_binds_audit_payload_and_is_durable(tmp_path):
    conn = _migrated_conn(tmp_path / "handoff-db.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"])
    generation = lease["generation"]

    wrong_actor_token = "a" * 32
    wrong_actor_event = activity.record(
        conn,
        actor_id=3,
        verb="claim_yielded",
        target_kind="issue",
        target_id=issue["id"],
        detail=(
            f"generation {generation}; handoff {wrong_actor_token}; "
            "blocked: Which recovery path should be used?"
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="matching native claim yield"):
        _raw_handoff_insert(
            conn,
            issue_id=issue["id"],
            event_id=wrong_actor_event["id"],
            generation=generation,
            token=wrong_actor_token,
        )
    conn.rollback()

    row_token = "b" * 32
    mismatched_event = activity.record(
        conn,
        actor_id=2,
        verb="claim_yielded",
        target_kind="issue",
        target_id=issue["id"],
        detail=(
            f"generation {generation}; handoff {'c' * 32}; "
            "blocked: a different question"
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="matching native claim yield"):
        _raw_handoff_insert(
            conn,
            issue_id=issue["id"],
            event_id=mismatched_event["id"],
            generation=generation,
            token=row_token,
        )
    conn.rollback()

    handoff = lease_commands.yield_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        **_handoff_payload(generation),
    )
    assert "id" not in handoff
    with pytest.raises(sqlite3.IntegrityError, match="claim handoff activity is immutable"):
        conn.execute(
            "UPDATE activity SET detail = 'rewritten' WHERE id = ?",
            (handoff["yielded"]["event_id"],),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="claim handoff activity is immutable"):
        conn.execute(
            "DELETE FROM activity WHERE id = ?",
            (handoff["yielded"]["event_id"],),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="transition is invalid"):
        conn.execute(
            "UPDATE issue_claim_handoffs SET attempted_work = 'rewritten' "
            "WHERE handoff_token = ?",
            (handoff["handoff_token"],),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="claim handoffs are durable"):
        conn.execute(
            "DELETE FROM issue_claim_handoffs WHERE handoff_token = ?",
            (handoff["handoff_token"],),
        )
    conn.rollback()

    replacement = _claim(conn, 2, issue["id"])
    second_token = "d" * 32
    second_question = "A second open question"
    second_event = activity.record(
        conn,
        actor_id=2,
        verb="claim_yielded",
        target_kind="issue",
        target_id=issue["id"],
        detail=(
            f"generation {replacement['generation']}; handoff {second_token}; "
            f"capacity: {second_question}"
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _raw_handoff_insert(
            conn,
            issue_id=issue["id"],
            event_id=second_event["id"],
            generation=replacement["generation"],
            token=second_token,
            reason="capacity",
            question=second_question,
        )
    conn.rollback()

    wrong_resume_event = activity.record(
        conn,
        actor_id=2,
        verb="claim_handoff_resumed",
        target_kind="issue",
        target_id=issue["id"],
        detail=(
            f"handoff {'e' * 32}; generation {replacement['generation']}"
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="matching native handoff resume"):
        conn.execute(
            "UPDATE issue_claim_handoffs SET resume_event_id = ?, "
            "resume_lease_generation = ? WHERE handoff_token = ?",
            (
                wrong_resume_event["id"],
                replacement["generation"],
                handoff["handoff_token"],
            ),
        )
    conn.rollback()

    resumed = lease_commands.resume_claim_handoff(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        handoff_token=handoff["handoff_token"],
        generation=replacement["generation"],
        resume_note="Context received, not resolved.",
    )
    assert resumed["state"] == "resumed"
    assert resumed["resumed"]["lease_generation"] == replacement["generation"]

    evidence_issue = _delegated_issue(conn)
    evidence_lease = _claim(conn, 2, evidence_issue["id"])
    evidence_token = "f" * 32
    evidence_event = activity.record(
        conn,
        actor_id=2,
        verb="claim_yielded",
        target_kind="issue",
        target_id=evidence_issue["id"],
        detail=(
            f"generation {evidence_lease['generation']}; handoff {evidence_token}; "
            "blocked: Which recovery path should be used?"
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="valid claim handoff evidence"):
        _raw_handoff_insert(
            conn,
            issue_id=evidence_issue["id"],
            event_id=evidence_event["id"],
            generation=evidence_lease["generation"],
            token=evidence_token,
            evidence_json='["valid", 7]',
        )
    conn.rollback()

    blank_variants = (
        {"attempted_work": "\t"},
        {"evidence_json": '["\\t"]'},
        {"question": "\n"},
        {"resume_instructions": "\r\n"},
    )
    for index, overrides in enumerate(blank_variants):
        token = f"{index + 1:032x}"
        question = overrides.get(
            "question", "Which recovery path should be used?"
        )
        event = activity.record(
            conn,
            actor_id=2,
            verb="claim_yielded",
            target_kind="issue",
            target_id=evidence_issue["id"],
            detail=(
                f"generation {evidence_lease['generation']}; handoff {token}; "
                f"blocked: {question}"
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            _raw_handoff_insert(
                conn,
                issue_id=evidence_issue["id"],
                event_id=event["id"],
                generation=evidence_lease["generation"],
                token=token,
                **overrides,
            )
        conn.rollback()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_generation_fences_every_delayed_holder_mutation(tmp_path):
    conn = _migrated_conn(tmp_path / "aba.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    first = _claim(conn, 2, issue["id"])
    handoff = lease_commands.yield_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        **_handoff_payload(first["generation"]),
    )
    replacement = _claim(conn, 2, issue["id"])
    assert replacement["generation"] != first["generation"]
    assert replacement["open_claim_handoff"]["handoff_token"] == handoff["handoff_token"]

    lease_before = leases.get_lease(conn, issue["id"])
    contributors_before = conn.execute(
        "SELECT user_id FROM issue_contributors WHERE issue_id = ? ORDER BY user_id",
        (issue["id"],),
    ).fetchall()
    events_before = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    )

    delayed_mutations = (
        lambda: _claim(conn, 2, issue["id"], generation=first["generation"]),
        lambda: lease_commands.yield_claim(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            **_handoff_payload(first["generation"]),
        ),
        lambda: lease_commands.complete_claim(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            generation=first["generation"],
        ),
        lambda: lease_commands.resume_claim_handoff(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            handoff_token=handoff["handoff_token"],
            generation=first["generation"],
        ),
        lambda: lease_commands.decline_delegation(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            generation=first["generation"],
        ),
    )
    for mutate in delayed_mutations:
        with pytest.raises(issue_commands.IssueCommandError) as rejected:
            mutate()
        assert rejected.value.kind == "lease_generation_mismatch"
        assert leases.get_lease(conn, issue["id"]) == lease_before
        assert conn.execute(
            "SELECT user_id FROM issue_contributors WHERE issue_id = ? ORDER BY user_id",
            (issue["id"],),
        ).fetchall() == contributors_before
        assert activity.list_activity(
            conn, target_kind="issue", target_id=issue["id"]
        ) == events_before
        assert claim_handoffs.get_open_handoff(conn, issue["id"])[
            "handoff_token"
        ] == handoff["handoff_token"]

    with pytest.raises(issue_commands.IssueCommandError, match="resume the open"):
        lease_commands.complete_claim(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            generation=replacement["generation"],
        )
    resumed = lease_commands.resume_claim_handoff(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        handoff_token=handoff["handoff_token"],
        generation=replacement["generation"],
    )
    assert resumed["state"] == "resumed"
    assert claim_handoffs.get_open_handoff(conn, issue["id"]) is None
    with pytest.raises(issue_commands.IssueCommandError, match="already resumed"):
        lease_commands.resume_claim_handoff(
            conn,
            actor=_actor(conn, 2),
            issue_id=issue["id"],
            handoff_token=handoff["handoff_token"],
            generation=replacement["generation"],
        )
    resumed_events = activity.list_activity(
        conn,
        target_kind="issue",
        target_id=issue["id"],
        verb="claim_handoff_resumed",
    )
    assert len(resumed_events) == 1
    lease_commands.complete_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        generation=replacement["generation"],
    )
    assert leases.get_lease(conn, issue["id"]) is None


def test_handoff_writes_roll_back_as_one_unit(tmp_path, monkeypatch):
    conn = _migrated_conn(tmp_path / "rollback.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"])
    lease_before = leases.get_lease(conn, issue["id"])
    notification_count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    def fail_handoff(*args, **kwargs):
        raise RuntimeError("handoff storage unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(lease_commands.claim_handoffs, "create_handoff", fail_handoff)
        with pytest.raises(RuntimeError, match="handoff storage unavailable"):
            lease_commands.yield_claim(
                conn,
                actor=_actor(conn, 2),
                issue_id=issue["id"],
                **_handoff_payload(
                    lease["generation"],
                    blocking_question="Can [[user:3]] choose the recovery path?",
                ),
            )
    assert leases.get_lease(conn, issue["id"]) == lease_before
    assert claim_handoffs.get_open_handoff(conn, issue["id"]) is None
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == notification_count
    assert activity.list_activity(
        conn,
        target_kind="issue",
        target_id=issue["id"],
        verb="claim_yielded",
    ) == []

    handoff = lease_commands.yield_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        **_handoff_payload(lease["generation"]),
    )
    replacement = _claim(conn, 2, issue["id"])
    events_before = activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    )

    def fail_resume(*args, **kwargs):
        raise RuntimeError("handoff update unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(lease_commands.claim_handoffs, "resume_handoff", fail_resume)
        with pytest.raises(RuntimeError, match="handoff update unavailable"):
            lease_commands.resume_claim_handoff(
                conn,
                actor=_actor(conn, 2),
                issue_id=issue["id"],
                handoff_token=handoff["handoff_token"],
                generation=replacement["generation"],
            )
    assert leases.get_lease(conn, issue["id"])["generation"] == replacement["generation"]
    assert claim_handoffs.get_open_handoff(conn, issue["id"])["state"] == "awaiting_resume"
    assert activity.list_activity(
        conn, target_kind="issue", target_id=issue["id"]
    ) == events_before


def test_concurrent_resume_records_exactly_one_acknowledgment(tmp_path):
    db_path = tmp_path / "resume-race.db"
    setup = _migrated_conn(db_path)
    _seed_users(setup)
    issue = _delegated_issue(setup)
    first = _claim(setup, 2, issue["id"])
    handoff = lease_commands.yield_claim(
        setup,
        actor=_actor(setup, 2),
        issue_id=issue["id"],
        **_handoff_payload(first["generation"]),
    )
    replacement = _claim(setup, 2, issue["id"])
    setup.close()
    barrier = threading.Barrier(2)

    def resume():
        conn = db.connect(db_path)
        try:
            barrier.wait(timeout=5)
            try:
                lease_commands.resume_claim_handoff(
                    conn,
                    actor=_actor(conn, 2),
                    issue_id=issue["id"],
                    handoff_token=handoff["handoff_token"],
                    generation=replacement["generation"],
                )
                return "resumed"
            except issue_commands.IssueCommandError as exc:
                return exc.kind
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result(timeout=10) for future in (pool.submit(resume), pool.submit(resume))]
    assert sorted(outcomes) == ["conflict", "resumed"]

    check = db.connect(db_path)
    try:
        persisted = claim_handoffs.get_handoff(
            check,
            issue_id=issue["id"],
            handoff_token=handoff["handoff_token"],
        )
        assert persisted["state"] == "resumed"
        assert len(
            activity.list_activity(
                check,
                target_kind="issue",
                target_id=issue["id"],
                verb="claim_handoff_resumed",
            )
        ) == 1
    finally:
        check.close()


def test_rest_handoff_lifecycle_projections_and_escaping(tmp_path):
    db_path = tmp_path / "rest.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        conn = db.connect(db_path)
        _seed_users(conn)
        conn.close()
        owner = {"X-Athena-Actor": "1"}
        agent_a = {"X-Athena-Actor": "2"}
        agent_b = {"X-Athena-Actor": "3"}
        issue = client.post(
            "/issues", json={"title": "safe handoff"}, headers=owner
        ).json()
        for user_id in (2, 3):
            assert client.post(
                f"/issues/{issue['id']}/delegate",
                json={"user_id": user_id},
                headers=owner,
            ).status_code == 201
        issue_tag = client.get(f"/issues/{issue['id']}").headers["etag"]
        first_claim = client.post(
            f"/issues/{issue['id']}/claim",
            headers={**agent_a, "If-Match": issue_tag},
        )
        assert first_claim.status_code == 201
        first_generation = first_claim.json()["generation"]
        assert first_claim.headers["cache-control"] == "private, no-store"

        base = _handoff_payload(
            first_generation,
            attempted_work='<script>alert("attempted")</script>',
            blocking_question='<script>alert("blocked")</script> Which path?',
        )
        missing = dict(base)
        missing.pop("generation")
        generation_errors = (
            (missing, 428, "lease_generation_required"),
            ({**base, "generation": "A" * 32}, 422, "invalid_lease_generation"),
            ({**base, "generation": "0" * 32}, 409, "lease_generation_mismatch"),
        )
        for body, status, code in generation_errors:
            rejected = client.post(
                f"/issues/{issue['id']}/yield", json=body, headers=agent_a
            )
            assert rejected.status_code == status
            assert rejected.json()["code"] == code
            assert rejected.headers["cache-control"] == "private, no-store"
            assert first_generation not in rejected.text

        for bad_evidence in (["x"] * 11, ["x" * 1001]):
            rejected = client.post(
                f"/issues/{issue['id']}/yield",
                json={**base, "evidence": bad_evidence},
                headers=agent_a,
            )
            assert rejected.status_code == 422

        yield_headers = {**agent_a, "Idempotency-Key": "safe-handoff-once"}
        yielded = client.post(
            f"/issues/{issue['id']}/yield", json=base, headers=yield_headers
        )
        assert yielded.status_code == 201
        handoff = yielded.json()
        assert handoff["state"] == "awaiting_resume"
        assert handoff["advisory_untrusted"] is True
        assert "id" not in handoff
        assert yielded.headers["cache-control"] == "private, no-store"

        replacement = client.post(
            f"/issues/{issue['id']}/claim",
            headers={**agent_b, "If-Match": issue_tag},
        )
        assert replacement.status_code == 201
        replacement_body = replacement.json()
        replacement_generation = replacement_body["generation"]
        assert replacement_generation != first_generation
        assert replacement_body["open_claim_handoff"] == handoff

        replay = client.post(
            f"/issues/{issue['id']}/yield", json=base, headers=yield_headers
        )
        assert replay.status_code == 201
        assert replay.headers["idempotent-replay"] == "true"
        assert replay.json() == handoff
        current = client.get(f"/issues/{issue['id']}/lease")
        assert current.json()["generation"] == replacement_generation
        assert current.json()["open_claim_handoff"] == handoff
        assert current.headers["cache-control"] == "private, no-store"

        mismatch = client.post(
            f"/issues/{issue['id']}/yield",
            json={**base, "generation": replacement_generation},
            headers=yield_headers,
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "idempotency_mismatch"

        context_response = client.get(
            f"/issues/{issue['id']}/work-context", headers=agent_b
        )
        assert context_response.status_code == 200
        context = context_response.json()
        assert context["claim_handoffs"]["open"] == handoff
        assert context["claim_handoffs"]["items"][0] == handoff
        assert context_response.headers["cache-control"] == "private, no-store"
        inbox = client.get("/delegations/me", headers=agent_b).json()
        delegated = next(item for item in inbox["items"] if item["issue"]["id"] == issue["id"])
        assert delegated["open_claim_handoff"] == handoff
        assert "open_claim_handoff" in delegated["warnings"]
        fleet = client.get("/fleet/active-work", headers=owner).json()
        active = next(item for item in fleet["items"] if item["issue"]["id"] == issue["id"])
        assert active["open_claim_handoff"] == handoff
        assert "open_claim_handoff" in active["attention_reasons"]

        for path in (
            f"/aegis/issues/{issue['id']}",
            f"/aegis/issues/{issue['id']}/work-context",
        ):
            page = client.get(path)
            assert page.status_code == 200
            assert '<script>alert("blocked")</script>' not in page.text
            assert "&lt;script&gt;alert" in page.text
            assert "Untrusted advisory context" in page.text

        resume_url = (
            f"/issues/{issue['id']}/claim-handoffs/"
            f"{handoff['handoff_token']}/resume"
        )
        missing_resume = client.post(resume_url, json={}, headers=agent_b)
        assert missing_resume.status_code == 428
        assert missing_resume.json()["code"] == "lease_generation_required"
        stale_resume = client.post(
            resume_url,
            json={"generation": first_generation},
            headers=agent_b,
        )
        assert stale_resume.status_code == 409
        assert stale_resume.json()["code"] == "lease_generation_mismatch"
        assert replacement_generation not in stale_resume.text

        missing_complete = client.post(
            f"/issues/{issue['id']}/complete", json={}, headers=agent_b
        )
        assert missing_complete.status_code == 428
        bodyless_complete = client.post(
            f"/issues/{issue['id']}/complete", headers=agent_b
        )
        assert bodyless_complete.status_code == 428
        assert bodyless_complete.json()["code"] == "lease_generation_required"
        blocked_complete = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": replacement_generation},
            headers=agent_b,
        )
        assert blocked_complete.status_code == 409
        assert "resume the open claim handoff" in blocked_complete.json()["detail"]

        resumed = client.post(
            resume_url,
            json={
                "generation": replacement_generation,
                "resume_note": "Context received; blocker not asserted resolved.",
            },
            headers=agent_b,
        )
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "resumed"
        assert resumed.headers["cache-control"] == "private, no-store"
        final_context = client.get(
            f"/issues/{issue['id']}/work-context", headers=agent_b
        ).json()
        assert final_context["claim_handoffs"]["open"] is None
        assert final_context["claim_handoffs"]["items"][0]["state"] == "resumed"
        assert client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": replacement_generation},
            headers=agent_b,
        ).status_code == 204
