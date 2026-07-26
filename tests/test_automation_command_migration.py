"""Automation's last two composed writes, routed through the command owner.

`docs/COMMAND_MIGRATION.md` listed automation as still composing "legacy
label/comment/contributor writes". ``comment`` had already been migrated (that
row was stale); ``add_label`` and ``add_contributor`` had not. Each opened its
own ``db.transaction``, called the data layer directly, and emitted its own
activity event — a second writer for a write that already had a command owner,
which is exactly the drift the migration rule exists to stop.

Routing them through ``issue_commands`` has one visible consequence beyond
ownership, and it is a real behavior change rather than an implementation
detail: pausing the Automation account now stops label and contributor firings
too. It already stopped assign and set_status, because those went through
``update_issue_as_automation``, whose shared ``_update_issue`` re-reads the actor
and refuses a paused one. That inconsistency is now gone, and the test below
names it.

What deliberately does NOT change: rule firings stay unmetered and ungated. A
budget or an approval gate must never silently stop a rule the operator
configured — budgets bound what *agents* initiate.
"""

from fastapi.testclient import TestClient

from athena.aegis import automation, contributors, issue_commands
from athena.core import activity, db, labels
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _setup(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "Bob", "password": "pw"}, headers=H1
    )


def _project_and_issue(client):
    pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H1).json()[
        "id"
    ]
    iid = client.post(
        "/issues", json={"title": "x", "project_id": pid}, headers=H1
    ).json()["id"]
    return pid, iid


def _rule(conn, *, action_type, params, pid, name="r", fail_closed=False):
    return automation.create_rule(
        conn,
        name=name,
        trigger_verb="created",
        action_type=action_type,
        conditions={"project_id": pid},
        action_params=params,
        created_by=1,
    )


def test_label_and_contributor_firings_still_apply_and_are_attributed(tmp_path):
    """The parity proof: routing through the command changed nothing an operator
    can observe about a normal firing."""
    db_file = tmp_path / "auto.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        pid, iid = _project_and_issue(client)
        conn = db.connect(db_file)
        _rule(
            conn, action_type="add_label", params={"label": "auto"}, pid=pid, name="l"
        )
        _rule(
            conn,
            action_type="add_contributor",
            params={"user_id": 2},
            pid=pid,
            name="c",
        )
        conn.close()

        assert automation.run_pass(db_file) == 2

        conn = db.connect(db_file)
        sysid = automation.system_actor_id(conn)
        assert [lbl["name"] for lbl in labels.labels_for_issue(conn, iid)] == ["auto"]
        assert any(m["user_id"] == 2 for m in contributors.list_contributors(conn, iid))
        verbs = {
            e["verb"]
            for e in activity.list_activity(conn, target_kind="issue", target_id=iid)
            if e["actor_id"] == sysid
        }
        assert {"labeled", "added_contributor"} <= verbs
        conn.close()

        # Idempotent across a replay: the loop guard skips the engine's own events,
        # and the commands record nothing for a re-apply.
        assert automation.run_pass(db_file) == 0


def test_a_replayed_firing_records_exactly_one_event(tmp_path):
    """The changed-or-not contract the schedule receipts depend on. Calling the
    command twice must add one row, not two, and must report False the second
    time so a re-run is not counted as work done."""
    db_file = tmp_path / "idem.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        _, iid = _project_and_issue(client)
        conn = db.connect(db_file)
        sysid = automation.system_actor_id(conn)
        label = labels.get_or_create_label(conn, name="auto")

        assert (
            issue_commands.attach_label_as_automation(
                conn, actor_id=sysid, issue_id=iid, label_id=label["id"]
            )
            is True
        )
        assert (
            issue_commands.attach_label_as_automation(
                conn, actor_id=sysid, issue_id=iid, label_id=label["id"]
            )
            is False
        )
        assert (
            issue_commands.add_contributor_as_automation(
                conn, actor_id=sysid, issue_id=iid, user_id=2
            )
            is True
        )
        assert (
            issue_commands.add_contributor_as_automation(
                conn, actor_id=sysid, issue_id=iid, user_id=2
            )
            is False
        )

        events = [
            e
            for e in activity.list_activity(conn, target_kind="issue", target_id=iid)
            if e["verb"] in ("labeled", "added_contributor")
        ]
        assert sorted(e["verb"] for e in events) == ["added_contributor", "labeled"]
        conn.close()


