"""Reviews describe one immutable submission, never a later matching identity."""

import json

import pytest
from support import lapse_lease

from athena.aegis import issue_etags, issues, lease_commands, projects
from athena.core import db


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "reviews.db"
    conn = db.connect(path)
    db.migrate(conn)
    for number, role in [(1, "admin"), (2, "member"), (3, "member"), (4, "member")]:
        conn.execute(
            "INSERT INTO users(id,email,name,role) VALUES (?,?,?,?)",
            (number, f"actor{number}@example.invalid", f"Actor {number}", role),
        )
    conn.commit()
    project = projects.create_project(
        conn, name="Review", key="RVW", created_by=1, commit=True
    )
    yield conn, path, project
    conn.close()


def actor(conn, actor_id=2):
    return dict(conn.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone())


def issue(conn, project, *, assignee=2, **fields):
    row = issues.create_issue(
        conn,
        title="Work",
        body="Do not expose raw body",
        created_by=1,
        project_id=project["id"] if project is not None else None,
        **fields,
        commit=True,
    )
    return issues.set_assignee(conn, row["id"], assignee, commit=True)


def claim(conn, item, actor_id=2):
    current = issues.get_issue(conn, item["id"])
    return lease_commands.claim_issue(
        conn,
        actor=actor(conn, actor_id),
        issue_id=item["id"],
        if_match=[issue_etags.current_etag(conn, current)],
    )


def complete(conn, item, held, actor_id=2, **fields):
    return lease_commands.complete_claim(
        conn,
        actor=actor(conn, actor_id),
        issue_id=item["id"],
        generation=held["generation"],
        **fields,
    )


def submission(previous=None, revision="a" * 40):
    return {
        "identity": {"kind": "git", "source": "owner/repository", "revision": revision},
        "expected_previous_candidate_id": previous,
    }


def submit(conn, item, previous=None, revision="a" * 40):
    return complete(
        conn,
        item,
        claim(conn, item),
        candidate_submission=submission(previous, revision),
    )["candidate_submission"]


def review(conn, item, candidate, **report):
    return complete(
        conn, item, claim(conn, item, 3), 3, review_candidate_id=candidate, **report
    )["candidate_review"]


def queue(conn, project, who=2, **kwargs):
    from athena.aegis.review_queue import build_project_review_queue

    return build_project_review_queue(
        conn, project_id=project["id"], actor=actor(conn, who), **kwargs
    )


def test_exact_version_loop_keeps_old_pass_historical(board):
    from athena.aegis.work_context import build_work_context

    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    first = submit(conn, build)
    linked = review(
        conn, check, first["candidate_id"], verdict="PASS", findings={"P2": 0}
    )
    row = queue(conn, project)["items"][0]
    assert row["review_state"] == "reported_pass"
    assert row["latest_review"]["completion_event_id"] == linked["completion_event_id"]
    second = submit(conn, build, first["candidate_id"], "b" * 40)
    row = queue(conn, project)["items"][0]
    assert row["candidate"]["candidate_id"] == second["candidate_id"]
    assert row["review_state"] == "awaiting_review" and row["latest_review"] is None
    context = build_work_context(conn, str(build["id"]), actor=actor(conn))
    history = context["review_tracking"]["recent_candidates"]["items"]
    assert [x["is_current"] for x in history] == [True, False]
    assert history[1]["review_state"] == "reported_pass"
    assert "Do not expose raw body" not in json.dumps(queue(conn, project))


def state(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
        for table in (
            "issue_leases",
            "activity",
            "activity_chain",
            "notifications",
            "issue_review_candidates",
            "issue_candidate_reviews",
        )
    }


def refuse(conn, kind, call):
    from athena.aegis.issue_commands import IssueCommandError

    before = state(conn)
    with pytest.raises(IssueCommandError) as caught:
        call()
    assert caught.value.kind == kind
    assert state(conn) == before
    return caught.value.detail


