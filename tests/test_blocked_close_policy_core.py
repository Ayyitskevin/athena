"""Core invariants for the opt-in blocked-issue close policy.

The shared issue command is the enforcement point for REST, browser, board,
bulk, MCP, and automation writes. These tests stay below those transports so a
new adapter cannot accidentally invent weaker policy semantics.
"""

import pytest

from athena.aegis import (
    dependencies,
    issue_activity,
    issue_commands,
    issue_etags,
    issues,
    projects,
    statuses,
)
from athena.core import db, notifications, run_context, users


def _setup(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    admin = users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        role=users.ADMIN_ROLE,
    )
    human = users.create_user(
        conn,
        email="human@example.com",
        name="Human",
        role=users.MEMBER_ROLE,
    )
    agent = users.create_user(
        conn,
        email="agent@example.com",
        name="Agent",
        role=users.MEMBER_ROLE,
        is_agent=True,
    )
    watcher = users.create_user(
        conn,
        email="watcher@example.com",
        name="Watcher",
        role=users.MEMBER_ROLE,
    )
    return conn, admin, human, agent, watcher


def _project(conn, actor, name, *, policy=False):
    project = projects.create_project(
        conn,
        name=name,
        key=name[:3].upper(),
        created_by=actor["id"],
    )
    if policy:
        conn.execute(
            "UPDATE projects SET block_agent_closes_when_blocked = 1 WHERE id = ?",
            (project["id"],),
        )
        conn.commit()
        project = projects.get_project(conn, project["id"])
        assert project is not None
    return project


def _issue(conn, actor, title, project, *, status=None):
    return issue_commands.create_issue(
        conn,
        actor=actor,
        title=title,
        project_id=project["id"],
        status=status,
    )


def _block(conn, blocker, target, actor):
    reason, inserted = dependencies.add_link(
        conn,
        from_id=blocker["id"],
        to_id=target["id"],
        relation="blocks",
        created_by=actor["id"],
    )
    assert (reason, inserted) == (None, True)


def _assert_policy_error(error):
    assert error.kind == "conflict"
    assert error.code == issue_commands.BLOCKED_CLOSE_POLICY_ERROR_CODE
    assert error.detail == issue_commands.BLOCKED_CLOSE_POLICY_ERROR_DETAIL


def test_policy_defaults_disabled_and_does_not_change_existing_agent_closes(tmp_path):
    # WHY: migration rollout must preserve today's behavior until an operator
    # explicitly opts a project into governance.
    conn, admin, _, agent, _ = _setup(tmp_path / "default-off.db")
    project = _project(conn, admin, "Default")
    assert project["block_agent_closes_when_blocked"] is False
    target = _issue(conn, agent, "Target", project)
    blocker = _issue(conn, agent, "Blocker", project)
    _block(conn, blocker, target, agent)

    updated = issue_commands.update_issue(
        conn, actor=agent, issue_id=target["id"], status="done"
    )

    assert updated["status"] == "done"
    conn.close()


@pytest.mark.parametrize("forged_override", [False, True])
def test_agent_close_is_atomic_stable_and_does_not_leak_hidden_blocker(
    tmp_path, forged_override
):
    # WHY: an agent cannot turn a human-only escape hatch into its own bypass,
    # and the rejection cannot name a blocker hidden in a private project.
    conn, admin, _, agent, _ = _setup(tmp_path / f"agent-{forged_override}.db")
    if forged_override:
        agent = users.set_role(conn, agent["id"], users.ADMIN_ROLE)
        assert agent is not None
    governed = _project(conn, admin, "Governed", policy=True)
    hidden = _project(conn, admin, "Hidden")
    conn.execute(
        "UPDATE projects SET visibility = 'private' WHERE id = ?", (hidden["id"],)
    )
    conn.commit()
    target = _issue(conn, agent, "Visible target", governed)
    blocker = _issue(conn, admin, "Secret blocker title", hidden)
    _block(conn, blocker, target, admin)
    baseline_events = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    with pytest.raises(issue_commands.IssueCommandError) as rejected:
        issue_commands.update_issue(
            conn,
            actor=agent,
            issue_id=target["id"],
            status="done",
            override_blocked_close=forged_override,
        )

    _assert_policy_error(rejected.value)
    assert "Secret blocker title" not in rejected.value.detail
    assert issues.get_issue(conn, target["id"])["status"] == "open"
    assert (
        conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == baseline_events
    )
    assert not conn.in_transaction
    conn.close()


