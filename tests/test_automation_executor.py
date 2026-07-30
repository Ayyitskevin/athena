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