@pytest.mark.parametrize("revision", ["a" * 40, "b" * 40])
def test_equal_and_returned_identities_are_new_submissions(board, revision):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    first = submit(conn, build)
    review(conn, check, first["candidate_id"], verdict="PASS")
    second = submit(conn, build, first["candidate_id"], revision)
    third = submit(conn, build, second["candidate_id"])
    assert first["candidate_id"] < second["candidate_id"] < third["candidate_id"]
    assert queue(conn, project)["items"][0]["review_state"] == "awaiting_review"


@pytest.mark.parametrize(
    "report,expected",
    [
        ({}, "unknown"),
        ({"findings": {"P2": 2}}, "unknown"),
        ({"verdict": "HOLD"}, "reported_hold"),
        ({"verdict": "BLOCKED"}, "reported_hold"),
        ({"verdict": "PASS-WITH-NOTES"}, "reported_pass"),
    ],
)
def test_latest_report_never_borrows_previous_pass_or_missing_counts(
    board, report, expected
):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    candidate = submit(conn, build)
    review(conn, check, candidate["candidate_id"], verdict="PASS", findings={"P1": 0})
    newest = review(conn, check, candidate["candidate_id"], **report)
    row = queue(conn, project)["items"][0]
    assert row["review_state"] == expected
    assert row["latest_review"]["completion_event_id"] == newest["completion_event_id"]
    assert row["latest_review"]["reported_findings"] == report.get("findings")


def test_stale_review_refuses_without_releasing_reviewer(board):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    first = submit(conn, build)
    held = claim(conn, check, 3)
    submit(conn, build, first["candidate_id"], "b" * 40)
    refuse(
        conn,
        "conflict",
        lambda: complete(
            conn,
            check,
            held,
            3,
            review_candidate_id=first["candidate_id"],
            verdict="PASS",
        ),
    )
    # Historical/unfinished work is still reportable through ordinary completion.
    result = complete(
        conn, check, held, 3, unfinished=["Candidate replaced while reviewing"]
    )
    assert result["released"] and "candidate_review" not in result


@pytest.mark.parametrize("bad", [0, -1, 2**63, True, "1", 1.0])
def test_command_review_ids_are_strict_before_writes(board, bad):
    conn, _, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    refuse(
        conn, "invalid", lambda: complete(conn, build, held, review_candidate_id=bad)
    )


def test_mutually_exclusive_modes_and_explicit_predecessor(board):
    conn, _, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    refuse(
        conn,
        "invalid",
        lambda: complete(
            conn, build, held, candidate_submission=submission(), review_candidate_id=1
        ),
    )
    missing = submission()
    del missing["expected_previous_candidate_id"]
    refuse(
        conn,
        "invalid",
        lambda: complete(conn, build, held, candidate_submission=missing),
    )
    result = complete(
        conn, build, held, candidate_submission=None, review_candidate_id=None
    )
    assert set(result) == {
        "released",
        "issue_id",
        "issue_status",
        "issue_still_open",
        "lapsed",
        "next",
    }


@pytest.mark.parametrize(
    "case,kind",
    [
        ("wrong_generation", "lease_generation_mismatch"),
        ("expired", "conflict"),
        ("admin_other", "conflict"),
        ("archived", "conflict"),
        ("backlog", "conflict"),
        ("wrong_predecessor", "conflict"),
        ("nested", "invalid"),
    ],
)
def test_submission_authority_refusals_retain_all_state(clock, board, case, kind):
    conn, path, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    actor_id, payload = 2, submission()
    if case == "wrong_generation":
        held = {**held, "generation": "0" * 32}
    elif case == "expired":
        lapse_lease(clock, path, build["id"])
    elif case == "admin_other":
        actor_id = 1
    elif case == "archived":
        issues.set_archived(conn, build["id"], True, commit=True)
    elif case == "backlog":
        issues.set_project(conn, build["id"], None, commit=True)
    elif case == "wrong_predecessor":
        payload = submission(9999)
    elif case == "nested":
        conn.execute("BEGIN")
    refuse(
        conn,
        kind,
        lambda: complete(conn, build, held, actor_id, candidate_submission=payload),
    )
    if case == "nested":
        assert conn.in_transaction
        conn.rollback()
    if case in {"expired", "admin_other"}:
        assert complete(conn, build, held, actor_id)["released"]


