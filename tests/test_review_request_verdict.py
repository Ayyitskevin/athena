"""A review request carries what its requested reviewer reported, and nothing else (C5).

WHY: a conductor asks one named reviewer to review one exact candidate on one
review issue, then decides on the answer. The queue's `review_state` follows
the candidate's newest linked review, whoever wrote it: an account nobody asked
can complete an unrelated issue naming the candidate with PASS and turn the
requested reviewer's HOLD into `reported_pass`. Decision D4 adds a field to the
request row that joins it to the completion that fulfilled it, so what the
conductor gates on is what the reviewer it asked reported, on that candidate,
on that review issue. `review_state` and unrequested reviews are unchanged.

The field is a fact about who reported what. It grants and enforces nothing;
hidden reports read as no report, and imported history carries none.
"""

from contextlib import closing

from athena.aegis import issues, projects
from athena.aegis.review_queue import build_project_review_queue
from athena.core import db, portability
from test_review_queue import (
    board as _board,
    claim,
    complete,
    issue,
    private,
    queue,
    submit,
)
from test_review_request_projection import context, desk_group, queue_group, request

board = _board
REX, UMA = 3, 4  # the reviewer the conductor asks; an account nobody asked


def report(conn, item, candidate, who=REX, **fields):
    """Claim `item` as `who` and complete it reviewing `candidate`."""
    return complete(
        conn,
        item,
        claim(conn, item, who),
        who,
        review_candidate_id=candidate["candidate_id"],
        **fields,
    )["candidate_review"]


def asked(conn, project, candidate, who=REX):
    """The request rows on the candidate's queue row, as `who` reads them."""
    rows = [
        row
        for row in queue(conn, project, who=who)["items"]
        if row["candidate"]["candidate_id"] == candidate["candidate_id"]
    ]
    return (rows[0].get("review_requests") or {}).get("items", []) if rows else []


def fulfillment(item):
    return item["status"], item["fulfillment_event_id"], item["reported_verdict"]


def test_an_unrequested_pass_does_not_move_the_requested_reviewers_hold(board):
    conn, _, project = board
    build = issue(conn, project)
    check = issue(conn, project, assignee=REX)
    chore = issue(conn, project, assignee=UMA)
    candidate = submit(conn, build)
    request(conn, check, candidate)
    (item,) = asked(conn, project, candidate)
    assert fulfillment(item) == ("requested", None, None)

    held = report(conn, check, candidate, verdict="HOLD", findings={"P1": 1})
    (item,) = asked(conn, project, candidate)
    assert fulfillment(item) == ("fulfilled", held["completion_event_id"], "HOLD")

    unasked = report(conn, chore, candidate, UMA, verdict="PASS")
    (row,) = queue(conn, project, who=REX)["items"]
    # The queue's own state still follows the newest review, as before.
    assert row["review_state"] == "reported_pass"
    assert row["latest_review"]["reported_by"]["id"] == UMA
    assert row["latest_review"]["completion_event_id"] == unasked["completion_event_id"]
    # What the conductor gates on does not move.
    (item,) = row["review_requests"]["items"]
    assert fulfillment(item) == ("fulfilled", held["completion_event_id"], "HOLD")
    assert context(conn, check)["review_request"] == item
    assert queue_group(conn, project)["items"] == [item]


def test_another_accounts_link_on_the_requested_issue_does_not_move_it(board):
    """The completion command refuses another account's review on a requested
    review issue; the read does not lean on that. This link is written directly,
    as a defect or an older build could have, and the request ignores it."""
    conn, _, project = board
    build = issue(conn, project)
    check = issue(conn, project, assignee=REX)
    candidate = submit(conn, build)
    request(conn, check, candidate)
    complete(conn, check, claim(conn, check, 1), 1, verdict="PASS")
    event = conn.execute(
        "SELECT MAX(id) FROM activity WHERE verb = 'claim_completed' AND actor_id = 1 "
        "AND target_kind = 'issue' AND target_id = ?",
        (check["id"],),
    ).fetchone()[0]
    assert event is not None
    conn.execute(
        "INSERT INTO issue_candidate_reviews(candidate_id,review_issue_id,completion_event_id) "
        "VALUES (?,?,?)",
        (candidate["candidate_id"], check["id"], event),
    )
    conn.commit()
    (row,) = queue(conn, project, who=REX)["items"]
    assert row["review_state"] == "reported_pass"  # the queue's own rule, unchanged
    (item,) = row["review_requests"]["items"]
    assert fulfillment(item) == ("requested", None, None)


