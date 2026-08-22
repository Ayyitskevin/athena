"""The live automation executor + loop guard (slice 2).

process_pending fires matching rules through an executor; this slice supplies the REAL
one (execute_action) and a system 'Automation' actor it attributes every change to. An
automated change is indistinguishable in the log from a human one except for the actor —
which is exactly what lets the engine skip its OWN events (the loop guard) so a rule
whose action emits a new event can't run away. Driven through run_pass (the background
loop itself is disabled in tests by conftest).
"""

from fastapi.testclient import TestClient

from athena.aegis import automation, comments, contributors, issues, statuses
from athena.core import activity, db, labels, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _setup(client):
    client.post(
        "/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"}
    )  # user 1 admin
    client.post(
        "/users", json={"email": "b@e.com", "name": "Bob", "password": "pw"}, headers=H1
    )  # user 2


def test_system_actor_get_or_create(tmp_path):
    db_file = tmp_path / "sa.db"
    with TestClient(create_app(db_file)):
        conn = db.connect(db_file)
        a = automation.system_actor_id(conn)
        b = automation.system_actor_id(conn)
        assert a == b  # get-or-create, never a duplicate
        actor = users.get_user(conn, a)
        assert actor["name"] == "Automation" and actor["is_agent"] is True


def test_all_action_types_apply_and_are_attributed(tmp_path):
    db_file = tmp_path / "act.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        pid = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H1
        ).json()["id"]
        iid = client.post(
            "/issues", json={"title": "x", "project_id": pid}, headers=H1
        ).json()["id"]

        conn = db.connect(db_file)
        start = issues.get_issue(conn, iid)
        target_status = next(
            n for n in statuses.status_names(conn, pid) if n != start["status"]
        )
        cond = {"project_id": pid}
        automation.create_rule(
            conn,
            name="assign",
            trigger_verb="created",
            action_type="assign",
            conditions=cond,
            action_params={"user_id": 2},
            created_by=1,
        )
        automation.create_rule(
            conn,
            name="status",
            trigger_verb="created",
            action_type="set_status",
            conditions=cond,
            action_params={"status": target_status},
            created_by=1,
        )
        automation.create_rule(
            conn,
            name="label",
            trigger_verb="created",
            action_type="add_label",
            conditions=cond,
            action_params={"label": "auto"},
            created_by=1,
        )
        automation.create_rule(
            conn,
            name="comment",
            trigger_verb="created",
            action_type="comment",
            conditions=cond,
            action_params={"body": "triaged"},
            created_by=1,
        )
        automation.create_rule(
            conn,
            name="contrib",
            trigger_verb="created",
            action_type="add_contributor",
            conditions=cond,
            action_params={"user_id": 2},
            created_by=1,
        )

        # One 'created' event fires all five rules.
        assert automation.run_pass(db_file) == 5

        c = db.connect(db_file)
        sysid = automation.system_actor_id(c)
        issue = issues.get_issue(c, iid)
        assert issue["assignee_id"] == 2 and issue["status"] == target_status
        assert [lbl["name"] for lbl in labels.labels_for_issue(c, iid)] == ["auto"]
        cs = comments.list_comments(c, iid)
        assert (
            len(cs) == 1 and cs[0]["body"] == "triaged" and cs[0]["author_id"] == sysid
        )
        assert any(m["user_id"] == 2 for m in contributors.list_contributors(c, iid))
        # Every change is attributed to the Automation actor in the audit trail.
        verbs = {
            e["verb"]
            for e in activity.list_activity(c, target_kind="issue", target_id=iid)
            if e["actor_id"] == sysid
        }
        assert {
            "assigned",
            "changed_status",
            "labeled",
            "commented",
            "added_contributor",
        } <= verbs

        # A second pass changes nothing: the engine skips its own events (loop guard) and
        # the actions are already in their desired state.
        assert automation.run_pass(db_file) == 0