def test_custom_done_categories_resolve_the_canonical_blocker(tmp_path):
    # WHY: custom status names must not fork dependency semantics. Both the target
    # close and blocker resolution use categories, never a literal "done" string.
    conn, admin, _, agent, _ = _setup(tmp_path / "custom-done.db")
    governed = _project(conn, admin, "Governed", policy=True)
    blocker_project = _project(conn, admin, "Upstream")
    assert statuses.add_status(conn, governed["id"], "shipped", "done") is None
    assert statuses.add_status(conn, blocker_project["id"], "verified", "done") is None
    target = _issue(conn, agent, "Target", governed)
    blocker = _issue(conn, admin, "Blocker", blocker_project)
    _block(conn, blocker, target, admin)

    with pytest.raises(issue_commands.IssueCommandError) as rejected:
        issue_commands.update_issue(
            conn, actor=agent, issue_id=target["id"], status="shipped"
        )
    _assert_policy_error(rejected.value)

    issue_commands.update_issue(
        conn, actor=admin, issue_id=blocker["id"], status="verified"
    )
    assert dependencies.open_blockers(conn, target["id"]) == []
    updated = issue_commands.update_issue(
        conn, actor=agent, issue_id=target["id"], status="shipped"
    )
    assert updated["status"] == "shipped"
    conn.close()


@pytest.mark.parametrize("human_key", ["admin", "member"])
def test_eligible_human_requires_explicit_audited_override_with_lineage_and_etag(
    tmp_path, human_key
):
    # WHY: the override is a deliberate, attributable human decision. It must
    # honor the same revision guard and fan-out as the status change it permits.
    conn, admin, human, _, watcher = _setup(tmp_path / f"human-{human_key}.db")
    actor = admin if human_key == "admin" else human
    governed = _project(conn, admin, "Governed", policy=True)
    target = _issue(conn, actor, "Target", governed)
    blocker = _issue(conn, admin, "Blocker", governed)
    _block(conn, blocker, target, admin)
    notifications.watch(conn, watcher["id"], "issue", target["id"])

    stale_tag = issue_etags.current_etag(conn, target)
    target = issue_commands.update_issue(
        conn, actor=actor, issue_id=target["id"], title="Target revised"
    )
    current_tag = issue_etags.current_etag(conn, target)
    with pytest.raises(issue_commands.IssueCommandError) as stale:
        issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=target["id"],
            status="done",
            override_blocked_close=True,
            if_match=[stale_tag],
        )
    assert stale.value.kind == "precondition_failed"

    with pytest.raises(issue_commands.IssueCommandError) as missing:
        issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=target["id"],
            status="done",
            if_match=[current_tag],
        )
    _assert_policy_error(missing.value)
    cutoff = conn.execute("SELECT MAX(id) FROM activity").fetchone()[0]
    run_token = run_context.set_run_id("human-policy-override")
    parent_token = run_context.set_parent_run_id("operator-session")
    fork_token = run_context.set_forked_from_event_id(cutoff)
    try:
        updated = issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=target["id"],
            status="done",
            override_blocked_close=True,
            if_match=[current_tag],
        )
    finally:
        run_context.reset_forked_from_event_id(fork_token)
        run_context.reset_parent_run_id(parent_token)
        run_context.reset_run_id(run_token)

    assert updated["status"] == "done"
    assert issue_etags.current_etag(conn, updated) != current_tag
    events = [
        dict(row)
        for row in conn.execute(
            "SELECT id, actor_id, verb, detail, run_id, parent_run_id, "
            "forked_from_event_id FROM activity WHERE target_kind = 'issue' "
            "AND target_id = ? AND id > ? ORDER BY id",
            (target["id"], cutoff),
        )
    ]
    assert [event["verb"] for event in events] == [
        "changed_status",
        "overrode_blocked_issue_close",
    ]
    assert events[1]["detail"] == ""
    assert {event["actor_id"] for event in events} == {actor["id"]}
    assert {event["run_id"] for event in events} == {"human-policy-override"}
    assert {event["parent_run_id"] for event in events} == {"operator-session"}
    assert {event["forked_from_event_id"] for event in events} == {cutoff}
    notified_event_ids = {
        row["event_id"]
        for row in conn.execute(
            "SELECT event_id FROM notifications WHERE user_id = ?",
            (watcher["id"],),
        )
    }
    assert {event["id"] for event in events} <= notified_event_ids
    conn.close()


