"""An exact review request is a durable ask; it grants no work or authority."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

from athena.aegis import contributors, issues, leases, review_request_commands
from athena.aegis.issue_commands import IssueCommandError
from athena.core import db, tokens
from athena.main import create_app
from test_review_queue import (
    actor,
    board as _board,
    claim,
    complete,
    issue,
    private,
    projects,
    review,
    state,
    submit,
)

board = _board


def request(conn, check, candidate_id, reviewer_id=3, who=1):
    return review_request_commands.request_candidate_review(
        conn,
        actor=actor(conn, who),
        review_issue_id=check["id"],
        candidate_id=candidate_id,
        reviewer_id=reviewer_id,
    )


def setup_request(conn, project):
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    return build, check, submit(conn, build)["candidate_id"]


def snapshot(conn):
    return {
        **state(conn),
        **{
            name: [tuple(r) for r in conn.execute(f"SELECT * FROM {name}")]
            for name in (
                "issue_review_requests",
                "issues",
                "issue_contributors",
                "project_members",
                "watches",
                "run_bindings",
            )
        },
    }


def refused(conn, kind, call):
    before = snapshot(conn)
    with pytest.raises(IssueCommandError) as caught:
        call()
    assert caught.value.kind == kind
    assert snapshot(conn) == before
    return caught.value.detail


def test_request_dedup_and_fulfillment_do_not_assign_claim_or_close(board):
    conn, _, project = board
    _, check, candidate_id = setup_request(conn, project)
    before = snapshot(conn)
    receipt = request(conn, check, candidate_id)
    assert receipt == {
        "request_id": 1,
        "review_issue_id": check["id"],
        "candidate_id": candidate_id,
        "reviewer_id": 3,
    }
    after = snapshot(conn)
    for name in (
        "issues",
        "issue_contributors",
        "project_members",
        "watches",
        "issue_leases",
        "issue_candidate_reviews",
    ):
        assert after[name] == before[name]
    trail = conn.execute(
        "SELECT * FROM activity WHERE verb='review_requested'"
    ).fetchall()
    assert len(trail) == 1
    assert (trail[0]["actor_id"], trail[0]["target_id"], trail[0]["detail"]) == (
        1,
        check["id"],
        "",
    )
    assert request(conn, check, candidate_id) == receipt
    assert snapshot(conn) == after
    held = claim(conn, check, 3)
    picked = snapshot(conn)
    assert request(conn, check, candidate_id) == receipt
    assert snapshot(conn) == picked
    result = complete(
        conn, check, held, 3, review_candidate_id=candidate_id, verdict="HOLD"
    )
    assert result["candidate_review"]["candidate_id"] == candidate_id
    assert result["candidate_review"]["completion_event_id"] > trail[0]["id"]
    assert issues.get_issue(conn, check["id"])["status"] == check["status"]


def test_no_retargeting_during_possession_even_for_same_digest_or_wrong_worker(board):
    conn, _, project = board
    build, check, first = setup_request(conn, project)
    contributors.add_contributor(conn, check["id"], 4, 1)
    request(conn, check, first)
    other = claim(conn, check, 4)
    refused(
        conn,
        "conflict",
        lambda: complete(conn, check, other, 4, review_candidate_id=first),
    )
    complete(conn, check, other, 4, unfinished=["Release"])
    held = claim(conn, check, 3)
    second = submit(conn, build, first)["candidate_id"]
    assert second != first
    refused(conn, "conflict", lambda: request(conn, check, first))
    refused(conn, "conflict", lambda: request(conn, check, second))
    refused(
        conn,
        "conflict",
        lambda: complete(conn, check, held, 3, review_candidate_id=first),
    )
    refused(
        conn,
        "conflict",
        lambda: complete(conn, check, held, 3, review_candidate_id=second),
    )
    assert leases.get_lease(conn, check["id"])["generation"] == held["generation"]
    complete(conn, check, held, 3, unfinished=["Submission changed"])
    receipt = request(conn, check, second, reviewer_id=4)
    assert receipt["request_id"] == 2
    fresh = claim(conn, check, 4)
    assert complete(conn, check, fresh, 4, review_candidate_id=second)["released"]
    detached = issue(conn, project, assignee=3)
    assert review(conn, detached, second, verdict="PASS")["candidate_id"] == second
    # An earlier exact review is historical; a subsequent request needs a later report.
    later = request(conn, detached, second)
    assert later["request_id"] == 3


def test_only_existing_issue_actor_can_request_eligible_named_worker(board):
    conn, _, project = board
    _, check, target = setup_request(conn, project)
    refused(conn, "forbidden", lambda: request(conn, check, target, who=4))
    assert refused(conn, "not_found", lambda: request(conn, check, 9999)) == refused(
        conn, "not_found", lambda: request(conn, check, target, reviewer_id=9999)
    )
    for unassigned in (1, 2, 4):
        refused(
            conn,
            "conflict",
            lambda: request(conn, check, target, reviewer_id=unassigned),
        )
    contributors.add_contributor(conn, check["id"], 2, 1)
    refused(conn, "conflict", lambda: request(conn, check, target, reviewer_id=2))
    contributors.add_contributor(conn, check["id"], 4, 1)
    request(conn, check, target, who=4)
    refused(
        conn, "conflict", lambda: request(conn, check, target, reviewer_id=4, who=4)
    )
    for column, value, reset, kind in (
        ("role", "viewer", "member", "conflict"),
        ("paused_at", "2026-01-01", None, "conflict"),
        ("removed_at", "2026-01-01", None, "not_found"),
    ):
        conn.execute(f"UPDATE users SET {column}=? WHERE id=3", (value,))
        conn.commit()
        refused(conn, kind, lambda: request(conn, check, target))
        conn.execute(f"UPDATE users SET {column}=? WHERE id=3", (reset,))
        conn.commit()


@pytest.mark.parametrize("field", ["candidate_id", "reviewer_id", "review_issue_id"])
@pytest.mark.parametrize("invalid", [True, 1.0, -1, 0, "1", 2**63])
def test_direct_ids_refuse_coercion_before_write(board, field, invalid):
    conn, _, _ = board
    fields = {"candidate_id": 1, "reviewer_id": 3, "review_issue_id": 2, field: invalid}
    refused(
        conn,
        "invalid",
        lambda: review_request_commands.request_candidate_review(
            conn, actor=actor(conn, 1), **fields
        ),
    )


def test_request_rechecks_live_authority_and_refuses_open_transactions(board):
    conn, _, project = board
    _, check, target = setup_request(conn, project)
    raw = tokens.create_token(conn, user_id=1, name="conductor", scopes=["issue:write"])
    bearer = tokens.resolve_token(conn, raw["token"])

    def call():
        return review_request_commands.request_candidate_review(
            conn,
            actor=bearer,
            review_issue_id=check["id"],
            candidate_id=target,
            reviewer_id=3,
        )

    conn.execute("BEGIN")
    refused(conn, "invalid", call)
    assert conn.in_transaction
    conn.rollback()
    for scopes in ("read", "unrecognized"):
        conn.execute("UPDATE api_tokens SET scopes=? WHERE id=?", (scopes, raw["id"]))
        conn.commit()
        refused(conn, "forbidden", call)
    tokens.revoke_token(conn, user_id=1, token_id=raw["id"])
    refused(conn, "unauthorized", call)
    conn.execute("UPDATE users SET paused_at='now' WHERE id=1")
    conn.commit()
    refused(conn, "forbidden", call)


def test_request_requires_free_open_issue_at_creation(board):
    conn, _, project = board
    build, check, target = setup_request(conn, project)
    held = claim(conn, check, 3)
    refused(conn, "conflict", lambda: request(conn, check, target))
    complete(conn, check, held, 3)
    for sql, params in [
        ("UPDATE issues SET status='done' WHERE id=?", (check["id"],)),
        ("UPDATE issues SET archived_at='now' WHERE id=?", (check["id"],)),
        ("UPDATE issues SET archived_at='now' WHERE id=?", (build["id"],)),
        ("UPDATE issues SET project_id=NULL WHERE id=?", (check["id"],)),
    ]:
        conn.execute(sql, params)
        conn.commit()
        refused(conn, "conflict", lambda: request(conn, check, target))
        conn.execute(
            "UPDATE issues SET archived_at=NULL, status='open', project_id=? WHERE id IN (?,?)",
            (project["id"], build["id"], check["id"]),
        )
        conn.commit()
    refused(conn, "conflict", lambda: request(conn, build, target))
    current = request(conn, check, target)
    another = submit(conn, issue(conn, project))["candidate_id"]
    refused(conn, "conflict", lambda: request(conn, check, another))
    assert request(conn, check, target) == current


@pytest.mark.parametrize("private_event", ["candidate", "request"])
def test_unseen_original_scope_never_allows_prior_binding_comparisons(
    board, private_event
):
    conn, _, public = board
    secret = projects.create_project(conn, name="Secret", key="SEC", created_by=1)
    private(conn, secret, 2, 3)
    origin = secret if private_event == "candidate" else public
    build, check, first = setup_request(conn, origin)
    if private_event == "request":
        for item in (build, check):
            issues.set_project(conn, item["id"], secret["id"])
    request(conn, check, first)
    for item in (build, check):
        issues.set_project(conn, item["id"], public["id"])
    contributors.add_contributor(conn, check["id"], 4, 1)
    second = submit(conn, build, first)["candidate_id"]
    other = submit(conn, issue(conn, public))["candidate_id"]
    refusals = {
        refused(
            conn, "conflict", lambda candidate=c: request(conn, check, candidate, who=4)
        )
        for c in (first, second, other, 99999)
    }
    assert refusals == {"review request is unavailable or changed"}
    assert request(conn, check, second)["candidate_id"] == second


def test_target_visibility_for_both_requester_and_named_reviewer(board):
    conn, _, public = board
    hidden = projects.create_project(conn, name="Secret", key="SEC", created_by=1)
    private(conn, hidden, 2)
    build, check, target = setup_request(conn, hidden)
    for item in (build, check):
        issues.set_project(conn, item["id"], public["id"])
    missing = refused(conn, "not_found", lambda: request(conn, check, 99999))
    assert (
        refused(conn, "not_found", lambda: request(conn, check, target)) == missing
    )  # named reviewer
    assert (
        refused(conn, "not_found", lambda: request(conn, check, target, who=3))
        == missing
    )  # requester
    private(conn, hidden, 3)
    assert request(conn, check, target)["candidate_id"] == target


def test_concurrent_request_is_one_fact_and_multiple_issues_can_target_one_candidate(
    board,
):
    conn, path, project = board
    _, check, target = setup_request(conn, project)
    barrier = Barrier(2)

    def send():
        worker = db.connect(path)
        try:
            barrier.wait(timeout=10)
            return request(worker, check, target)
        finally:
            worker.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert results[0] == results[1]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM activity WHERE verb='review_requested'"
        ).fetchone()[0]
        == 1
    )
    extra = issue(conn, project, assignee=4)
    assert (
        request(conn, extra, target, reviewer_id=4)["request_id"]
        != results[0]["request_id"]
    )
    assert conn.execute("SELECT COUNT(*) FROM issue_review_requests").fetchone()[0] == 2


def test_http_replay_exact_historical_receipt_validation_and_scope(board):
    conn, path, project = board
    build, check, target = setup_request(conn, project)
    writer = tokens.create_token(
        conn, user_id=1, name="conductor", scopes=["issue:write"]
    )
    reader = tokens.create_token(conn, user_id=1, name="reader", scopes=["read"])
    url = f"/issues/{check['id']}/review-requests"
    payload = {"candidate_id": target, "reviewer_id": 3}
    auth = {"Authorization": f"Bearer {writer['token']}", "X-Athena-Run": "ask-once"}
    keyed = {**auth, "Idempotency-Key": "ask-once"}
    with TestClient(create_app(path)) as client:
        assert client.post(url, json=payload).status_code == 401
        assert (
            client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {reader['token']}"},
            ).status_code
            == 403
        )
        for malformed in (
            {"candidate_id": "1", "reviewer_id": 3},
            {**payload, "unknown": 9},
            {"candidate_id": target},
            {"candidate_id": True, "reviewer_id": 3},
        ):
            assert client.post(url, json=malformed, headers=auth).status_code == 422
        first = client.post(url, json=payload, headers=keyed)
        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "private, no-store"
        assert first.json() == {
            "request_id": 1,
            "candidate_id": target,
            "reviewer_id": 3,
            "review_issue_id": check["id"],
        }
        assert (
            conn.execute(
                "SELECT run_id FROM activity WHERE verb='review_requested'"
            ).fetchone()[0]
            == "ask-once"
        )
        submit(conn, build, target)
        replay = client.post(url, json=payload, headers=keyed)
        assert (replay.status_code, replay.json()) == (200, first.json())
        assert client.post(url, json=payload, headers=auth).status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM issue_review_requests").fetchone()[0] == 1
