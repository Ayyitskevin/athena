"""Rule-driven writes carry lineage, and a firing is idempotent across a crash.

An automated change used to land on the trail attributed to the Automation actor
but with no record of WHICH rule fired it or WHICH event triggered it — so a
replay couldn't tell an operator "this status change was rule 3 reacting to event
88". And because the engine advances its cursor only at end-of-pass, a crash after
a comment committed would re-post it on restart. These pin both: every rule action
stamps a per-firing run id (grouping + fork point + parent run), and re-running the
same (rule, event) firing makes no second change.
"""
from fastapi.testclient import TestClient

from athena.aegis import automation
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="auto_lineage.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann"})


def _rule(db_file, action_type, params, trigger_verb="created"):
    """Create a rule directly (as the existing automation tests do)."""
    conn = db.connect(db_file)
    rule = automation.create_rule(
        conn,
        name=f"r-{action_type}",
        trigger_verb=trigger_verb,
        action_type=action_type,
        action_params=params,
        created_by=1,
    )
    conn.commit()
    conn.close()
    return rule


def _activity(db_file, **kw):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT id, actor_id, verb, run_id, parent_run_id, forked_from_event_id "
        "FROM activity ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_rule_action_is_stamped_with_its_firing_lineage(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        member = client.post(
            "/users", json={"email": "m@e.com", "name": "Mem", "role": "member"},
            headers=H1,
        ).json()
        _rule(db_file, "assign", {"user_id": member["id"]})
        # An agent creates an issue under its OWN run — the automation reaction
        # should chain onto that run as parent.
        issue = client.post(
            "/issues",
            json={"title": "auto-assign me"},
            headers={**H1, "X-Athena-Run": "agent-run-1"},
        ).json()

        conn = db.connect(db_file)
        created = conn.execute(
            "SELECT id FROM activity WHERE verb='created' AND target_id=?",
            (issue["id"],),
        ).fetchone()["id"]
        fired = automation.process_pending(
            conn, executor=lambda c, r, e: automation.execute_action(
                c, r, e, actor_id=automation.system_actor_id(c)
            )
        )
        conn.commit()
        conn.close()
    assert fired == 1

    assigned = [e for e in _activity(db_file) if e["verb"] == "assigned"]
    assert len(assigned) == 1
    ev = assigned[0]
    # The firing run id names the rule AND the triggering event…
    assert ev["run_id"] == automation.automation_run_id(1, created)
    # …its fork point is that triggering event…
    assert ev["forked_from_event_id"] == created
    # …and it chains onto the run that caused the trigger.
    assert ev["parent_run_id"] == "agent-run-1"


def test_comment_firing_is_idempotent_across_a_reprocessed_event(tmp_path):
    # The crash case in miniature: the same (rule, event) firing is executed twice
    # (as a restart from a stale cursor would). The comment must be posted ONCE.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        _rule(db_file, "comment", {"body": "auto ack"})
        issue = client.post("/issues", json={"title": "ack me"}, headers=H1).json()

        conn = db.connect(db_file)
        created = conn.execute(
            "SELECT id FROM activity WHERE verb='created' AND target_id=?",
            (issue["id"],),
        ).fetchone()["id"]
        actor_id = automation.system_actor_id(conn)
        rule = {"id": 1, "action_type": "comment", "action_params": {"body": "auto ack"}}
        event = {"id": created, "target_id": issue["id"], "run_id": None}
        first = automation.execute_action(conn, rule, event, actor_id=actor_id)
        conn.commit()
        # Re-run the identical firing (what a restart from a stale cursor does).
        second = automation.execute_action(conn, rule, event, actor_id=actor_id)
        conn.commit()
        n_comments = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE issue_id=?", (issue["id"],)
        ).fetchone()[0]
        n_events = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE verb='commented' AND target_id=?",
            (issue["id"],),
        ).fetchone()[0]
        conn.close()

    assert first is True  # first firing acts
    assert second is False  # the re-run is a no-op
    assert n_comments == 1  # exactly one comment, not two
    assert n_events == 1  # and exactly one trail entry


def test_full_pass_is_idempotent_when_the_cursor_does_not_advance(tmp_path):
    # End to end: run the same pass twice WITHOUT advancing the cursor between
    # them (a crash-before-cursor-commit). The rule fires once in effect.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        _rule(db_file, "comment", {"body": "hello"})
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()

    def one_pass():
        conn = db.connect(db_file)
        try:
            n = automation.process_pending(
                conn,
                executor=lambda c, r, e: automation.execute_action(
                    c, r, e, actor_id=automation.system_actor_id(c)
                ),
            )
            conn.commit()
            return n
        finally:
            conn.close()

    # Reset the cursor to 0 before the second pass to simulate "cursor never moved".
    one_pass()
    conn = db.connect(db_file)
    conn.execute("UPDATE automation_state SET cursor = 0 WHERE id = 1")
    conn.commit()
    conn.close()
    one_pass()  # re-processes the same events

    conn = db.connect(db_file)
    n_comments = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE issue_id=?", (issue["id"],)
    ).fetchone()[0]
    conn.close()
    assert n_comments == 1  # the reprocessed pass added no duplicate
