"""Orientation is actor-visible, bounded after filtering, and one WAL snapshot."""

from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from support import lapse_lease

from athena.aegis import desk, issue_etags, issues, lease_commands, leases, office
from athena.aegis import projects, ready_queue
from athena.core import access, db, packet_time
from athena.main import create_app


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "coordination.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) "
        "VALUES ('admin@example.invalid', 'Admin', 'admin')"
    )
    conn.execute(
        "INSERT INTO users (email, name, role) "
        "VALUES ('member@example.invalid', 'Member', 'member')"
    )
    conn.commit()
    admin = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
    member = dict(conn.execute("SELECT * FROM users WHERE id = 2").fetchone())
    yield path, conn, admin, member
    conn.close()


def _issue(conn, *, project_id=None, title="Synthetic work"):
    return issues.create_issue(
        conn,
        title=title,
        body="Synthetic brief",
        created_by=1,
        project_id=project_id,
        commit=True,
    )


def _held(
    conn,
    actor,
    *,
    project_id=None,
    handoff=False,
    title="Synthetic work",
    paths=None,
    lease_seconds=leases.DEFAULT_LEASE_SECONDS,
):
    issue = _issue(conn, project_id=project_id, title=title)
    issue = issues.set_assignee(conn, issue["id"], actor["id"], commit=True)
    tag = issue_etags.current_etag(conn, issue)
    held = lease_commands.claim_issue(
        conn,
        actor=actor,
        issue_id=issue["id"],
        if_match=[tag],
        paths=paths,
        lease_seconds=lease_seconds,
    )
    if handoff:
        lease_commands.yield_claim(
            conn,
            actor=actor,
            issue_id=issue["id"],
            generation=held["generation"],
            reason="needs_input",
            attempted_work="Synthetic attempted work",
            evidence=[],
            blocking_question="Synthetic private question?",
            resume_instructions="Read the answer before continuing.",
        )
        held = lease_commands.claim_issue(
            conn, actor=actor, issue_id=issue["id"], if_match=[tag], paths=paths
        )
    return issue, held


def _write(path, statement, params=()):
    writer = db.connect(path)
    try:
        writer.execute(statement, params)
        writer.commit()
    finally:
        writer.close()


@pytest.mark.parametrize("change", ["revoke", "private"])
def test_private_content_disappears_but_own_possession_survives(board, change):
    path, conn, _, member = board
    project = projects.create_project(
        conn, name="Restricted", key="SEC", created_by=1, commit=True
    )
    if change == "revoke":
        projects.set_visibility(conn, project["id"], "private", commit=True)
        access.add_project_member(conn, project["id"], member["id"], 1, commit=True)
    issue, held = _held(
        conn, member, project_id=project["id"], handoff=True, paths=["src/private.py"]
    )
    before = desk.build_desk(conn, actor=member)
    assert before["office"]["chair"]["issue_title"] == issue["title"]
    assert len(before["asks"]["claim_handoffs"]["items"]) == 1

    if change == "revoke":
        access.remove_project_member(conn, project["id"], member["id"], commit=True)
    else:
        projects.set_visibility(conn, project["id"], "private", commit=True)
    issues.update_issue(conn, issue["id"], title="PRIVATE NEW TITLE", commit=True)
    with TestClient(create_app(path)) as client:
        headers = {"X-Athena-Actor": str(member["id"])}
        assert client.get(f"/issues/{issue['id']}", headers=headers).status_code == 404
        assert (
            client.get(
                f"/issues/{issue['id']}/work-context", headers=headers
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/issues/{issue['id']}", headers={"X-Athena-Actor": "1"}
            ).status_code
            == 200
        )
        response = client.get("/desk", headers=headers)
        assert response.status_code == 200
        packet = response.json()
        response = client.get("/office", headers=headers)
        assert response.status_code == 200
        for cubicle in (packet["office"], response.json()):
            assert cubicle["active_lease_count"] == 1
            assert cubicle["seated"] is True
            chair = cubicle["chair"]
            assert chair["issue_id"] == issue["id"]
            assert chair["generation"] == held["generation"]
            # The command's stored stamp, in the packet's shape (A13).
            assert chair["expires_at"] == packet_time.rfc3339(held["expires_at"])
            assert chair["declared_paths"] == held["declared_paths"]
            assert chair["issue_title"] is None
            assert chair["issue_key"] is None
            assert chair["project_id"] is None
            assert chair["blocked_by"] == []
            assert chair["checkout_hint"] == f"athena/issue-{issue['id']}"
        assert packet["asks"]["claim_handoffs"]["items"] == []
        assert packet["asks"]["claim_handoffs"]["clipped"] is False
    assert leases.get_lease(conn, issue["id"])["generation"] == held["generation"]