def test_the_requested_reviewer_elsewhere_does_not_move_the_request(board):
    """Another candidate, or the same candidate on an issue it was not asked on."""
    conn, _, project = board
    build, other_build = issue(conn, project), issue(conn, project)
    check = issue(conn, project, assignee=REX)
    other_check = issue(conn, project, assignee=REX)
    candidate, other = submit(conn, build), submit(conn, other_build)
    request(conn, check, candidate)

    report(conn, other_check, other, verdict="PASS")
    (item,) = asked(conn, project, candidate)
    assert fulfillment(item) == ("requested", None, None)

    # The request names its review issue: a review of the right candidate by
    # the right account on a different issue is an unrequested review, as the
    # request's status already says.
    side = report(conn, other_check, candidate, verdict="PASS")
    (row,) = [
        row
        for row in queue(conn, project, who=REX)["items"]
        if row["candidate"]["candidate_id"] == candidate["candidate_id"]
    ]
    assert row["latest_review"]["completion_event_id"] == side["completion_event_id"]
    (item,) = row["review_requests"]["items"]
    assert fulfillment(item) == ("requested", None, None)


def test_a_second_report_by_the_requested_reviewer_replaces_the_first(board):
    """The newest fulfilling report wins, as the queue's newest review does; a
    newer report with no verdict never falls back to an older one."""
    conn, _, project = board
    build = issue(conn, project)
    check = issue(conn, project, assignee=REX)
    candidate = submit(conn, build)
    request(conn, check, candidate)
    report(conn, check, candidate, verdict="HOLD")
    second = report(conn, check, candidate, verdict="PASS")
    (row,) = queue(conn, project, who=REX)["items"]
    (item,) = row["review_requests"]["items"]
    assert fulfillment(item) == ("fulfilled", second["completion_event_id"], "PASS")
    assert row["latest_review"]["completion_event_id"] == second["completion_event_id"]

    third = report(conn, check, candidate, verdict="HOLD")
    (item,) = asked(conn, project, candidate)
    assert fulfillment(item) == ("fulfilled", third["completion_event_id"], "HOLD")

    silent = report(conn, check, candidate)  # a report without a verdict
    (item,) = asked(conn, project, candidate)
    assert fulfillment(item) == ("fulfilled", silent["completion_event_id"], None)


def test_a_replacement_and_its_request_carry_their_own_report(board):
    conn, _, project = board
    build = issue(conn, project)
    check = issue(conn, project, assignee=REX)
    second_check = issue(conn, project, assignee=REX)
    first = submit(conn, build)
    request(conn, check, first)
    held = report(conn, check, first, verdict="HOLD")
    replacement = submit(conn, build, first["candidate_id"], "b" * 40)

    # The fulfilled request keeps its own report after its candidate is replaced.
    assert fulfillment(context(conn, check)["review_request"]) == (
        "fulfilled",
        held["completion_event_id"],
        "HOLD",
    )
    assert asked(conn, project, replacement) == []

    # A new request on a second review issue starts with nothing reported.
    request(conn, second_check, replacement)
    (item,) = asked(conn, project, replacement)
    assert fulfillment(item) == ("requested", None, None)
    passed = report(conn, second_check, replacement, verdict="PASS")
    (item,) = asked(conn, project, replacement)
    assert fulfillment(item) == ("fulfilled", passed["completion_event_id"], "PASS")
    assert fulfillment(context(conn, check)["review_request"])[2] == "HOLD"

    # Reusing the first review issue, as the recipe does: the earlier HOLD on
    # that issue answered the earlier request, not this one.
    third = submit(conn, build, replacement["candidate_id"], "c" * 40)
    request(conn, check, third)
    (item,) = asked(conn, project, third)
    assert item["review_issue_id"] == check["id"]
    assert fulfillment(item) == ("requested", None, None)
    assert context(conn, check)["review_request"] == item


