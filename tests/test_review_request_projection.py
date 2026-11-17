"""A review request is recoverable only from the facts the caller may see."""

from datetime import UTC, datetime, timedelta

import pytest

from athena.aegis import desk, issues, projects, review_request_projection, work_context
from athena.aegis.review_request_commands import request_candidate_review
from athena.aegis.review_queue_models import ProjectReviewQueue, ReviewRequest
from test_review_queue import (
    actor,
    board as _board,
    claim,
    complete,
    issue,
    private,
    queue,
    review,
    submit,
)

board = _board


def request(conn, check, candidate, reviewer=3):
    return request_candidate_review(
        conn,
        actor=actor(conn, 1),
        review_issue_id=check["id"],
        candidate_id=candidate["candidate_id"],
        reviewer_id=reviewer,
    )


def context(conn, check, who=3, *, brief=False):
    return work_context.build_work_context(
        conn, str(check["id"]), actor=actor(conn, who), brief=brief
    )


def desk_group(conn, who=3, *, now=None):
    return desk.build_desk(conn, actor=actor(conn, who), now=now)["asks"].get(
        "review_requests"
    )


def queue_group(conn, project, who=3):
    packet = queue(conn, project, who=who)
    validated = ProjectReviewQueue.model_validate(packet).model_dump(by_alias=True)
    assert validated == packet
    return packet["items"][0].get("review_requests")


def test_request_is_one_exact_visible_item_across_reads_and_never_a_verdict(board):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    candidate = submit(conn, build)
    assert desk_group(conn) is None and queue_group(conn, project) is None
    assert "review_request" not in context(conn, check)
    receipt = request(conn, check, candidate)
    item = {
        **receipt,
        "status": "requested",
        "reviewer_holds_active_lease": False,
        "identity": {"kind": "git", "source": "owner/repository", "revision": "a" * 40},
        "source_issue_id": build["id"],
        "completion_event_id": candidate["completion_event_id"],
        "source_work_context_url": f"/issues/{build['id']}/work-context",
    }
    assert ReviewRequest.model_validate(item).model_dump() == item
    assert context(conn, check)["review_request"] == item
    assert "review_request" not in context(conn, check, brief=True)
    assert desk_group(conn)["items"] == queue_group(conn, project)["items"] == [item]
    assert desk_group(conn, who=2) is None
    source = context(conn, build)
    assert "review_request" not in source
    assert "review_requests" not in source["review_tracking"]["current_candidate"]
    assert (
        source["review_tracking"]["current_candidate"]["review_state"]
        == "awaiting_review"
    )
    held = claim(conn, check, 3)
    picked = {**item, "status": "picked_up", "reviewer_holds_active_lease": True}
    assert desk_group(conn)["items"] == [picked]
    assert context(conn, check)["review_request"] == picked
    assert queue_group(conn, project)["items"] == [picked]
    assert desk_group(conn, now=datetime.now(UTC) + timedelta(days=8))["items"] == [
        item
    ]
    complete(conn, check, held, 3)
    assert desk_group(conn)["items"] == [item]  # ordinary release answers no review
    review(conn, check, candidate["candidate_id"], verdict="HOLD")
    fulfilled = {**item, "status": "fulfilled"}
    assert context(conn, check)["review_request"] == fulfilled
    assert queue_group(conn, project)["items"] == [fulfilled]
    assert desk_group(conn) is None
    submit(conn, build, candidate["candidate_id"])  # same identity, distinct submission
    assert context(conn, check)["review_request"] == fulfilled
    assert queue_group(conn, project) is None


