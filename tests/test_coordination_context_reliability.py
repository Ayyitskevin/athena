"""Orientation is actor-visible, bounded after filtering, and one WAL snapshot."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from athena.aegis import desk, issue_etags, issues, lease_commands, leases, office
from athena.aegis import projects, ready_queue
from athena.core import access, db
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
        conn, title=title, body="Synthetic brief", created_by=1, project_id=project_id
    )


def _held(
    conn, actor, *, project_id=None, handoff=False, title="Synthetic work", paths=None
):
    issue = _issue(conn, project_id=project_id, title=title)
    issue = issues.set_assignee(conn, issue["id"], actor["id"])
    tag = issue_etags.current_etag(conn, issue)
    held = lease_commands.claim_issue(
        conn, actor=actor, issue_id=issue["id"], if_match=[tag], paths=paths
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
    project = projects.create_project(conn, name="Restricted", key="SEC", created_by=1)
    if change == "revoke":
        projects.set_visibility(conn, project["id"], "private")
        access.add_project_member(conn, project["id"], member["id"], 1)
    issue, held = _held(
        conn, member, project_id=project["id"], handoff=True, paths=["src/private.py"]
    )
    before = desk.build_desk(conn, actor=member)
    assert before["office"]["chair"]["issue_title"] == issue["title"]
    assert len(before["asks"]["claim_handoffs"]["items"]) == 1

    if change == "revoke":
        access.remove_project_member(conn, project["id"], member["id"])
    else:
        projects.set_visibility(conn, project["id"], "private")
    issues.update_issue(conn, issue["id"], title="PRIVATE NEW TITLE")
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
            assert chair["expires_at"] == held["expires_at"]
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

    def trace(statement):
        if not committed and statement.startswith(
            "SELECT i.id, i.title, i.status, i.priority, i.created_at"
        ):
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
        conn, name="Hidden later", key="HID", created_by=1
    )
    for _ in range(21):
        _held(conn, member, project_id=project["id"], handoff=True)
    visible, _ = _held(conn, member, handoff=True)
    projects.set_visibility(conn, project["id"], "private")
    packet = desk.build_desk(conn, actor=member)
    handoffs = packet["asks"]["claim_handoffs"]
    assert [x["issue_id"] for x in handoffs["items"]] == [visible["id"]]
    assert handoffs["clipped"] is False
    assert packet["office"]["active_lease_count"] == 22


def test_newer_lapsed_leases_do_not_hide_the_older_active_chair(board):
    _, conn, admin, _ = board
    active, _ = _held(conn, admin)
    for _ in range(20):
        issue, _ = _held(conn, admin)
        conn.execute(
            "UPDATE issue_leases SET claimed_at = '2020-01-02 00:00:00', expires_at = '2020-01-03 00:00:00' WHERE issue_id = ?",
            (issue["id"],),
        )
        conn.commit()
    conn.execute(
        "UPDATE issue_leases SET claimed_at = '2020-01-01 00:00:00' WHERE issue_id = ?",
        (active["id"],),
    )
    conn.commit()
    packet = desk.build_desk(conn, actor=admin)
    assert packet["work"]["leases"]["lapsed_total"] == 20
    assert packet["office"]["active_lease_count"] == 1
    assert packet["office"]["seated"] is True
    assert packet["office"]["chair"]["issue_id"] == active["id"]


def test_standalone_office_observes_expiry_at_one_clock_instant(board, monkeypatch):
    _, conn, admin, _ = board
    issue, _ = _held(conn, admin)
    conn.execute(
        "UPDATE issue_leases SET expires_at = '2030-01-01 00:00:01' WHERE issue_id = ?",
        (issue["id"],),
    )
    conn.commit()

    class CrossingClock(datetime):
        observed = datetime(2030, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            value = cls.observed
            cls.observed += timedelta(seconds=1)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(office, "datetime", CrossingClock)
    monkeypatch.setattr(leases, "datetime", CrossingClock)
    packet = office.build_office(conn, actor=admin)
    assert packet["active_lease_count"] == 1
    assert packet["chair"]["issue_id"] == issue["id"]
    assert packet["seated"] is True
