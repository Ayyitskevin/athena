"""The automation run-id namespace is server-reserved on the client header path.

The automation engine's per-firing idempotency guard treats "an activity row
already carries this firing's run id" as "this firing already happened"
(automation._already_fired). The firing run id is fully predictable
(`automation:rule-N:event-M`), and client run ids were stored verbatim from the
`X-Athena-Run` header — so a client with a write scope could pre-stamp that id on
its own event and silently suppress the rule from ever firing. The request
middleware now drops a client-supplied run id in the reserved `automation:`
namespace (run_context.set_client_run_id), while the engine keeps minting those
ids in-process. These pin: the forgery no longer suppresses a firing, a reserved
client run id is dropped to untagged, and ordinary client run ids still ride
through.
"""

from fastapi.testclient import TestClient

from athena.aegis import automation
from athena.core import db, run_context
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="auto_reserve.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann"})


def _rule(db_file, action_type, params, trigger_verb="created"):
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


def test_reserved_prefix_helpers():
    assert run_context.is_reserved_run_id("automation:rule-1:event-2") is True
    assert run_context.is_reserved_run_id("automation:schedule:rule-1:slot-x") is True
    assert run_context.is_reserved_run_id("agent-run-1") is False
    assert run_context.is_reserved_run_id(None) is False


def test_client_cannot_stamp_a_reserved_run_id(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        forged = "automation:rule-1:event-999"
        issue = client.post(
            "/issues",
            json={"title": "pre-stamp attempt"},
            headers={**H1, "X-Athena-Run": forged},
        ).json()
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT run_id FROM activity WHERE verb='created' AND target_id=?",
        (issue["id"],),
    ).fetchone()
    conn.close()
    # The reserved run id was dropped — the event is untagged, not stamped into
    # the automation namespace where it could be mistaken for a firing.
    assert row["run_id"] is None


def test_forged_run_id_does_not_suppress_the_firing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "role": "member"},
            headers=H1,
        ).json()
        _rule(db_file, "assign", {"user_id": member["id"]})

        # An agent creates the trigger event, then tries to poison the exact
        # firing run id the engine will use for rule 1 reacting to that event.
        conn = db.connect(db_file)
        next_event_id = (
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0] + 1
        )
        conn.close()
        forged = automation.automation_run_id(1, next_event_id)
        issue = client.post(
            "/issues",
            json={"title": "auto-assign me"},
            headers={**H1, "X-Athena-Run": forged},
        ).json()

        conn = db.connect(db_file)
        created = conn.execute(
            "SELECT id FROM activity WHERE verb='created' AND target_id=?",
            (issue["id"],),
        ).fetchone()["id"]
        # The create landed at the predicted id, so the forged run id targeted the
        # real firing — but it was dropped, so no row carries it yet.
        assert created == next_event_id
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM activity WHERE run_id=?", (forged,)
            ).fetchone()[0]
            == 0
        )
        fired = automation.process_pending(
            conn,
            executor=lambda c, r, e: automation.execute_action(
                c, r, e, actor_id=automation.system_actor_id(c)
            ),
        )
        conn.commit()
        assigned = conn.execute(
            "SELECT run_id FROM activity WHERE verb='assigned' AND target_id=?",
            (issue["id"],),
        ).fetchall()
        conn.close()

    # The rule fired despite the forgery attempt, stamping the firing run id itself.
    assert fired == 1
    assert [r["run_id"] for r in assigned] == [forged]


def test_ordinary_client_run_id_still_rides_through(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        issue = client.post(
            "/issues",
            json={"title": "normal run"},
            headers={**H1, "X-Athena-Run": "agent-run-7"},
        ).json()
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT run_id FROM activity WHERE verb='created' AND target_id=?",
        (issue["id"],),
    ).fetchone()
    conn.close()
    assert row["run_id"] == "agent-run-7"