def test_a_superseded_unanswered_request_carries_no_report(board):
    """A request ends unanswered only by supersession; there is no cancel."""
    conn, _, project = board
    build = issue(conn, project)
    check = issue(conn, project, assignee=REX)
    first = submit(conn, build)
    request(conn, check, first)
    held = claim(conn, check, REX)
    submit(conn, build, first["candidate_id"], "b" * 40)
    item = context(conn, check)["review_request"]
    assert fulfillment(item) == ("superseded", None, None)
    assert desk_group(conn)["items"] == [item]
    complete(conn, check, held, REX)  # ordinary release: no review association
    assert fulfillment(context(conn, check)["review_request"]) == (
        "superseded",
        None,
        None,
    )


def test_a_report_the_reader_cannot_see_reads_as_no_report(board):
    """Hidden reads as missing: the field leaks no verdict the reader could not
    otherwise read, and never falls back to an earlier visible one."""
    conn, _, public = board
    hidden = projects.create_project(
        conn, name="Hidden", key="HID", created_by=1, commit=True
    )
    build = issue(conn, public)
    check = issue(conn, public, assignee=REX)
    candidate = submit(conn, build)
    request(conn, check, candidate)

    def reads():
        return queue_group(conn, public), context(conn, check).get("review_request")

    unanswered = reads()
    report(conn, check, candidate, verdict="PASS")
    for item in (build, check):
        issues.set_project(conn, item["id"], hidden["id"], commit=True)
    newest = report(conn, check, candidate, verdict="HOLD")  # recorded in HID
    for item in (build, check):
        issues.set_project(conn, item["id"], public["id"], commit=True)
    private(conn, hidden)
    assert reads() == unanswered
    assert fulfillment(unanswered[1]) == ("requested", None, None)
    admin = context(conn, check, who=1)["review_request"]
    assert fulfillment(admin) == ("fulfilled", newest["completion_event_id"], "HOLD")

    # A review issue the reader cannot see: the request row is not served.
    issues.set_project(conn, check["id"], hidden["id"], commit=True)
    packet = queue(conn, public, who=REX)
    assert all("review_requests" not in row for row in packet["items"])
    assert context(conn, check) is None
    assert fulfillment(context(conn, check, who=1)["review_request"])[2] == "HOLD"


def test_imported_history_carries_no_request_and_no_report(board, tmp_path):
    conn, _, project = board
    build = issue(conn, project)
    check = issue(conn, project, assignee=REX)
    candidate = submit(conn, build)
    request(conn, check, candidate)
    report(conn, check, candidate, verdict="HOLD")
    bundle = portability.export_bundle(conn, "project", project["id"])
    with closing(db.connect(tmp_path / "imported.db")) as copied:
        db.migrate(copied)
        for user in bundle["users"]:
            copied.execute(
                "INSERT INTO users(email,name,role) VALUES (?,?,'member')",
                (user["email"], user["name"]),
            )
        copied.commit()
        manifest = portability.build_import_manifest(copied, bundle)
        assert portability.replay_import_manifest(copied, bundle, manifest)["ok"]
        imported = copied.execute("SELECT id FROM projects WHERE key='RVW'").fetchone()
        packet = build_project_review_queue(
            copied, project_id=imported["id"], actor=None
        )
        assert packet["items"] == []
        assert (
            copied.execute("SELECT COUNT(*) FROM issue_review_requests").fetchone()[0]
            == 0
        )