def test_superseded_target_is_on_held_reviewers_desk_until_release(board):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    first = submit(conn, build)
    request(conn, check, first)
    held = claim(conn, check, 3)
    replacement = submit(conn, build, first["candidate_id"])
    item = context(conn, check)["review_request"]
    assert (
        item["candidate_id"],
        item["status"],
        item["reviewer_holds_active_lease"],
    ) == (first["candidate_id"], "superseded", True)
    assert desk_group(conn)["items"] == [item]
    assert queue_group(conn, project) is None
    complete(conn, check, held, 3)
    assert desk_group(conn) is None
    assert context(conn, check)["review_request"] == {
        **item,
        "reviewer_holds_active_lease": False,
    }
    request(conn, check, replacement)
    assert [row["candidate_id"] for row in desk_group(conn)["items"]] == [
        replacement["candidate_id"]
    ]


def test_hidden_newest_fulfillment_has_same_status_and_counts_as_no_fulfillment(board):
    conn, _, public = board
    other = projects.create_project(conn, name="Temporary", key="TEMP", created_by=1)
    build, check = issue(conn, public), issue(conn, public, assignee=3)
    candidate = submit(conn, build)
    request(conn, check, candidate)

    def projections():
        return (
            desk_group(conn),
            queue_group(conn, public),
            context(conn, check).get("review_request"),
        )

    without_completion = projections()
    # Earlier visible completion must not resurrect a verdict/status.
    review(conn, check, candidate["candidate_id"], verdict="PASS")
    assert desk_group(conn) is None
    for item in (build, check):
        issues.set_project(conn, item["id"], other["id"])
    review(conn, check, candidate["candidate_id"], verdict="HOLD")
    for item in (build, check):
        issues.set_project(conn, item["id"], public["id"])
    private(conn, other)
    assert projections() == without_completion
    assert context(conn, check, who=1)["review_request"]["status"] == "fulfilled"


@pytest.mark.parametrize(
    "hidden", ["request_event", "candidate_event", "source_root", "review_root"]
)
def test_missing_visibility_omits_request_without_counts(board, hidden):
    conn, _, public = board
    other = projects.create_project(conn, name="Other", key="OTH", created_by=1)
    origin = other if hidden == "candidate_event" else public
    build, check = issue(conn, origin), issue(conn, public, assignee=3)
    candidate = submit(conn, build)
    if hidden == "candidate_event":
        issues.set_project(conn, build["id"], public["id"])
    if hidden == "request_event":
        for item in (build, check):
            issues.set_project(conn, item["id"], other["id"])
    request(conn, check, candidate)
    if hidden == "request_event":
        for item in (build, check):
            issues.set_project(conn, item["id"], public["id"])
    if hidden in ("source_root", "review_root"):
        item = build if hidden == "source_root" else check
        issues.set_project(conn, item["id"], other["id"])
    private(conn, other)
    assert desk_group(conn) is None
    packet = queue(conn, public, who=3)
    assert all("review_requests" not in entry for entry in packet["items"])
    visible_context = context(conn, check)
    assert visible_context is None or "review_request" not in visible_context
    assert context(conn, check, who=1)["review_request"]["status"] == "requested"


def test_hidden_successor_and_hidden_latest_request_never_fall_back(board):
    conn, _, public = board
    other = projects.create_project(conn, name="Other", key="OTH", created_by=1)
    build, check = issue(conn, public), issue(conn, public, assignee=3)
    first = submit(conn, build)
    request(conn, check, first)
    held = claim(conn, check, 3)
    issues.set_project(conn, build["id"], other["id"])
    second = submit(conn, build, first["candidate_id"], "b" * 40)
    issues.set_project(conn, build["id"], public["id"])
    private(conn, other)
    assert desk_group(conn) is None
    assert "review_request" not in context(conn, check)
    assert context(conn, check, who=1)["review_request"]["status"] == "superseded"
    complete(conn, check, held, 3)
    # Make the successor visible but keep the new request's own event hidden.
    private(conn, other, 3)
    third = projects.create_project(conn, name="Third", key="THR", created_by=1)
    for item in (build, check):
        issues.set_project(conn, item["id"], third["id"])
    request(conn, check, second)
    for item in (build, check):
        issues.set_project(conn, item["id"], public["id"])
    private(conn, third)
    assert queue_group(conn, public) is None
    assert desk_group(conn) is None
    assert "review_request" not in context(conn, check)
    assert (
        context(conn, check, who=1)["review_request"]["candidate_id"]
        == second["candidate_id"]
    )