def test_loop_guard_prevents_runaway(tmp_path):
    db_file = tmp_path / "loop.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        # A '*' rule whose action emits a new event (a comment) would re-trigger on its
        # OWN 'commented' event forever without the guard.
        automation.create_rule(
            conn,
            name="tick",
            trigger_verb="*",
            action_type="comment",
            action_params={"body": "tick"},
            created_by=1,
        )
        iid = client.post("/issues", json={"title": "x"}, headers=H1).json()["id"]

        automation.run_pass(db_file)
        n1 = len(comments.list_comments(db.connect(db_file), iid))
        assert n1 == 1  # fired once on the human 'created' event
        # Subsequent passes are stable — the automation's own 'commented' events are
        # skipped, so it never comments on its own comment.
        automation.run_pass(db_file)
        automation.run_pass(db_file)
        assert len(comments.list_comments(db.connect(db_file), iid)) == n1


def test_imported_history_never_fires_a_rule(tmp_path):
    # WHY: an imported event (a forge delivery, an import bundle) is foreign
    # history — something Athena was TOLD, not work it did. Before this fix the
    # engine's scan read every row with no imported_at filter, so an imported
    # forge_commit fired a wildcard rule: it moved the issue and minted writes
    # off history Athena never made. The scan now excludes imported rows in SQL.
    db_file = tmp_path / "imported.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        pid = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H1
        ).json()["id"]
        iid = client.post(
            "/issues", json={"title": "x", "project_id": pid}, headers=H1
        ).json()["id"]
        # Drain the native setup events (issue 'created' et al.) past the cursor
        # before the rule exists, so the passes below see ONLY the planted rows.
        assert automation.run_pass(db_file) == 0

        conn = db.connect(db_file)
        start_status = issues.get_issue(conn, iid)["status"]
        target_status = next(
            n for n in statuses.status_names(conn, pid) if n != start_status
        )
        automation.create_rule(
            conn,
            name="wild",
            trigger_verb="*",
            action_type="set_status",
            action_params={"status": target_status},
            created_by=1,
        )
        # What a forge delivery lands as (0041): same verb and target as the
        # native control below, marked imported.
        activity.record(
            conn,
            actor_id=1,
            verb="forge_commit",
            target_kind="issue",
            target_id=iid,
            detail="gh: commit",
            imported_at="2026-01-01 00:00:00",
        )
        conn.close()

        assert automation.run_pass(db_file) == 0
        after = issues.get_issue(db.connect(db_file), iid)
        assert after["status"] == start_status  # nothing fired, nothing moved

        # Control: the identical NATIVE event fires the same rule — the filter
        # excludes foreign history, not the verb.
        conn = db.connect(db_file)
        activity.record(
            conn,
            actor_id=1,
            verb="forge_commit",
            target_kind="issue",
            target_id=iid,
            detail="native",
        )
        conn.close()
        assert automation.run_pass(db_file) == 1
        fired = issues.get_issue(db.connect(db_file), iid)
        assert fired["status"] == target_status


def test_idle_pass_does_not_consume_admin_bootstrap(tmp_path):
    db_file = tmp_path / "boot.db"
    with TestClient(create_app(db_file)) as client:
        # Automation defaults ON, so on a fresh install the loop ticks before anyone
        # signs up. An idle pass must NOT create the 'Automation' user: that would make
        # count_users() > 0 and turn the first-user-is-admin bootstrap into a 401, locking
        # the deploy out. The actor is created only when a rule actually fires.
        automation.run_pass(db_file)
        conn = db.connect(db_file)
        assert users.count_users(conn) == 0
        # The bootstrap is intact: the first human created (unauthenticated) is admin.
        first = client.post(
            "/users", json={"email": "a@e.com", "name": "A", "password": "pw"}
        )
        assert first.status_code == 201 and first.json()["role"] == "admin"


def test_invalid_action_params_fail_soft(tmp_path):
    db_file = tmp_path / "soft.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        # A rule pointing at a nonexistent user / invalid status makes no change and
        # doesn't strand the engine — the event is still consumed (cursor advances).
        automation.create_rule(
            conn,
            name="bad-assign",
            trigger_verb="created",
            action_type="assign",
            action_params={"user_id": 9999},
            created_by=1,
        )
        automation.create_rule(
            conn,
            name="bad-status",
            trigger_verb="created",
            action_type="set_status",
            action_params={"status": "nope"},
            created_by=1,
        )
        iid = client.post("/issues", json={"title": "x"}, headers=H1).json()["id"]
        before = issues.get_issue(conn, iid)["status"]
        # The pass completes without raising; the no-op actions make NO change to the
        # issue (the unknown user / invalid status fail soft, not crash).
        automation.run_pass(db_file)
        after = issues.get_issue(db.connect(db_file), iid)
        assert after["assignee_id"] is None and after["status"] == before
        # The cursor still advanced past the event, so a second pass has nothing to do.
        assert automation.run_pass(db_file) == 0