def test_backlog_keeps_issue_content_and_handoff_visible(board):
    _, conn, _, member = board
    issue, _ = _held(conn, member, handoff=True)
    packet = desk.build_desk(conn, actor=member)
    assert packet["office"]["chair"]["issue_title"] == issue["title"]
    assert [x["issue_id"] for x in packet["asks"]["claim_handoffs"]["items"]] == [
        issue["id"]
    ]
    assert packet["asks"]["claim_handoffs"]["clipped"] is False


def test_ready_count_and_items_share_a_snapshot_with_a_concurrent_archive(board):
    path, conn, admin, _ = board
    issue = _issue(conn)
    committed = []
    evaluated = []

    def trace(statement):
        # The rule is one statement (H3): the count and the items cannot
        # disagree with each other. The lane still reads leases and handoffs
        # for its items afterwards, so the archive commits as the first of
        # those reads begins, and the packet must still be one snapshot.
        if committed:
            return
        if statement.startswith("WITH candidate(id) AS"):
            evaluated.append(True)
            return
        if evaluated:
            _write(
                path,
                "UPDATE issues SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
                (issue["id"],),
            )
            committed.append(True)

    conn.set_trace_callback(trace)
    try:
        packet = ready_queue.build_ready(conn, actor=admin)
    finally:
        conn.set_trace_callback(None)
    assert committed == [True]
    assert packet["total"] == 1
    assert [x["issue"]["id"] for x in packet["items"]] == [issue["id"]]
    assert packet["clipped"] is False
    assert ready_queue.build_ready(conn, actor=admin)["total"] == 0


@pytest.mark.parametrize("reader", [desk.build_desk, office.build_office])
def test_release_mid_read_does_not_split_possession_from_its_count(
    board, monkeypatch, reader
):
    path, conn, admin, _ = board
    issue, _ = _held(conn, admin)
    committed = []

    def interleave(original):
        def release_after_read(conn, **kwargs):
            held = original(conn, **kwargs)
            if not committed:
                writer = db.connect(path)
                try:
                    writer.execute(
                        "DELETE FROM issue_leases WHERE issue_id = ?", (issue["id"],)
                    )
                    writer.execute(
                        "UPDATE issues SET title = 'Changed after release' WHERE id = ?",
                        (issue["id"],),
                    )
                    writer.commit()
                finally:
                    writer.close()
                committed.append(True)
            return held

        return release_after_read

    monkeypatch.setattr(
        leases, "active_leases_held_by", interleave(leases.active_leases_held_by)
    )
    monkeypatch.setattr(leases, "leases_held_by", interleave(leases.leases_held_by))
    packet = reader(conn, actor=admin)
    if reader is desk.build_desk:
        assert [x["issue_id"] for x in packet["work"]["leases"]["items"]] == [
            issue["id"]
        ]
        assert packet["work"]["leases"]["total"] == 1
        assert packet["work"]["ready"]["items"] == []
        cubicle = packet["office"]
    else:
        cubicle = packet
    assert committed == [True]
    assert cubicle["active_lease_count"] == 1
    assert cubicle["chair"]["issue_id"] == issue["id"]
    assert cubicle["chair"]["issue_title"] == issue["title"]
    assert office.build_office(conn, actor=admin)["chair"] is None