@pytest.mark.parametrize(
    "case",
    [
        "same_account",
        "same_issue",
        "cross_project",
        "source_archived",
        "review_archived",
        "backlog",
    ],
)
def test_review_requires_distinct_accounts_and_current_project_pair(board, case):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    candidate = submit(conn, build)
    actor_id = 3
    if case == "same_account":
        actor_id = 2
        issues.set_assignee(conn, check["id"], 2, commit=True)
    elif case == "same_issue":
        check = build
        issues.set_assignee(conn, check["id"], 3, commit=True)
    held = claim(conn, check, actor_id)
    if case == "cross_project":
        other = projects.create_project(
            conn, name="Other", key="OTH", created_by=1, commit=True
        )
        issues.set_project(conn, build["id"], other["id"], commit=True)
    elif case == "source_archived":
        issues.set_archived(conn, build["id"], True, commit=True)
    elif case == "review_archived":
        issues.set_archived(conn, check["id"], True, commit=True)
    elif case == "backlog":
        issues.set_project(conn, check["id"], None, commit=True)
        issues.set_project(conn, build["id"], None, commit=True)
    refuse(
        conn,
        "conflict",
        lambda: complete(
            conn, check, held, actor_id, review_candidate_id=candidate["candidate_id"]
        ),
    )


def private(conn, project, *members):
    projects.set_visibility(conn, project["id"], "private", commit=True)
    for member in members:
        conn.execute(
            "INSERT OR IGNORE INTO project_members(project_id,user_id) VALUES (?,?)",
            (project["id"], member),
        )
    conn.commit()


def test_hidden_predecessor_correct_wrong_null_refuse_identically_before_cas(board):
    from athena.aegis.work_context import build_work_context

    conn, _, original = board
    public = projects.create_project(
        conn, name="Public", key="PUB", created_by=1, commit=True
    )
    build = issue(conn, original)
    first = submit(conn, build)
    private(conn, original)
    issues.set_project(conn, build["id"], public["id"], commit=True)
    held = claim(conn, build)
    details = [
        refuse(
            conn,
            "conflict",
            lambda previous=previous: complete(
                conn, build, held, candidate_submission=submission(previous)
            ),
        )
        for previous in (first["candidate_id"], 9999, None)
    ]
    assert len(set(details)) == 1
    assert queue(conn, public)["visible_total"] == 0
    assert "review_tracking" not in build_work_context(
        conn, str(build["id"]), actor=actor(conn)
    )
    private(conn, original, 2)
    second = complete(
        conn, build, held, candidate_submission=submission(first["candidate_id"])
    )["candidate_submission"]
    assert (
        queue(conn, public)["items"][0]["candidate"]["candidate_id"]
        == second["candidate_id"]
    )


def test_hidden_newest_candidate_never_promotes_old_visible_one(board):
    from athena.aegis.work_context import build_work_context

    conn, _, public = board
    hidden = projects.create_project(
        conn, name="Hidden", key="HID", created_by=1, commit=True
    )
    build, check = issue(conn, public), issue(conn, public, assignee=3)
    first = submit(conn, build)
    review(conn, check, first["candidate_id"], verdict="PASS")
    issues.set_project(conn, build["id"], hidden["id"], commit=True)
    submit(conn, build, first["candidate_id"], "b" * 40)
    private(conn, hidden)
    issues.set_project(conn, build["id"], public["id"], commit=True)
    assert queue(conn, public)["items"] == []
    assert "review_tracking" not in build_work_context(
        conn, str(build["id"]), actor=actor(conn)
    )
    assert queue(conn, public, who=1)["visible_total"] == 1


def test_hidden_latest_review_never_falls_back_to_older_visible_pass(board):
    conn, _, public = board
    hidden = projects.create_project(
        conn, name="Hidden", key="HID", created_by=1, commit=True
    )
    build, check = issue(conn, public), issue(conn, public, assignee=3)
    candidate = submit(conn, build)
    review(conn, check, candidate["candidate_id"], verdict="PASS")
    newer_check = issue(conn, public, assignee=3)
    issues.set_project(conn, build["id"], hidden["id"], commit=True)
    issues.set_project(conn, newer_check["id"], hidden["id"], commit=True)
    review(conn, newer_check, candidate["candidate_id"], verdict="HOLD")
    issues.set_project(conn, build["id"], public["id"], commit=True)
    issues.set_project(conn, newer_check["id"], public["id"], commit=True)
    private(conn, hidden)
    row = queue(conn, public)["items"][0]
    assert (row["review_state"], row["latest_review"]) == ("awaiting_review", None)
    assert queue(conn, public, who=1)["items"][0]["review_state"] == "reported_hold"


