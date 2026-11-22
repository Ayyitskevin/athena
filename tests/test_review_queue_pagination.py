"""Traverse the current, actor-visible review queue across independent reads."""

import pytest

from athena.aegis import issues, projects
from test_review_queue import board as _board, issue, private, queue, review, submit

board = _board


def candidate_ids(packet):
    return [row["candidate"]["candidate_id"] for row in packet["items"]]


@pytest.mark.parametrize("invalid", [0, -1, 2**63, True, "1", 1.0])
def test_queue_rejects_invalid_cursor(board, invalid):
    conn, _, project = board
    with pytest.raises(ValueError):
        queue(conn, project, cursor=invalid)


def test_exclusive_cursor_traverses_exact_size_final_page_and_oldest_hold(board):
    conn, _, project = board
    oldest = submit(conn, issue(conn, project))
    check = issue(conn, project, assignee=3)
    held = review(conn, check, oldest["candidate_id"], verdict="HOLD")
    ids = [oldest["candidate_id"]]
    ids.extend(submit(conn, issue(conn, project))["candidate_id"] for _ in range(9))

    first = queue(conn, project)
    final = queue(conn, project, cursor=first["next_cursor"])
    assert candidate_ids(first) + candidate_ids(final) == ids[::-1]
    assert first["next_cursor"] == candidate_ids(first)[-1]
    assert final["next_cursor"] is None
    for page in (first, final):
        assert page["visible_total"] == 10
        assert page["returned"] == page["limit"] == 5
        assert page["clipped"]
    row = final["items"][-1]
    assert row["review_state"] == "reported_hold"
    assert row["candidate"]["completion_event_id"] == oldest["completion_event_id"]
    assert row["latest_review"]["completion_event_id"] == held["completion_event_id"]

    empty = queue(conn, project, cursor=oldest["candidate_id"])
    assert empty["returned"] == 0 and empty["items"] == []
    assert empty["next_cursor"] is None
    assert empty["visible_total"] == 10 and empty["clipped"]
    assert candidate_ids(queue(conn, project, cursor=2**63 - 1)) == candidate_ids(first)


def test_resume_rechecks_canonical_submission_and_original_event_visibility(board):
    conn, _, project = board
    other = projects.create_project(
        conn, name="Past work", key="PAST", created_by=1, commit=True
    )
    visible = submit(conn, issue(conn, project))
    moved = issue(conn, project)
    original = submit(conn, moved)
    issues.set_project(conn, moved["id"], other["id"], commit=True)
    latest = submit(conn, moved, original["candidate_id"])
    issues.set_project(conn, moved["id"], project["id"], commit=True)
    replaced = issue(conn, project)
    stale = submit(conn, replaced)
    anchor = issue(conn, project)
    boundary = submit(conn, anchor)["candidate_id"]

    first = queue(conn, project, limit=1)
    assert first["visible_total"] == 4 and first["next_cursor"] == boundary
    # The cursor remains a numeric boundary even after its issue leaves this project.
    issues.set_project(conn, anchor["id"], other["id"], commit=True)
    new = submit(conn, replaced, stale["candidate_id"])
    private(conn, other)

    remaining = queue(conn, project, cursor=boundary)
    assert remaining["visible_total"] == 2 and remaining["next_cursor"] is None
    assert candidate_ids(remaining) == [visible["candidate_id"]]
    assert candidate_ids(queue(conn, project)) == [
        new["candidate_id"],
        visible["candidate_id"],
    ]
    assert candidate_ids(queue(conn, project, who=1, cursor=boundary)) == [
        latest["candidate_id"],
        visible["candidate_id"],
    ]