def _radio_env(monkeypatch, tmp_path, configured=True):
    if configured:
        keyfile = tmp_path / "buzz.key"
        keyfile.write_text("SECKEY=" + "ab" * 32 + "\n")
        monkeypatch.setenv("ATHENA_BUZZ_RELAY_URL", "http://relay.test:3000")
        monkeypatch.setenv("ATHENA_BUZZ_CLI", "/bin/false")
        monkeypatch.setenv("ATHENA_BUZZ_KEY_FILE", str(keyfile))
    else:
        for var in ("ATHENA_BUZZ_RELAY_URL", "ATHENA_BUZZ_CLI", "ATHENA_BUZZ_KEY_FILE"):
            monkeypatch.delenv(var, raising=False)


def _buzz_rule_and_event(client, conn, action_params):
    rule = automation.create_rule(
        conn,
        name="ping",
        trigger_verb="created",
        action_type="buzz_message",
        action_params=action_params,
        created_by=1,
    )
    iid = client.post("/issues", json={"title": "Radio me"}, headers=H1).json()["id"]
    event = next(
        e
        for e in activity.list_events(conn, target_kind="issue", target_id=iid)
        if e["verb"] == "created"
    )
    return rule, event, iid


def test_buzz_message_action_sends_a_delivery_not_a_write(tmp_path, monkeypatch):
    # The action is an outbound DELIVERY (webhook semantics): the send goes out,
    # but NOTHING lands in the DB — no activity event, no trail row. The audited
    # thing is the rule's lifecycle, not each send.
    db_file = tmp_path / "buzz.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        _radio_env(monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(
            automation.buzz_radio,
            "send_channel_message",
            lambda **kw: (sent.append(kw), {"status": "sent", "detail": "ok"})[1],
        )
        rule, event, iid = _buzz_rule_and_event(client, conn, {})
        before = [e["id"] for e in activity.list_events(conn, limit=200)]
        assert (
            automation.execute_action(
                conn, rule, event, actor_id=automation.system_actor_id(conn)
            )
            is True
        )
        after = [e["id"] for e in activity.list_events(conn, limit=200)]
        assert after == before  # a delivery, not a write
        assert len(sent) == 1
        kw = sent[0]
        # Channel defaults to the assign channel at fire time; no mention unless
        # configured; the body is composed from the event + issue, deterministically.
        from athena import config

        assert kw["channel"] == config.buzz_assign_channel()
        assert kw["mention"] is None
        assert "ATHENA_EVENT created" in kw["content"]
        assert "Radio me" in kw["content"]
        assert f"/aegis/issues/{iid}" in kw["content"]


def test_buzz_message_action_params_pass_through(tmp_path, monkeypatch):
    db_file = tmp_path / "buzz2.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        _radio_env(monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(
            automation.buzz_radio,
            "send_channel_message",
            lambda **kw: (sent.append(kw), {"status": "sent", "detail": "ok"})[1],
        )
        params = {
            "channel": "e29bb951-d272-4822-a8e5-ffac2f9462f2",
            "mention": "cd" * 32,
            "note": "P0 — needs eyes.",
        }
        rule, event, _ = _buzz_rule_and_event(client, conn, params)
        assert automation.execute_action(conn, rule, event, actor_id=1) is True
        kw = sent[0]
        assert kw["channel"] == params["channel"]
        assert kw["mention"] == params["mention"]
        assert "Note: P0 — needs eyes." in kw["content"]