def test_limit_applies_after_visibility_and_desk_relevance(board, monkeypatch):
    conn, _, public = board
    hidden = projects.create_project(conn, name="Other", key="OTH", created_by=1)
    build = issue(conn, public)
    candidate = submit(conn, build)
    visible_ids = []
    for _ in range(22):
        check = issue(conn, public, assignee=3)
        visible_ids.append(request(conn, check, candidate)["request_id"])
    hidden_check = issue(conn, public, assignee=3)
    request(conn, hidden_check, candidate)
    issues.set_project(conn, hidden_check["id"], hidden["id"])
    private(conn, hidden)
    # This one stays in the queue, but it must not displace a desk row.
    answered_check = issue(conn, public, assignee=3)
    answered_id = request(conn, answered_check, candidate)["request_id"]
    review(conn, answered_check, candidate["candidate_id"], verdict="PASS")
    materialized = []
    original = review_request_projection._item

    def count_items(row):
        materialized.append(row["request_id"])
        return original(row)

    monkeypatch.setattr(review_request_projection, "_item", count_items)
    before = conn.total_changes
    for read, ids, total, limit in [
        (lambda: desk_group(conn), visible_ids, 22, 20),
        (lambda: queue_group(conn, public), [*visible_ids, answered_id], 23, 5),
    ]:
        materialized.clear()
        queries = []
        conn.set_trace_callback(queries.append)
        try:
            group = read()
        finally:
            conn.set_trace_callback(None)
        assert group["limit"] == group["returned"] == limit and group["clipped"]
        assert group["visible_total"] == total
        assert (
            [row["request_id"] for row in group["items"]]
            == list(reversed(ids))[:limit]
            == materialized
        )
        count, page = [query for query in queries if "WITH visible_events AS" in query]
        assert count.startswith("SELECT COUNT(*) AS n ")
        assert page.rstrip().endswith(f"ORDER BY request_id DESC LIMIT {limit}")
        # A window count can spool every full identity in SQLite despite a
        # fixed Python limit. Count separately and stream the page via an index.
        for statement in (count, page):
            plan = [
                step["detail"]
                for step in conn.execute("EXPLAIN QUERY PLAN " + statement)
            ]
            assert not any(
                "TEMP B-TREE" in step or "MATERIALIZE" in step for step in plan
            )

    assert conn.total_changes == before


def test_http_self_desk_queue_full_and_brief_contract(board):
    from fastapi.testclient import TestClient

    from athena.main import create_app

    conn, path, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    candidate = submit(conn, build)
    headers = {"X-Athena-Actor": "3"}
    url = f"/issues/{check['id']}/work-context"
    with TestClient(create_app(path)) as client:
        ordinary = client.get(url, headers=headers)
        brief = client.get(url + "?brief=true", headers=headers)
        assert ordinary.status_code == brief.status_code == 200
        assert "review_request" not in ordinary.json()
        receipt = request(conn, check, candidate)
        full = client.get(url, headers=headers)
        assert full.status_code == 200
        item = full.json()["review_request"]
        assert item["request_id"] == receipt["request_id"]
        assert full.headers["etag"] != ordinary.headers["etag"]
        brief_again = client.get(url + "?brief=true", headers=headers)
        assert brief_again.content == brief.content
        assert brief_again.headers["etag"] == brief.headers["etag"]
        assert client.get("/desk", headers=headers).json()["asks"]["review_requests"][
            "items"
        ] == [item]
        packet = client.get(f"/projects/{project['id']}/review-queue", headers=headers)
        assert packet.json()["items"][0]["review_requests"]["items"] == [item]
        other = claim(conn, check, 1)
        assert client.get(url, headers=headers).json()["review_request"] == item
        complete(conn, check, other, 1)
