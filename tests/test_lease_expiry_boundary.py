"""A lease can expire while its writer transaction still owns the database.

The real SQLite generation trigger must keep refusing resurrection; callers need
the same typed refusal as a renewal that was already expired when first read.
"""

import sqlite3

from fastapi.testclient import TestClient
import pytest

from athena.aegis import issue_commands, issue_etags, issues, lease_commands, leases
from athena.core import activity, db
from athena.main import create_app

# The expiry is crossed by the app's one clock (the ``clock`` fixture, G6): the
# lease code reads it, and SQLite's ``datetime('now')`` in the generation
# trigger reads it too while it is replaced. Every date expression is still
# evaluated by SQLite, so the interleaving is independent of CPU scheduling and
# the suite never sleeps.


def _seed(conn):
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('lease@e.com','Owner','admin')"
    )
    conn.commit()
    actor = dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone())
    issue = issues.create_issue(
        conn, title="expiry boundary", body="work", created_by=1, commit=True
    )
    return actor, issue["id"]


def _tag(conn, issue_id):
    return issue_etags.current_etag(conn, issues.get_issue(conn, issue_id))


def _row(conn, issue_id):
    row = conn.execute(
        "SELECT * FROM issue_leases WHERE issue_id=?", (issue_id,)
    ).fetchone()
    return None if row is None else dict(row)


@pytest.mark.parametrize("transport", ["command", "REST"])
def test_renewal_crossing_expiry_is_a_typed_refusal_with_no_write(
    clock, tmp_path, monkeypatch, transport
):
    db_file = tmp_path / "expiry.db"
    with TestClient(create_app(db_file), raise_server_exceptions=False) as client:
        conn = db.connect(db_file)
        try:
            actor, issue_id = _seed(conn)
            tag = _tag(conn, issue_id)
            kwargs = {"actor": actor, "issue_id": issue_id, "if_match": [tag]}
            first = lease_commands.claim_issue(conn, **kwargs, lease_seconds=60)
            generation = first["generation"]
            # Allowed control: active renewal keeps the existing generation.
            renewed = lease_commands.claim_issue(
                conn, **kwargs, generation=generation, lease_seconds=60
            )
            assert renewed["generation"] == generation and renewed["active"]
            before = _row(conn, issue_id)
            events_before = activity.list_activity(
                conn, target_kind="issue", target_id=issue_id
            )
            original_upsert = leases.upsert_lease

            def expire_before_upsert(db_conn, *args, **write_kwargs):
                assert write_kwargs["generation"] == generation
                assert leases.get_lease(db_conn, issue_id)["active"] is True
                clock.advance(seconds=60)
                assert leases.get_lease(db_conn, issue_id)["active"] is False
                return original_upsert(db_conn, *args, **write_kwargs)

            with monkeypatch.context() as boundary:
                boundary.setattr(leases, "upsert_lease", expire_before_upsert)
                if transport == "command":
                    with pytest.raises(issue_commands.IssueCommandError) as refused:
                        lease_commands.claim_issue(
                            conn, **kwargs, generation=generation
                        )
                    assert refused.value.kind == "lease_generation_mismatch"
                else:
                    response = client.post(
                        f"/issues/{issue_id}/claim",
                        headers={"X-Athena-Actor": "1", "If-Match": tag},
                        json={"generation": generation},
                    )
                    assert response.status_code == 409, response.text
                    assert response.json()["code"] == "lease_generation_mismatch"

            assert _row(conn, issue_id) == before
            assert leases.get_lease(conn, issue_id)["active"] is False
            assert (
                activity.list_activity(conn, target_kind="issue", target_id=issue_id)
                == events_before
            )
            # The old token stays expired; only an explicit acquisition replaces it.
            with pytest.raises(issue_commands.IssueCommandError) as expired:
                lease_commands.claim_issue(conn, **kwargs, generation=generation)
            assert expired.value.kind == "lease_generation_mismatch"
            reacquired = lease_commands.claim_issue(conn, **kwargs)
            assert reacquired["active"] is True
            assert reacquired["generation"] != generation
        finally:
            conn.close()


@pytest.mark.parametrize(
    ("renew", "message"),
    [
        (True, "unrelated constraint failure"),
        (True, "reactivated issue lease requires a new generation: unrelated suffix"),
        (False, "reactivated issue lease requires a new generation"),
    ],
)
def test_other_integrity_failures_are_not_misreported_as_expiry(
    tmp_path, monkeypatch, renew, message
):
    conn = db.connect(tmp_path / "unrelated.db")
    try:
        db.migrate(conn)
        actor, issue_id = _seed(conn)
        kwargs = {
            "actor": actor,
            "issue_id": issue_id,
            "if_match": [_tag(conn, issue_id)],
        }
        if renew:
            kwargs["generation"] = lease_commands.claim_issue(conn, **kwargs)[
                "generation"
            ]
        before = _row(conn, issue_id)
        events_before = activity.list_activity(
            conn, target_kind="issue", target_id=issue_id
        )
        error = sqlite3.IntegrityError(message)

        def fail_write(*args, **kwargs):
            raise error

        monkeypatch.setattr(leases, "upsert_lease", fail_write)
        with pytest.raises(sqlite3.IntegrityError) as raised:
            lease_commands.claim_issue(conn, **kwargs)
        assert raised.value is error
        assert _row(conn, issue_id) == before
        assert (
            activity.list_activity(conn, target_kind="issue", target_id=issue_id)
            == events_before
        )
    finally:
        conn.close()
