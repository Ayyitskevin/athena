"""The automation rules engine — data layer + matching + one engine pass (slice 1).

A rule reacts to an activity event (a trigger verb + target kind, optionally narrowed by
conditions on the target issue) and, when the live executor lands, performs an action.
This slice is the testable spine: CRUD, the _matches predicate, and process_pending —
which drains events after a single cursor, fires matching enabled rules through an
INJECTED executor, and advances the cursor (best-effort, at-most-once, with a loop guard
that skips a chosen actor's own events). Mirrors how webhooks.deliver_pending was built
before its background loop.
"""
from fastapi.testclient import TestClient

from athena.aegis import automation
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _setup(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})  # user 1 (admin)
    client.post("/users", json={"email": "b@e.com", "name": "Bob", "password": "pw"}, headers=H1)  # user 2


def test_rule_crud(tmp_path):
    db_file = tmp_path / "crud.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        r = automation.create_rule(
            conn, name="auto-assign", trigger_verb="created", action_type="assign",
            conditions={"project_id": 1}, action_params={"user_id": 2}, created_by=1,
        )
        # JSON blobs round-trip to dicts; enabled defaults true.
        assert r["enabled"] is True
        assert r["conditions"] == {"project_id": 1} and r["action_params"] == {"user_id": 2}
        assert automation.get_rule(conn, r["id"])["name"] == "auto-assign"
        assert [x["id"] for x in automation.list_rules(conn)] == [r["id"]]
        # Disable hides it from the enabled list but keeps it.
        automation.set_enabled(conn, r["id"], False)
        assert automation.get_rule(conn, r["id"])["enabled"] is False
        assert automation.list_rules(conn, enabled_only=True) == []
        # Delete.
        assert automation.delete_rule(conn, r["id"]) is True
        assert automation.get_rule(conn, r["id"]) is None
        assert automation.delete_rule(conn, r["id"]) is False


def test_matches_verb_kind_conditions(tmp_path):
    db_file = tmp_path / "match.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H1).json()["id"]
        iid = client.post("/issues", json={"title": "x", "project_id": pid}, headers=H1).json()["id"]
        conn = db.connect(db_file)
        ev = {"verb": "created", "target_kind": "issue", "target_id": iid}

        any_verb = automation.create_rule(conn, name="any", trigger_verb="*", action_type="comment", created_by=1)
        created = automation.create_rule(conn, name="c", trigger_verb="created", action_type="comment", created_by=1)
        other = automation.create_rule(conn, name="o", trigger_verb="changed_status", action_type="comment", created_by=1)
        assert automation._matches(conn, any_verb, ev) is True       # '*' fires on any verb
        assert automation._matches(conn, created, ev) is True        # exact verb
        assert automation._matches(conn, other, ev) is False         # different verb
        # Wrong target kind never fires.
        assert automation._matches(conn, any_verb, {**ev, "target_kind": "page"}) is False

        # Conditions are an AND of issue-field equalities resolved at match time.
        proj = automation.create_rule(conn, name="p", trigger_verb="created", action_type="comment", conditions={"project_id": pid}, created_by=1)
        proj_wrong = automation.create_rule(conn, name="pw", trigger_verb="created", action_type="comment", conditions={"project_id": pid + 999}, created_by=1)
        assert automation._matches(conn, proj, ev) is True
        assert automation._matches(conn, proj_wrong, ev) is False
        # A condition on a vanished target fails closed.
        assert automation._matches(conn, proj, {**ev, "target_id": 999999}) is False


def test_process_pending_fires_matches_and_advances_cursor(tmp_path):
    db_file = tmp_path / "engine.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H1).json()["id"]
        conn = db.connect(db_file)
        rule = automation.create_rule(
            conn, name="assign-in-P", trigger_verb="created", action_type="assign",
            conditions={"project_id": pid}, action_params={"user_id": 2}, created_by=1,
        )
        calls: list[tuple] = []

        def fake(c, r, e):
            calls.append((r["id"], e["target_id"], e["verb"]))

        # Two new issues: one in P (matches the condition), one in the backlog (doesn't).
        in_p = client.post("/issues", json={"title": "a", "project_id": pid}, headers=H1).json()["id"]
        client.post("/issues", json={"title": "b"}, headers=H1)  # backlog

        fired = automation.process_pending(conn, executor=fake)
        assert fired == 1 and calls == [(rule["id"], in_p, "created")]
        # The cursor advanced past the whole batch, so a second pass is a no-op.
        assert automation.process_pending(conn, executor=fake) == 0
        assert len(calls) == 1


def test_same_status_project_move_does_not_fire_status_rule(tmp_path):
    db_file = tmp_path / "project-move-trigger.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        source = client.post(
            "/projects",
            json={"name": "Source", "key": "SRC"},
            headers=H1,
        ).json()
        destination = client.post(
            "/projects",
            json={"name": "Destination", "key": "DST"},
            headers=H1,
        ).json()
        issue = client.post(
            "/issues",
            json={
                "title": "Move without status change",
                "project_id": source["id"],
            },
            headers=H1,
        ).json()
        conn = db.connect(db_file)
        automation.create_rule(
            conn,
            name="status-only",
            trigger_verb="changed_status",
            action_type="comment",
            action_params={"body": "status fired"},
            created_by=1,
        )
        assert automation.process_pending(
            conn, executor=lambda *_args: None
        ) == 0

        moved = client.put(
            f"/issues/{issue['id']}/project",
            json={"project_id": destination["id"]},
            headers=H1,
        )
        assert moved.status_code == 200
        calls: list[str] = []
        assert (
            automation.process_pending(
                conn,
                executor=lambda _conn, _rule, event: calls.append(
                    event["verb"]
                ),
            )
            == 0
        )
        assert calls == []
        conn.close()


def test_disabled_rule_and_loop_guard(tmp_path):
    db_file = tmp_path / "guard.db"
    with TestClient(create_app(db_file)) as client:
        _setup(client)
        conn = db.connect(db_file)
        rule = automation.create_rule(conn, name="r", trigger_verb="created", action_type="comment", created_by=1)
        calls: list[int] = []
        ex = lambda c, r, e: calls.append(1)  # noqa: E731

        # Disabled → never fires (the cursor still advances, draining the event).
        automation.set_enabled(conn, rule["id"], False)
        client.post("/issues", json={"title": "x"}, headers=H1)
        assert automation.process_pending(conn, executor=ex) == 0 and calls == []

        # Re-enabled, but skip_actor_id = the issue's author (user 1) → loop guard skips it.
        automation.set_enabled(conn, rule["id"], True)
        client.post("/issues", json={"title": "y"}, headers=H1)
        assert automation.process_pending(conn, executor=ex, skip_actor_id=1) == 0 and calls == []

        # Without the guard, the same kind of event fires.
        client.post("/issues", json={"title": "z"}, headers=H1)
        assert automation.process_pending(conn, executor=ex) == 1 and calls == [1]