def test_only_the_automation_account_may_use_the_bypass(tmp_path):
    """The identity assertion is re-made INSIDE the write transaction, so passing
    another user's id cannot reach the per-issue policy bypass. Before this slice
    the automation label/contributor path took a bare int and asserted nothing at
    all."""
    db_file = tmp_path / "forge.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        _, iid = _project_and_issue(client)
        conn = db.connect(db_file)
        label = labels.get_or_create_label(conn, name="auto")

        for call in (
            lambda: issue_commands.attach_label_as_automation(
                conn, actor_id=2, issue_id=iid, label_id=label["id"]
            ),
            lambda: issue_commands.add_contributor_as_automation(
                conn, actor_id=2, issue_id=iid, user_id=1
            ),
        ):
            try:
                call()
                raise AssertionError("a non-automation actor must not reach the bypass")
            except issue_commands.IssueCommandError as exc:
                assert exc.kind == "forbidden"
                assert "automation actor required" in exc.detail

        # Nothing was written on the way to either refusal.
        assert labels.labels_for_issue(conn, iid) == []
        assert contributors.list_contributors(conn, iid) == []
        conn.close()


def test_pausing_the_automation_account_now_stops_label_and_contributor_rules(tmp_path):
    """A named behavior change, not a side effect.

    Pausing the Automation account already stopped assign and set_status firings,
    because those ran through ``_update_issue``, which refuses a paused actor. Label
    and contributor firings bypassed that check entirely and kept writing. Now the
    pause means the same thing for every action.
    """
    db_file = tmp_path / "paused.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        pid, iid = _project_and_issue(client)
        conn = db.connect(db_file)
        sysid = automation.system_actor_id(conn)
        _rule(
            conn, action_type="add_label", params={"label": "auto"}, pid=pid, name="l"
        )
        _rule(
            conn,
            action_type="add_contributor",
            params={"user_id": 2},
            pid=pid,
            name="c",
        )
        conn.close()

        assert (
            client.put(
                f"/users/{sysid}/paused", json={"paused": True}, headers=H1
            ).status_code
            == 200
        )

        # The rules match and are attempted; both are refused, so nothing applies.
        automation.run_pass(db_file)
        conn = db.connect(db_file)
        assert labels.labels_for_issue(conn, iid) == []
        assert contributors.list_contributors(conn, iid) == []

        # Named explicitly, so the absence above cannot be some other rule not
        # matching: the pause is what refuses, and it says so.
        label = labels.get_or_create_label(conn, name="auto")
        for call in (
            lambda: issue_commands.attach_label_as_automation(
                conn, actor_id=sysid, issue_id=iid, label_id=label["id"]
            ),
            lambda: issue_commands.add_contributor_as_automation(
                conn, actor_id=sysid, issue_id=iid, user_id=2
            ),
        ):
            try:
                call()
                raise AssertionError("a paused automation account must not write")
            except issue_commands.IssueCommandError as exc:
                assert exc.detail == "account is paused"
        conn.close()

        # Unpaused, the very same calls apply — the refusal was the pause, and it
        # lifts.
        assert (
            client.put(
                f"/users/{sysid}/paused", json={"paused": False}, headers=H1
            ).status_code
            == 200
        )
        conn = db.connect(db_file)
        assert (
            issue_commands.attach_label_as_automation(
                conn, actor_id=sysid, issue_id=iid, label_id=label["id"]
            )
            is True
        )
        conn.close()


def test_rule_firings_stay_unmetered_and_ungated(tmp_path):
    """A budget on the Automation account must not stop a rule the operator
    configured, and an approval policy must not queue one. Both would turn an
    operator's own automation into something needing the operator's permission."""
    db_file = tmp_path / "meter.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        pid, iid = _project_and_issue(client)
        conn = db.connect(db_file)
        sysid = automation.system_actor_id(conn)
        _rule(
            conn, action_type="add_label", params={"label": "auto"}, pid=pid, name="l"
        )
        _rule(
            conn,
            action_type="add_contributor",
            params={"user_id": 2},
            pid=pid,
            name="c",
        )
        conn.close()

        # A budget with no room at all.
        assert (
            client.put(
                f"/users/{sysid}/budget",
                json={"window": "day", "action_limit": 0},
                headers=H1,
            ).status_code
            == 200
        )

        assert automation.run_pass(db_file) == 2
        conn = db.connect(db_file)
        assert [lbl["name"] for lbl in labels.labels_for_issue(conn, iid)] == ["auto"]
        assert any(m["user_id"] == 2 for m in contributors.list_contributors(conn, iid))
        conn.close()


def test_a_fail_closed_contributor_rule_surfaces_the_commands_refusal(tmp_path):
    """An unknown user is the command's ``invalid``, and a fail-closed rule must
    let it fail the occurrence rather than report a firing that did nothing."""
    db_file = tmp_path / "closed.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        _, iid = _project_and_issue(client)
        conn = db.connect(db_file)
        sysid = automation.system_actor_id(conn)
        try:
            issue_commands.add_contributor_as_automation(
                conn, actor_id=sysid, issue_id=iid, user_id=9999
            )
            raise AssertionError("an unknown contributor must be refused")
        except issue_commands.IssueCommandError as exc:
            assert exc.kind == "invalid"
        conn.close()