def test_buzz_message_unconfigured_radio_fails_soft_or_closed(tmp_path, monkeypatch):
    db_file = tmp_path / "buzz3.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        _radio_env(monkeypatch, tmp_path, configured=False)
        rule, event, _ = _buzz_rule_and_event(client, conn, {})
        # Event rules fail soft: no radio → skipped firing, engine keeps going.
        assert automation.execute_action(conn, rule, event, actor_id=1) is False
        # Scheduled receipts pass fail_closed=True: the miss must be VISIBLE
        # failure state, not mistaken for a completed no-op.
        import pytest as _pytest

        with _pytest.raises(ValueError, match="not configured"):
            automation.execute_action(conn, rule, event, actor_id=1, fail_closed=True)


def test_buzz_message_failed_send_raises_on_both_paths(tmp_path, monkeypatch):
    # Superseded the earlier fail-soft expectation: a delivery leaves no trace of
    # its own, so returning False on a failed send meant a lost message on a rule
    # that still read green. Both paths raise now.
    db_file = tmp_path / "buzz4.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        _radio_env(monkeypatch, tmp_path)
        monkeypatch.setattr(
            automation.buzz_radio,
            "send_channel_message",
            lambda **kw: {"status": "failed", "detail": "relay down"},
        )
        rule, event, _ = _buzz_rule_and_event(client, conn, {})
        import pytest as _pytest

        for closed in (False, True):
            with _pytest.raises(ValueError, match="relay down"):
                automation.execute_action(
                    conn, rule, event, actor_id=1, fail_closed=closed
                )


def test_buzz_message_rule_surfaces_missing_radio_as_configuration_error(
    tmp_path, monkeypatch
):
    db_file = tmp_path / "buzz5.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        _radio_env(monkeypatch, tmp_path, configured=False)
        automation.create_rule(
            conn,
            name="ping",
            trigger_verb="created",
            action_type="buzz_message",
            action_params={},
            created_by=1,
        )
        listed = automation.list_rules(conn)
        assert any(
            "buzz radio is not configured" in (r.get("configuration_error") or "")
            for r in listed
        )
        # With the radio configured the same rule reads clean.
        _radio_env(monkeypatch, tmp_path, configured=True)
        listed = automation.list_rules(conn)
        assert all(
            "buzz radio" not in (r.get("configuration_error") or "") for r in listed
        )


def test_buzz_message_failed_send_is_always_visible_failure(tmp_path, monkeypatch):
    # A delivery leaves no trace of its own, so a failed send must never fail
    # SOFT the way the in-app actions do — an operator would see a green rule and
    # a lost message. It raises on both paths, which routes it through
    # process_pending into failure_count/last_error.
    db_file = tmp_path / "buzz-fail-visible.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        _radio_env(monkeypatch, tmp_path)
        monkeypatch.setattr(
            automation.buzz_radio,
            "send_channel_message",
            lambda **kw: {"status": "failed", "detail": "connection refused"},
        )
        rule, event, _ = _buzz_rule_and_event(client, conn, {})
        import pytest as _pytest

        with _pytest.raises(ValueError, match="connection refused"):
            automation.execute_action(conn, rule, event, actor_id=1)

        # And the engine turns that raise into visible rule state rather than
        # stranding the cursor: one pass, rule flagged, cursor still advances.
        def executor(c, r, e):
            automation.execute_action(c, r, e, actor_id=automation.system_actor_id(c))

        client.post("/issues", json={"title": "another"}, headers=H1)
        automation.process_pending(conn, executor=executor)
        flagged = next(r for r in automation.list_rules(conn) if r["id"] == rule["id"])
        assert flagged["failure_count"] >= 1
        assert "connection refused" in (flagged["last_error"] or "")


def test_buzz_message_rejected_as_a_scheduled_action():
    # Scheduled occurrences retry until they succeed and dedup by reading the
    # trail for the firing's run id. A delivery writes no such row, so a retry
    # would re-send — refused at the boundary instead.
    error = automation.validate_rule(
        trigger_verb=automation.SCHEDULE_TRIGGER_VERB,
        action_type="buzz_message",
        conditions={},
        action_params={},
        trigger_type="schedule",
        schedule_at="2026-09-01T09:00:00Z",
    )
    assert error is not None and "scheduled" in error
    # The same action stays valid on the event path, which has no retry.
    assert (
        automation.validate_rule(
            trigger_verb="created",
            action_type="buzz_message",
            conditions={},
            action_params={},
        )
        is None
    )