@pytest.mark.parametrize("destination_kind", ["disabled", "backlog"])
def test_agent_cannot_escape_enabled_policy_by_project_egress(
    tmp_path, destination_kind
):
    # WHY: moving an open blocked issue out of governance must not become a
    # two-step close-policy bypass, including the project-less backlog.
    conn, admin, _, agent, _ = _setup(tmp_path / f"egress-{destination_kind}.db")
    source = _project(conn, admin, "Source", policy=True)
    destination = (
        _project(conn, admin, "Destination") if destination_kind == "disabled" else None
    )
    target = _issue(conn, agent, "Target", source)
    blocker = _issue(conn, agent, "Blocker", source)
    _block(conn, blocker, target, agent)
    destination_counter = destination["issue_counter"] if destination else None
    baseline_events = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    with pytest.raises(issue_commands.IssueCommandError) as rejected:
        issue_commands.update_issue(
            conn,
            actor=agent,
            issue_id=target["id"],
            project_id=destination["id"] if destination else None,
            override_blocked_close=True,
        )

    _assert_policy_error(rejected.value)
    unchanged = issues.get_issue(conn, target["id"])
    assert unchanged["project_id"] == source["id"]
    if destination is not None:
        assert (
            projects.get_project(conn, destination["id"])["issue_counter"]
            == destination_counter
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == baseline_events
    )
    conn.close()


@pytest.mark.parametrize("enabled_side", ["destination", "both"])
def test_agent_nonclosing_move_into_or_between_enabled_projects_is_allowed(
    tmp_path, enabled_side
):
    # WHY: governance is a close gate, not a general agent-placement lock. Entry
    # into protection and protected-to-protected moves are harmless while open.
    conn, admin, _, agent, _ = _setup(tmp_path / f"allowed-{enabled_side}.db")
    source = _project(conn, admin, "Source", policy=enabled_side == "both")
    destination = _project(conn, admin, "Destination", policy=True)
    target = _issue(conn, agent, "Target", source)
    blocker = _issue(conn, agent, "Blocker", source)
    _block(conn, blocker, target, agent)

    moved = issue_commands.update_issue(
        conn,
        actor=agent,
        issue_id=target["id"],
        project_id=destination["id"],
    )

    assert moved["project_id"] == destination["id"]
    assert moved["status"] == "open"
    conn.close()


def test_project_only_move_that_implicitly_enters_done_category_is_still_a_close(
    tmp_path,
):
    # WHY: equal status names can mean different lifecycle categories per project.
    # A move from doing→done is a close even when no explicit status field exists.
    conn, admin, _, agent, _ = _setup(tmp_path / "implicit-close.db")
    source = _project(conn, admin, "Source")
    destination = _project(conn, admin, "Destination", policy=True)
    assert statuses.add_status(conn, source["id"], "ready", "doing") is None
    assert statuses.add_status(conn, destination["id"], "ready", "done") is None
    target = _issue(conn, agent, "Target", source, status="ready")
    blocker = _issue(conn, agent, "Blocker", source)
    _block(conn, blocker, target, agent)

    with pytest.raises(issue_commands.IssueCommandError) as rejected:
        issue_commands.update_issue(
            conn,
            actor=agent,
            issue_id=target["id"],
            project_id=destination["id"],
        )

    _assert_policy_error(rejected.value)
    unchanged = issues.get_issue(conn, target["id"])
    assert (unchanged["project_id"], unchanged["status"]) == (source["id"], "ready")
    conn.close()


