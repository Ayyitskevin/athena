"""A project brief is bounded visible evidence, never authority or acceptance."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
import pytest

from athena.aegis import issues, projects
from athena.core import db
from athena.main import create_app


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "brief.db"
    conn = db.connect(path)
    db.migrate(conn)
    for name, role in [("Owner", "admin"), ("Member", "member"), ("Worker", "member")]:
        conn.execute(
            "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
            (f"{name.lower()}@example.invalid", name, role),
        )
    conn.commit()
    project = projects.create_project(conn, name="Brief", key="BRF", created_by=1)
    member = dict(conn.execute("SELECT * FROM users WHERE id=2").fetchone())
    yield path, conn, project, member
    conn.close()


def _issue(conn, project_id, title="Work", **kwargs):
    return issues.create_issue(
        conn,
        title=title,
        body="BODY MUST NOT LEAK",
        created_by=1,
        project_id=project_id,
        **kwargs,
    )


def test_empty_project_is_a_complete_bounded_packet(board):
    from athena.aegis.project_brief import build_project_brief

    _, conn, project, member = board
    packet = build_project_brief(
        conn,
        project_id=project["id"],
        actor=member,
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert packet["schema"] == "athena.project_brief.v1"
    assert packet["summary"] == {
        "open_issue_count": 0,
        "unassigned_open_issue_count": 0,
    }
    for name in ("active_claims", "blockers", "open_handoffs", "latest_completions"):
        assert packet[name] == {
            "items": [],
            "visible_total": 0,
            "returned": 0,
            "limit": 5,
            "clipped": False,
        }


def test_http_empty_project_has_private_cache_and_no_etag(board):
    path, _, project, _ = board
    with TestClient(create_app(path)) as client:
        response = client.get(f"/projects/{project['id']}/brief")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert {x.strip().lower() for x in response.headers["vary"].split(",")} >= {
        "authorization",
        "x-athena-actor",
    }
    assert "etag" not in response.headers


# Fixed clocks make the expiry boundary independent of the machine running tests.
NOW = datetime(2035, 1, 2, 12, tzinfo=UTC)


def _actor(conn, actor_id=1):
    return dict(conn.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone())


def _hold(conn, issue, *, holder=1, expiry="2035-01-02 12:00:01"):
    from athena.aegis import leases

    held = leases.upsert_lease(conn, issue["id"], holder, 3600)
    conn.execute(
        "UPDATE issue_leases SET expires_at=? WHERE issue_id=?", (expiry, issue["id"])
    )
    conn.commit()
    return held


def _handoff(conn, issue, question="What should happen next?"):
    from athena.aegis import issue_etags, lease_commands

    actor = _actor(conn)
    lease = lease_commands.claim_issue(
        conn,
        actor=actor,
        issue_id=issue["id"],
        if_match=[issue_etags.current_etag(conn, issue)],
    )
    return lease_commands.yield_claim(
        conn,
        actor=actor,
        issue_id=issue["id"],
        generation=lease["generation"],
        reason="needs_input",
        attempted_work="PRIVATE ATTEMPT",
        evidence=[],
        blocking_question=question,
        resume_instructions="PRIVATE INSTRUCTIONS",
    )


def _report(conn, issue, report=None, *, malformed=False, imported=False):
    import json
    from athena.aegis.issue_activity import COMPLETION_REPORT_MARKER
    from athena.core import activity

    detail = "generation " + "a" * 32
    if report is not None or malformed:
        detail += COMPLETION_REPORT_MARKER + ("{" if malformed else json.dumps(report))
    return activity.record(
        conn,
        actor_id=3,
        verb="claim_completed",
        target_kind="issue",
        target_id=issue["id"],
        detail=detail,
        imported_at="2030-01-01 00:00:00" if imported else None,
    )


def _brief(conn, project, actor, **kwargs):
    from athena.aegis.project_brief import build_project_brief

    return build_project_brief(
        conn, project_id=project["id"], actor=actor, now=NOW, **kwargs
    )


def _block(conn, work, blocker):
    from athena.aegis import dependencies

    reason, inserted = dependencies.add_link(
        conn, from_id=blocker["id"], to_id=work["id"], relation="blocks", created_by=1
    )
    assert reason is None and inserted


def test_populations_keep_assignment_possession_and_reports_distinct(board):
    import json
    from athena.aegis import dependencies
    from athena.aegis.project_brief_models import ProjectBrief

    _, conn, project, member = board
    work = _issue(conn, project["id"], "Assigned work")
    issues.set_assignee(conn, work["id"], 2)
    _hold(conn, work, holder=3)
    done = _issue(conn, project["id"], "Done but held", status="done")
    _handoff(conn, done)
    _hold(conn, done, holder=1, expiry="2035-01-02 12:00:02")
    _report(conn, done, {"verdict": "PASS", "findings": {"P2": 0}})
    _issue(conn, project["id"], "Unclaimed")
    archived = _issue(conn, project["id"], "Archived work")
    _hold(conn, archived)
    _handoff_issue = _issue(conn, project["id"], "Archived handoff")
    _handoff(conn, _handoff_issue)
    for issue in (archived, _handoff_issue):
        conn.execute(
            "UPDATE issues SET archived_at=CURRENT_TIMESTAMP WHERE id=?", (issue["id"],)
        )
    conn.commit()
    foreign = projects.create_project(conn, name="Other", key="OTH", created_by=1)
    blocker = _issue(conn, foreign["id"], "Archived blocker")
    conn.execute(
        "UPDATE issues SET archived_at=CURRENT_TIMESTAMP WHERE id=?", (blocker["id"],)
    )
    conn.commit()
    _block(conn, work, blocker)
    _block(conn, done, blocker)  # A done root does not enter the blocker section.
    closed_blocker = _issue(conn, foreign["id"], "Resolved", status="done")
    _block(conn, work, closed_blocker)
    packet = _brief(conn, project, member)
    assert packet["summary"] == {
        "open_issue_count": 2,
        "unassigned_open_issue_count": 1,
    }
    assert packet["active_claims"]["visible_total"] == 2
    assert [x["issue"]["id"] for x in packet["active_claims"]["items"]] == [
        work["id"],
        done["id"],
    ]
    assert packet["active_claims"]["items"][0]["holder"]["id"] == 3
    assert packet["active_claims"]["items"][0]["issue"]["assignee"]["id"] == 2
    assert packet["blockers"]["visible_total"] == 1
    edge = packet["blockers"]["items"][0]
    assert edge["blocked_by"]["archived_at"] is not None
    assert edge["blocked_by"]["project_id"] == foreign["id"]
    assert (
        edge["blocked_by"]["id"]
        == dependencies.open_blockers(conn, work["id"], actor=member)[0]["id"]
    )
    assert packet["open_handoffs"]["visible_total"] == 1
    assert packet["open_handoffs"]["items"][0]["issue"]["status_category"] == "done"
    assert packet["latest_completions"]["items"][0]["reported_verdict"] == "PASS"
    ProjectBrief.model_validate(packet)
    serialized = json.dumps(packet)
    for private in (
        "BODY MUST NOT LEAK",
        "PRIVATE ATTEMPT",
        "PRIVATE INSTRUCTIONS",
        "handoff_token",
        "generation",
        "detail",
        "email",
    ):
        assert private not in serialized


def test_hidden_blockers_and_historical_events_do_not_count_or_consume_limit(board):
    from athena.core import access

    _, conn, project, member = board
    secret = projects.create_project(conn, name="Secret", key="SEC", created_by=1)
    projects.set_visibility(conn, secret["id"], "private")
    hidden = _issue(conn, secret["id"], "SECRET BLOCKER")
    work = _issue(conn, project["id"], "Visible work")
    _block(conn, work, hidden)
    visible = _issue(conn, None, "Backlog blocker")
    _block(conn, work, visible)
    moved = _issue(conn, secret["id"], "Moved work")
    old = _report(conn, moved, {"verdict": "HOLD"})
    _handoff(conn, moved, "SECRET QUESTION")
    issues.set_project(conn, moved["id"], project["id"])
    visible_question = _issue(conn, project["id"], "Visible question")
    _handoff(conn, visible_question)
    visible_report = _report(conn, work, {"verdict": "PASS"})
    packet = _brief(conn, project, member, limit=1)
    assert packet["blockers"]["visible_total"] == 1
    assert packet["blockers"]["items"][0]["blocked_by"]["key"] is None
    assert packet["blockers"]["items"][0]["blocked_by"]["project_id"] is None
    assert packet["open_handoffs"]["visible_total"] == 1
    assert packet["open_handoffs"]["items"][0]["issue"]["id"] == visible_question["id"]
    assert packet["latest_completions"]["visible_total"] == 1
    assert packet["latest_completions"]["items"][0]["event_id"] == visible_report["id"]
    assert "SECRET" not in str(packet)
    access.add_project_member(conn, secret["id"], 2, 1)
    granted = _brief(conn, project, member, limit=20)
    assert granted["blockers"]["visible_total"] == 2
    assert granted["open_handoffs"]["visible_total"] == 2
    assert old["id"] in [r["event_id"] for r in granted["latest_completions"]["items"]]


@pytest.mark.parametrize(
    "new_report,malformed,available,verdict,findings",
    [
        (None, False, False, None, None),
        (None, True, False, None, None),
        ({"findings": {"P2": 0}}, False, True, None, {"P2": 0}),
        ({"verdict": "HOLD"}, False, True, "HOLD", None),
    ],
)
def test_latest_native_event_never_revives_older_pass(
    board, new_report, malformed, available, verdict, findings
):
    _, conn, project, member = board
    issue = _issue(conn, project["id"], status="done")
    _report(conn, issue, {"verdict": "PASS", "findings": {"P1": 0}})
    latest = _report(conn, issue, new_report, malformed=malformed)
    imported = _report(conn, issue, {"verdict": "PASS"}, imported=True)
    conn.execute(
        "UPDATE activity SET created_at='2030-01-01 00:00:00' WHERE target_id=?",
        (issue["id"],),
    )
    conn.commit()
    row = _brief(conn, project, member)["latest_completions"]["items"][0]
    assert row["event_id"] == latest["id"] < imported["id"]
    assert row["report_available"] is available
    assert row["reported_verdict"] == verdict
    assert row["reported_findings"] == findings


def test_expiry_is_one_injected_clock_and_multi_claims_remain_visible(board):
    from datetime import timedelta, timezone
    from athena.aegis.project_brief import build_project_brief

    _, conn, project, member = board
    for expiry in (
        "2035-01-02 11:59:59",
        "2035-01-02 12:00:00",
        "2035-01-02 12:00:01",
        "2035-01-02 12:00:02",
    ):
        _hold(conn, _issue(conn, project["id"]), expiry=expiry)
    packet = build_project_brief(
        conn,
        project_id=project["id"],
        actor=member,
        now=NOW.astimezone(timezone(timedelta(hours=4))),
    )
    assert packet["observed_at"] == NOW.isoformat()
    assert packet["active_claims"]["visible_total"] == 2
    assert len({x["holder"]["id"] for x in packet["active_claims"]["items"]}) == 1
    assert conn.execute("SELECT COUNT(*) FROM issue_leases").fetchone()[0] == 4


def test_status_sql_exactly_matches_owner_for_custom_unknown_and_fallback(board):
    from athena.aegis import project_brief, statuses

    _, conn, project, _ = board
    empty = projects.create_project(conn, name="No statuses", key="EMP", created_by=1)
    conn.execute(
        "DELETE FROM project_statuses WHERE project_id IN (?,?)",
        (project["id"], empty["id"]),
    )
    conn.execute(
        "INSERT INTO project_statuses (project_id,name,category,position) VALUES (?,?,?,0)",
        (project["id"], "shipped", "done"),
    )
    conn.commit()
    for project_id in (project["id"], empty["id"], None):
        for name in ("open", "done", "shipped", "unrecognized"):
            issue = _issue(conn, project_id, status=name)
            category = conn.execute(
                f"SELECT {project_brief._category_sql('i')} FROM issues i WHERE i.id=?",
                (issue["id"],),
            ).fetchone()[0]
            assert category == statuses.category_of(conn, project_id, name)
    packet = _brief(conn, project, None)
    assert packet["summary"]["open_issue_count"] == 3  # Literal done is unknown here.


def test_unicode_excerpts_and_independent_section_clipping(board):
    _, conn, project, member = board
    long = "界🙂" * 250
    projects.update_project(conn, project["id"], name=long)
    conn.execute("UPDATE users SET name=? WHERE id=1", (long,))
    conn.execute(
        "INSERT INTO project_statuses (project_id,name,category,position) VALUES (?,?,?,4)",
        (project["id"], long, "doing"),
    )
    conn.commit()
    for _ in range(3):
        issue = _issue(conn, project["id"], title=long, status=long)
        _handoff(conn, issue, long)
        _hold(conn, issue)
        _report(conn, issue, {"verdict": "HOLD"})
        _block(conn, issue, _issue(conn, None))
    packet = _brief(conn, project, member, limit=2)
    for lane in ("active_claims", "blockers", "open_handoffs", "latest_completions"):
        assert packet[lane]["visible_total"] == 3
        assert packet[lane]["returned"] == 2 and packet[lane]["clipped"]
    expected = {"text": long[:160], "total_chars": 500, "clipped": True}
    assert packet["project"]["name"] == expected
    claim = packet["active_claims"]["items"][0]
    assert (
        claim["holder"]["name"]
        == claim["issue"]["title"]
        == claim["issue"]["status"]
        == expected
    )
    assert packet["open_handoffs"]["items"][0]["blocking_question"] == {
        "text": long[:400],
        "total_chars": 500,
        "clipped": True,
    }


def test_count_items_and_enrichment_share_one_wal_snapshot(board):
    path, conn, project, member = board
    issue = _issue(conn, project["id"], "Before")
    _hold(conn, issue)
    changed = []

    def trace(statement):
        if not changed and statement.startswith("SELECT i.id AS id"):
            writer = db.connect(path)
            writer.execute(
                "UPDATE issues SET title='After', archived_at=CURRENT_TIMESTAMP WHERE id=?",
                (issue["id"],),
            )
            writer.execute("DELETE FROM issue_leases WHERE issue_id=?", (issue["id"],))
            writer.commit()
            writer.close()
            changed.append(True)

    conn.set_trace_callback(trace)
    try:
        packet = _brief(conn, project, member)
    finally:
        conn.set_trace_callback(None)
    assert changed == [True]
    assert packet["active_claims"]["visible_total"] == 1
    assert packet["active_claims"]["items"][0]["issue"]["title"]["text"] == "Before"
    assert _brief(conn, project, member)["active_claims"]["visible_total"] == 0


def test_nested_read_preserves_transaction_and_performs_no_dml(board):
    path, conn, project, member = board
    statements = []
    conn.execute("UPDATE projects SET name='Uncommitted' WHERE id=?", (project["id"],))
    conn.set_trace_callback(statements.append)
    try:
        packet = _brief(conn, project, member)
    finally:
        conn.set_trace_callback(None)
    assert packet["project"]["name"]["text"] == "Uncommitted"
    assert conn.in_transaction
    other = db.connect(path)
    assert (
        other.execute(
            "SELECT name FROM projects WHERE id=?", (project["id"],)
        ).fetchone()[0]
        == "Brief"
    )
    other.close()
    assert not any(
        s.lstrip()
        .upper()
        .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "COMMIT", "ROLLBACK"))
        for s in statements
    )
    conn.rollback()
    assert _brief(conn, project, member)["project"]["name"]["text"] == "Brief"


@pytest.mark.parametrize(
    "field,value",
    [
        (field, value)
        for field in ("project_id", "limit")
        for value in (True, False, 0, -1, 1.5, "1", None, 2**63)
    ],
)
def test_reader_refuses_invalid_ids_and_limits_before_sql(board, field, value):
    from athena.aegis.project_brief import build_project_brief

    _, conn, project, member = board
    args = {"project_id": project["id"], "actor": member, field: value}
    with pytest.raises(ValueError, match=field):
        build_project_brief(conn, **args)


def test_http_visibility_revocation_read_token_validation_and_domain_immutability(
    board,
):
    from athena.core import access, tokens

    path, conn, project, member = board
    issue = _issue(conn, project["id"])
    _hold(conn, issue, holder=2)
    projects.set_visibility(conn, project["id"], "private")
    access.add_project_member(conn, project["id"], 2, 1)
    token = tokens.create_token(
        conn, user_id=2, name="synthetic-read", scopes=["read"]
    )["token"]
    before = {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
        for table in (
            "issues",
            "issue_leases",
            "issue_claim_handoffs",
            "activity",
            "agent_cursors",
        )
    }
    with TestClient(create_app(path)) as client:
        url = f"/projects/{project['id']}/brief"
        assert (
            client.get(url, headers={"Authorization": f"Bearer {token}"}).status_code
            == 200
        )
        assert client.get(url).status_code == 404
        assert client.get(url, headers={"X-Athena-Actor": "1"}).status_code == 200
        for invalid in ("0", "21", "-1", "true", "1.5", "abc"):
            assert (
                client.get(
                    url, params={"limit": invalid}, headers={"X-Athena-Actor": "2"}
                ).status_code
                == 422
            )
        for invalid in ("0", "-1", str(2**63)):
            assert client.get(f"/projects/{invalid}/brief").status_code == 422
        access.remove_project_member(conn, project["id"], 2)
        hidden = client.get(url, headers={"X-Athena-Actor": "2"})
        missing = client.get("/projects/999999/brief", headers={"X-Athena-Actor": "2"})
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json() == {"detail": "no such project"}
        for response in (hidden, missing):
            assert response.headers["cache-control"] == "private, no-store"
            assert {"authorization", "x-athena-actor"} <= {
                x.strip().lower() for x in response.headers["vary"].split(",")
            }
    after = {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
        for table in before
    }
    assert before == after


def test_fixed_limit_bounds_statements_materialized_rows_and_report_parses(
    board, monkeypatch
):
    import sqlite3
    from athena.aegis import lease_commands

    _, conn, project, member = board

    def add_rows(project_id, count):
        for _ in range(count):
            issue = _issue(conn, project_id)
            _handoff(conn, issue)
            _hold(conn, issue)
            _block(conn, issue, _issue(conn, None))
            _report(conn, issue, {"verdict": "HOLD"})
            # More historical records must not become more parsed payloads.
            _report(conn, issue, {"findings": {"P2": 0}})

    add_rows(project["id"], 3)
    original_parser = lease_commands.reported_completion
    stats = {"parsed": 0, "materialized": 0}

    def parser(event):
        stats["parsed"] += 1
        return original_parser(event)

    monkeypatch.setattr(lease_commands, "reported_completion", parser)

    def row_factory(cursor, row):
        stats["materialized"] += 1
        return sqlite3.Row(cursor, row)

    def measure():
        stats.update(parsed=0, materialized=0)
        statements = []
        conn.row_factory = row_factory
        conn.set_trace_callback(statements.append)
        try:
            packet = _brief(conn, project, member, limit=2)
        finally:
            conn.row_factory = sqlite3.Row
            conn.set_trace_callback(None)
        assert not any(
            s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for s in statements
        )
        return packet, (len(statements), stats["materialized"], stats["parsed"])

    small, before = measure()
    add_rows(project["id"], 37)
    unrelated = projects.create_project(conn, name="Unrelated", key="UNR", created_by=1)
    add_rows(unrelated["id"], 40)
    large, after = measure()
    assert before == after
    assert after[2] == 2
    assert after[1] <= 20  # Includes scalar aggregates and root authorization rows.
    for lane in ("active_claims", "blockers", "open_handoffs", "latest_completions"):
        assert small[lane]["visible_total"] == 3
        assert large[lane]["visible_total"] == 40
        assert large[lane]["returned"] == large[lane]["limit"] == 2
        assert large[lane]["clipped"]
    for lane in ("active_claims", "blockers", "open_handoffs"):
        assert [x["issue"]["id"] for x in small[lane]["items"]] == [
            x["issue"]["id"] for x in large[lane]["items"]
        ]
    newest = [x["event_id"] for x in large["latest_completions"]["items"]]
    assert newest == sorted(newest, reverse=True)


def test_hidden_newer_completion_does_not_replace_latest_visible_event(board):
    from athena.core import activity

    _, conn, project, member = board
    secret = projects.create_project(
        conn, name="Former private", key="OLD", created_by=1
    )
    projects.set_visibility(conn, secret["id"], "private")
    issue = _issue(conn, project["id"])
    visible = _report(conn, issue, {"verdict": "HOLD"})
    hidden = activity.record(
        conn,
        actor_id=1,
        verb="claim_completed",
        target_kind="issue",
        target_id=issue["id"],
        detail="generation " + "b" * 32,
        issue_project_ids=[secret["id"]],
    )
    assert hidden["id"] > visible["id"]
    packet = _brief(conn, project, member)
    assert packet["latest_completions"]["visible_total"] == 1
    assert packet["latest_completions"]["items"][0]["event_id"] == visible["id"]
    admin = _brief(conn, project, _actor(conn))
    assert admin["latest_completions"]["items"][0]["event_id"] == hidden["id"]
    assert not admin["latest_completions"]["items"][0]["report_available"]


def test_resumed_handoff_and_archived_report_are_excluded(board):
    from athena.aegis import issue_etags, lease_commands

    _, conn, project, member = board
    issue = _issue(conn, project["id"])
    handoff = _handoff(conn, issue)
    actor = _actor(conn)
    lease = lease_commands.claim_issue(
        conn,
        actor=actor,
        issue_id=issue["id"],
        if_match=[issue_etags.current_etag(conn, issue)],
    )
    lease_commands.resume_claim_handoff(
        conn,
        actor=actor,
        issue_id=issue["id"],
        handoff_token=handoff["handoff_token"],
        generation=lease["generation"],
    )
    _report(conn, issue, {"verdict": "PASS"})
    assert _brief(conn, project, member)["open_handoffs"]["visible_total"] == 0
    conn.execute(
        "UPDATE issues SET archived_at=CURRENT_TIMESTAMP WHERE id=?", (issue["id"],)
    )
    conn.commit()
    assert _brief(conn, project, member)["latest_completions"]["visible_total"] == 0


def test_default_clock_is_captured_once_and_naive_injection_is_utc(board, monkeypatch):
    from athena.aegis import project_brief

    _, conn, project, member = board
    _hold(conn, _issue(conn, project["id"]), expiry="2035-01-02 12:00:01")
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(True)
            return NOW

    monkeypatch.setattr(project_brief, "datetime", Clock)
    packet = project_brief.build_project_brief(
        conn, project_id=project["id"], actor=member
    )
    assert calls == [True]
    assert packet["active_claims"]["visible_total"] == 1
    naive = project_brief.build_project_brief(
        conn, project_id=project["id"], actor=member, now=NOW.replace(tzinfo=None)
    )
    assert naive["observed_at"] == packet["observed_at"]


def test_nested_reader_failure_preserves_callers_pending_work(board, monkeypatch):
    from athena.aegis import lease_commands

    path, conn, project, member = board
    _report(conn, _issue(conn, project["id"]), {"verdict": "HOLD"})
    conn.execute("UPDATE projects SET name='Pending' WHERE id=?", (project["id"],))

    def broken_parser(event):
        raise RuntimeError("synthetic parser interruption")

    monkeypatch.setattr(lease_commands, "reported_completion", broken_parser)
    with pytest.raises(RuntimeError, match="synthetic parser interruption"):
        _brief(conn, project, member)
    assert conn.in_transaction
    assert (
        conn.execute(
            "SELECT name FROM projects WHERE id=?", (project["id"],)
        ).fetchone()[0]
        == "Pending"
    )
    other = db.connect(path)
    assert (
        other.execute(
            "SELECT name FROM projects WHERE id=?", (project["id"],)
        ).fetchone()[0]
        == "Brief"
    )
    other.close()
    conn.rollback()


def test_same_project_blocker_uses_strict_preview_shape_over_http(board):
    from pydantic import ValidationError
    from athena.aegis.project_brief_models import ProjectBrief

    path, conn, project, member = board
    work = _issue(conn, project["id"])
    blocker = _issue(conn, project["id"], status="unknown-status")
    _block(conn, work, blocker)
    packet = _brief(conn, project, member)
    ProjectBrief.model_validate(packet)
    packet["blockers"]["items"][0]["blocked_by"]["body"] = "must not be accepted"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectBrief.model_validate(packet)
    with TestClient(create_app(path)) as client:
        response = client.get(f"/projects/{project['id']}/brief")
    assert response.status_code == 200
    preview = response.json()["blockers"]["items"][0]["blocked_by"]
    assert "project_id" not in preview  # Only outside-root blockers need it.
    assert preview["status_category"] is None
    assert preview["archived_at"] is None
    assert preview["key"] == blocker["key"]
