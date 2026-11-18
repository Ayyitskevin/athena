"""Fixed-limit read bounds, native guards, and precise visibility/clipping."""

import ast
import json

import pytest

from athena.aegis import issues, lease_commands, projects, review_queue
from test_review_queue import (
    board as _board,
    actor,
    issue,
    private,
    queue,
    review,
    state,
    submit,
)
import test_imported_at_guards as guard_tests

board = _board


@pytest.mark.parametrize("limit", [0, 21, True, "1", 1.0])
def test_reader_limit_does_not_coerce_invalid_values(board, limit):
    conn, _, project = board
    with pytest.raises(ValueError):
        queue(conn, project, limit=limit)


def test_queue_materializes_and_parses_only_fixed_limit_despite_history_growth(
    board, monkeypatch
):
    conn, _, project = board
    check = issue(conn, project, assignee=3)
    builds = [issue(conn, project) for _ in range(8)]
    candidates = []
    for build in builds:
        candidate = submit(conn, build)
        candidates.append(candidate)
        review(conn, check, candidate["candidate_id"], verdict="PASS")
    parses, statements = [], []
    original = lease_commands.reported_completion

    def observe(row):
        parses.append(row)
        return original(row)

    monkeypatch.setattr(lease_commands, "reported_completion", observe)

    def measure():
        parses.clear()
        statements.clear()
        conn.set_trace_callback(statements.append)
        try:
            packet = queue(conn, project, limit=2)
        finally:
            conn.set_trace_callback(None)
        assert (
            packet["visible_total"] == 8
            and packet["returned"] == 2
            and packet["clipped"]
        )
        assert len(parses) <= 2
        assert any(
            "ORDER BY c.id DESC, i.id LIMIT 3" in statement for statement in statements
        )
        return len(statements)

    first_count = measure()
    other = projects.create_project(conn, name="Other", key="OTH", created_by=1)
    for _ in range(30):
        submit(conn, issue(conn, other))
    # Unselected report history must not be eagerly parsed either.
    for _ in range(25):
        review(conn, check, candidates[0]["candidate_id"], verdict="HOLD")
    assert measure() == first_count


def test_read_is_pure_and_excerpts_do_not_leak_full_names_or_raw_report(board):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    candidate = submit(conn, build)
    review(
        conn,
        check,
        candidate["candidate_id"],
        verdict="PASS",
        unfinished=["PRIVATE EVIDENCE VALUE"],
    )
    long = "x" * 10_000
    issues.update_issue(conn, build["id"], title=long, status="z" * 300)
    issues.update_issue(conn, check["id"], title=long)
    conn.execute("UPDATE users SET name=? WHERE id IN (2,3)", (long,))
    conn.execute("UPDATE projects SET name=? WHERE id=?", (long, project["id"]))
    conn.commit()
    before = state(conn)
    packet = queue(conn, project)
    row = packet["items"][0]
    assert row["issue"]["title"] == {
        "text": "x" * 160,
        "total_chars": 10000,
        "clipped": True,
    }
    assert row["issue"]["status_category"] is None
    assert row["candidate"]["submitted_by"]["name"]["clipped"]
    assert row["latest_review"]["review_issue"]["title"]["clipped"]
    assert packet["project"]["name"]["clipped"]
    assert long not in json.dumps(
        packet
    ) and "PRIVATE EVIDENCE VALUE" not in json.dumps(packet)
    assert state(conn) == before


def test_latest_review_current_root_visibility_required_after_move(board):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    candidate = submit(conn, build)
    review(conn, check, candidate["candidate_id"], verdict="PASS")
    hidden = projects.create_project(conn, name="Hidden", key="HID", created_by=1)
    private(conn, hidden)
    issues.set_project(conn, check["id"], hidden["id"])
    assert queue(conn, project)["items"][0]["latest_review"] is None
    private(conn, hidden, 2)
    assert queue(conn, project)["items"][0]["review_state"] == "reported_pass"


def test_missing_and_hidden_project_have_same_reader_shape(board):
    conn, _, project = board
    private(conn, project)
    assert queue(conn, project) is None
    assert (
        review_queue.build_project_review_queue(
            conn, project_id=999999, actor=actor(conn)
        )
        is None
    )
    assert queue(conn, project, who=1)["items"] == []


@pytest.mark.parametrize(
    "relative,function",
    [
        ("aegis/review_commands.py", "visible_candidate"),
        ("aegis/review_commands.py", "latest_visible_request"),
        ("aegis/review_request_projection.py", "visible_requests"),
        ("aegis/review_queue.py", "_candidate_base"),
        ("aegis/review_queue.py", "_latest_review"),
    ],
)
def test_native_reader_guard_loss_is_detected(tmp_path, relative, function):
    package = guard_tests._tree_copy(tmp_path)
    target = package / relative
    source = target.read_text()
    node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == function
    )
    lines = source.splitlines(keepends=True)
    body = "".join(lines[node.lineno - 1 : node.end_lineno])
    assert body.count("a.imported_at IS NULL") == 1
    lines[node.lineno - 1 : node.end_lineno] = [
        body.replace("a.imported_at IS NULL", "1 = 1")
    ]
    target.write_text("".join(lines))
    result = guard_tests._run(package)
    assert result.returncode == 1
    assert f"{relative}::{function} lost its guard" in result.stderr