def test_automation_actor_is_also_denied_and_cannot_request_override(tmp_path):
    # WHY: the in-process automation bypasses issue ownership, not governance.
    conn, admin, _, _, _ = _setup(tmp_path / "automation.db")
    automation_actor = users.create_user(
        conn,
        email=issue_commands.AUTOMATION_ACTOR_EMAIL,
        name="Automation",
        is_agent=True,
    )
    governed = _project(conn, admin, "Governed", policy=True)
    target = _issue(conn, admin, "Target", governed)
    blocker = _issue(conn, admin, "Blocker", governed)
    _block(conn, blocker, target, admin)

    with pytest.raises(issue_commands.IssueCommandError) as rejected:
        issue_commands.update_issue_as_automation(
            conn,
            actor_id=automation_actor["id"],
            issue_id=target["id"],
            status="done",
        )

    _assert_policy_error(rejected.value)
    assert issues.get_issue(conn, target["id"])["status"] == "open"
    conn.close()


def test_live_agent_flag_prevents_stale_human_snapshot_override(tmp_path):
    # WHY: a previously resolved actor dict must not remain a human escape hatch
    # after the durable identity has been converted to an agent.
    conn, admin, human, _, _ = _setup(tmp_path / "stale-actor.db")
    governed = _project(conn, admin, "Governed", policy=True)
    target = _issue(conn, human, "Target", governed)
    blocker = _issue(conn, admin, "Blocker", governed)
    _block(conn, blocker, target, admin)
    stale_human = dict(human)
    assert users.set_agent(conn, human["id"], True) is not None

    with pytest.raises(issue_commands.IssueCommandError) as rejected:
        issue_commands.update_issue(
            conn,
            actor=stale_human,
            issue_id=target["id"],
            status="done",
            override_blocked_close=True,
        )

    _assert_policy_error(rejected.value)
    assert issues.get_issue(conn, target["id"])["status"] == "open"
    conn.close()


@pytest.mark.parametrize("live_change", ["viewer", "paused"])
def test_live_authorization_prevents_stale_human_override(tmp_path, live_change):
    # WHY: a cached human identity must not retain override authority after its
    # durable account becomes read-only or paused.
    conn, admin, human, _, _ = _setup(tmp_path / f"stale-{live_change}.db")
    governed = _project(conn, admin, "Governed", policy=True)
    target = _issue(conn, human, "Target", governed)
    blocker = _issue(conn, admin, "Blocker", governed)
    _block(conn, blocker, target, admin)
    stale_human = dict(human)
    if live_change == "viewer":
        assert users.set_role(conn, human["id"], users.VIEWER_ROLE) is not None
    else:
        assert users.set_paused(conn, human["id"], True) is not None
    baseline_events = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    with pytest.raises(issue_commands.IssueCommandError) as rejected:
        issue_commands.update_issue(
            conn,
            actor=stale_human,
            issue_id=target["id"],
            status="done",
            override_blocked_close=True,
        )

    assert rejected.value.kind == "forbidden"
    assert issues.get_issue(conn, target["id"])["status"] == "open"
    assert (
        conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == baseline_events
    )
    conn.close()


def test_override_audit_failure_rolls_back_status_event_notifications_and_run(
    tmp_path, monkeypatch
):
    # WHY: the exception decision is load-bearing audit, not an after-the-fact log.
    conn, admin, human, _, watcher = _setup(tmp_path / "audit-rollback.db")
    governed = _project(conn, admin, "Governed", policy=True)
    target = _issue(conn, human, "Target", governed)
    blocker = _issue(conn, admin, "Blocker", governed)
    _block(conn, blocker, target, admin)
    notifications.watch(conn, watcher["id"], "issue", target["id"])
    baseline = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("activity", "notifications", "run_bindings")
    }
    real_recorder = issue_activity.record_blocked_close_override

    def fail_after_override(*args, **kwargs):
        real_recorder(*args, **kwargs)
        raise RuntimeError("injected override audit failure")

    monkeypatch.setattr(
        issue_activity, "record_blocked_close_override", fail_after_override
    )
    run_token = run_context.set_run_id("rolled-back-override")
    try:
        with pytest.raises(RuntimeError, match="injected override audit failure"):
            issue_commands.update_issue(
                conn,
                actor=human,
                issue_id=target["id"],
                status="done",
                override_blocked_close=True,
            )
    finally:
        run_context.reset_run_id(run_token)

    assert issues.get_issue(conn, target["id"])["status"] == "open"
    for table, count in baseline.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
    assert not conn.in_transaction
    conn.close()