def test_candidate_original_scope_and_missing_candidate_have_same_refusal(board):
    conn, _, original = board
    public = projects.create_project(
        conn, name="Public", key="PUB", created_by=1, commit=True
    )
    build = issue(conn, original)
    candidate = submit(conn, build)
    issues.set_project(conn, build["id"], public["id"], commit=True)
    private(conn, original)
    check = issue(conn, public, assignee=3)
    held = claim(conn, check, 3)
    details = [
        refuse(
            conn,
            "not_found",
            lambda cid=cid: complete(conn, check, held, 3, review_candidate_id=cid),
        )
        for cid in (candidate["candidate_id"], 9999)
    ]
    assert details[0] == details[1]
    private(conn, original, 3)
    assert complete(
        conn, check, held, 3, review_candidate_id=candidate["candidate_id"]
    )["candidate_review"]


def test_completion_insertion_failure_rolls_back_release_audit_and_rows(
    board, monkeypatch
):
    from athena.aegis import review_commands

    conn, _, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    before = state(conn)
    real = review_commands.insert_submission

    def fail_after_insert(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("controlled response construction failure")

    monkeypatch.setattr(review_commands, "insert_submission", fail_after_insert)
    with pytest.raises(RuntimeError, match="controlled"):
        complete(conn, build, held, candidate_submission=submission())
    assert state(conn) == before


def test_open_handoff_requires_explicit_resume_before_submission(board):
    conn, _, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    handoff = lease_commands.yield_claim(
        conn,
        actor=actor(conn),
        issue_id=build["id"],
        generation=held["generation"],
        reason="needs_input",
        attempted_work="read",
        evidence=[],
        blocking_question="Ready?",
        resume_instructions="Resume",
    )
    held = claim(conn, build)
    refuse(
        conn,
        "conflict",
        lambda: complete(conn, build, held, candidate_submission=submission()),
    )
    lease_commands.resume_claim_handoff(
        conn,
        actor=actor(conn),
        issue_id=build["id"],
        generation=held["generation"],
        handoff_token=handoff["handoff_token"],
    )
    assert complete(conn, build, held, candidate_submission=submission())[
        "candidate_submission"
    ]


def test_history_clipping_does_not_change_brief_warnings(board, monkeypatch):
    from athena.aegis import work_context, review_queue

    conn, _, project = board
    build = issue(conn, project)
    previous = None
    for _ in range(7):
        previous = submit(conn, build, previous)["candidate_id"]
    brief = work_context.build_work_context(
        conn, str(build["id"]), actor=actor(conn), brief=True
    )
    full = work_context.build_work_context(conn, str(build["id"]), actor=actor(conn))
    assert full["review_tracking"]["recent_candidates"]["returned"] == 5
    assert full["review_tracking"]["recent_candidates"]["visible_total"] == 7
    assert full["review_tracking"]["recent_candidates"]["clipped"]
    assert (
        "context_clipped" in full["warnings"]
        and "context_clipped" not in brief["warnings"]
    )
    monkeypatch.setattr(review_queue, "issue_review_tracking", lambda *a, **k: None)
    assert (
        work_context.build_work_context(
            conn, str(build["id"]), actor=actor(conn), brief=True
        )
        == brief
    )


def test_done_source_remains_in_queue_but_archived_source_does_not(board):
    conn, _, project = board
    build = issue(conn, project, status="done")
    candidate = submit(conn, build)
    row = queue(conn, project)["items"][0]
    assert row["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert row["issue"]["status_category"] == "done"
    issues.set_archived(conn, build["id"], True, commit=True)
    assert queue(conn, project)["visible_total"] == 0