@pytest.mark.parametrize(
    "reader", [desk.build_desk, ready_queue.build_ready, office.build_office]
)
def test_nested_reader_does_not_commit_or_end_callers_transaction(board, reader):
    path, conn, admin, _ = board
    issue = _issue(conn)
    conn.execute("BEGIN")
    conn.execute(
        "UPDATE issues SET title = 'Uncommitted caller edit' WHERE id = ?",
        (issue["id"],),
    )
    observer = db.connect(path)
    try:
        reader(conn, actor=admin)
        assert conn.in_transaction
        assert issues.get_issue(observer, issue["id"])["title"] == issue["title"]
        conn.rollback()
        assert issues.get_issue(conn, issue["id"])["title"] == issue["title"]
    finally:
        observer.close()


@pytest.mark.parametrize("count", [0, 1, 2, 20, 21])
def test_handoff_bounds_and_office_count_do_not_reuse_held_display_limit(board, count):
    _, conn, admin, _ = board
    expected = [_held(conn, admin, handoff=True)[0]["id"] for _ in range(count)]
    packet = desk.build_desk(conn, actor=admin)
    handoffs = packet["asks"]["claim_handoffs"]
    assert [x["issue_id"] for x in handoffs["items"]] == expected[:20]
    assert handoffs["clipped"] is (count > 20)
    assert handoffs["limit"] == 20
    assert packet["office"]["active_lease_count"] == count
    assert packet["office"]["seated"] is (count == 1)
    if count > 1:
        assert "more than one active lease" in " ".join(packet["office"]["warnings"])


def test_hidden_handoffs_are_filtered_before_the_bound_and_clipping(board):
    _, conn, _, member = board
    project = projects.create_project(
        conn, name="Hidden later", key="HID", created_by=1, commit=True
    )
    for _ in range(21):
        _held(conn, member, project_id=project["id"], handoff=True)
    visible, _ = _held(conn, member, handoff=True)
    projects.set_visibility(conn, project["id"], "private", commit=True)
    packet = desk.build_desk(conn, actor=member)
    handoffs = packet["asks"]["claim_handoffs"]
    assert [x["issue_id"] for x in handoffs["items"]] == [visible["id"]]
    assert handoffs["clipped"] is False
    assert packet["office"]["active_lease_count"] == 22


def test_newer_lapsed_leases_do_not_hide_the_older_active_chair(clock, board):
    path, conn, admin, _ = board
    active, _ = _held(conn, admin)
    # Twenty newer claims, each the shortest lease there is: the app's clock
    # passes them and leaves the older chair live.
    clock.advance(seconds=1)
    lapsed = [
        _held(conn, admin, lease_seconds=leases.MIN_LEASE_SECONDS)[0] for _ in range(20)
    ]
    lapse_lease(clock, path, lapsed[-1]["id"])
    packet = desk.build_desk(conn, actor=admin)
    assert packet["work"]["leases"]["lapsed_total"] == 20
    assert packet["office"]["active_lease_count"] == 1
    assert packet["office"]["seated"] is True
    assert packet["office"]["chair"]["issue_id"] == active["id"]


def test_standalone_office_observes_expiry_at_one_clock_instant(board, monkeypatch):
    _, conn, admin, _ = board
    issue, held = _held(conn, admin)

    class CrossingClock:
        """The app's one clock as the office and the lease reads see it, a
        second later at every read, starting a second before the lease's
        expiry: a second read lands on the expiry, where it has lapsed."""

        observed = packet_time.parse(held["expires_at"]) - timedelta(seconds=1)

        @classmethod
        def now(cls):
            value = cls.observed
            cls.observed += timedelta(seconds=1)
            return value

    monkeypatch.setattr(office, "clock", CrossingClock)
    monkeypatch.setattr(leases, "clock", CrossingClock)
    packet = office.build_office(conn, actor=admin)
    assert packet["active_lease_count"] == 1
    assert packet["chair"]["issue_id"] == issue["id"]
    assert packet["seated"] is True
