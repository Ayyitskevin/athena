"""A desk cursor cannot pass the newest event, and cursors already past it are
repaired once (spec A3, review finding LOOP-2, decision D14).

The cursor is advance-only (migration 0073), which is right for a bookmark and
fatal for a typo: before A3, `POST /desk/cursor {"after_id": 999999}` returned
200, every later correction was refused as a rewind, and `events_since_cursor`
read 0 forever, so one mistaken call blinded that seat's change signal with no
error and no way back.

The contract these pin: an `after_id` above the newest activity id is refused
with the machine code `cursor_ahead` and the newest id the CALLER can see (not
the table's maximum, which would disclose fleet activity volume); the newest id
itself is still accepted; an ordinary rewind is refused exactly as before; and
migration 0085 rewinds an already-poisoned cursor once, leaving the 0073
trigger as it was so a rewind stays impossible afterwards.
"""

import asyncio
from contextlib import closing
import json
import sqlite3

from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from athena.aegis import desk, desk_commands
from athena.core import activity, db, recovery, users
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient, AthenaError

H1 = {"X-Athena-Actor": "1"}
REPAIR = "0085_desk_cursor_repair.sql"


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com"):
    onboarded = client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()
    return onboarded["user"]["id"], {
        "Authorization": f"Bearer {onboarded['token']['token']}"
    }


def _newest_event_id(db_file):
    with closing(db.connect(db_file)) as conn:
        return conn.execute("SELECT MAX(id) FROM activity").fetchone()[0]


def _seat_with_hidden_newer_events(client, db_file):
    """An agent whose newest visible event is older than the table's newest.

    The agent sees its own issue; onboarding a second agent afterwards appends
    events it cannot see, so the two maxima differ and a leak would show."""
    _bootstrap(client)
    agent_id, bearer = _agent(client)
    client.post("/issues", json={"title": "mine"}, headers=bearer)
    visible = client.get("/desk", headers=bearer).json()["signals"][
        "latest_visible_event_id"
    ]
    _agent(client, email="luna@e.com")
    newest = _newest_event_id(db_file)
    assert visible is not None and newest > visible
    return agent_id, bearer, visible, newest


def test_an_id_past_the_newest_event_is_refused_with_cursor_ahead(tmp_path):
    db_file = tmp_path / "ahead.db"
    with TestClient(create_app(db_file)) as client:
        _, bearer, visible, newest = _seat_with_hidden_newer_events(client, db_file)

        for ahead in (newest + 1, 999999):
            refused = client.post(
                "/desk/cursor", json={"after_id": ahead}, headers=bearer
            )
            assert refused.status_code == 422, refused.text
            body = refused.json()
            assert body["code"] == "cursor_ahead"
            # The newest id THIS caller can see, never the table's maximum.
            assert body["latest_visible_event_id"] == visible
            assert str(visible) in body["detail"]
            assert str(newest) not in body["detail"]

        # Nothing moved: the refused value never became the bookmark.
        board = client.get("/desk", headers=bearer).json()
        assert board["cursor"] is None
        assert board["signals"]["events_since_cursor"] is None


def test_the_newest_event_id_itself_is_accepted(tmp_path):
    db_file = tmp_path / "newest.db"
    with TestClient(create_app(db_file)) as client:
        _, bearer, visible, newest = _seat_with_hidden_newer_events(client, db_file)

        at_visible = client.post(
            "/desk/cursor", json={"after_id": visible}, headers=bearer
        )
        assert at_visible.status_code == 200, at_visible.text
        assert at_visible.json()["after_id"] == visible

        # The bound is the trail's newest row, so a reader whose visibility later
        # narrows can still re-acknowledge the position it already holds.
        at_newest = client.post(
            "/desk/cursor", json={"after_id": newest}, headers=bearer
        )
        assert at_newest.status_code == 200, at_newest.text
        assert at_newest.json()["after_id"] == newest


def test_an_ordinary_rewind_is_still_refused_as_before(tmp_path):
    db_file = tmp_path / "rewind.db"
    with TestClient(create_app(db_file)) as client:
        _, bearer, visible, _ = _seat_with_hidden_newer_events(client, db_file)
        client.post("/desk/cursor", json={"after_id": visible}, headers=bearer)

        rewind = client.post("/desk/cursor", json={"after_id": 1}, headers=bearer)
        assert rewind.status_code == 409
        assert rewind.json() == {
            "detail": f"cursor is already at {visible}; a cursor may not move backward",
            "code": "conflict",
        }


def test_the_command_refuses_ahead_with_a_code_and_the_visible_id(tmp_path):
    db_file = tmp_path / "command.db"
    with TestClient(create_app(db_file)) as client:
        agent_id, _, visible, newest = _seat_with_hidden_newer_events(client, db_file)
    with closing(db.connect(db_file)) as conn:
        actor = users.get_user(conn, agent_id)
        with pytest.raises(desk_commands.DeskCommandError) as caught:
            desk_commands.advance_cursor(conn, actor=actor, after_id=newest + 1)
        assert caught.value.kind == "invalid"
        assert caught.value.code == "cursor_ahead"
        assert caught.value.latest_visible_event_id == visible
        assert desk_commands.STATUS_BY_KIND[caught.value.kind] == 422
        # The refusal left nothing behind inside its transaction.
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM agent_cursors").fetchone()[0] == 0


def test_mcp_surfaces_the_cursor_ahead_refusal(tmp_path):
    db_file = tmp_path / "mcp.db"
    app = create_app(db_file)
    with TestClient(app) as admin:
        _, bearer, visible, newest = _seat_with_hidden_newer_events(admin, db_file)
        api = AthenaClient(client=TestClient(app, headers=bearer))

        with pytest.raises(AthenaError) as refused:
            api.advance_desk_cursor(after_id=newest + 1)
        assert refused.value.status_code == 422
        assert refused.value.code == "cursor_ahead"
        assert str(visible) in str(refused.value.detail)

        server = mcp_server.build_server(
            api,
            scopes=frozenset({"read", "issue:write"}),
            profile=mcp_server.PROFILES["seat"],
        )
        with pytest.raises(ToolError) as caught:
            asyncio.run(
                server.call_tool("advance_desk_cursor", {"after_id": newest + 1})
            )
        error = json.loads(str(caught.value).split("ATHENA_ERROR_JSON=", 1)[1])
        assert error["code"] == "cursor_ahead"
        assert error["status_code"] == 422

        moved = api.advance_desk_cursor(after_id=visible)
        assert moved["after_id"] == visible


# --- the one-time repair (D14) ---------------------------------------------


def _baseline(tmp_path, monkeypatch, migration_inventory_through):
    """Point the runner at the inventory before the repair; return the full one."""
    packaged = db.MIGRATIONS_DIR
    migration_inventory_through("0084_title_link_bindings.sql")
    assert not (db.MIGRATIONS_DIR / REPAIR).exists()
    # Through the repair, not every packaged migration: a later one is not this
    # test's business, and the repair need not be the newest.
    full = db.MIGRATIONS_DIR.parent / "through-repair"
    full.mkdir()
    for migration in sorted(packaged.glob("*.sql")):
        if migration.name <= REPAIR:
            (full / migration.name).write_bytes(migration.read_bytes())
    assert (full / REPAIR).exists()
    return full


def _repair_and_later(full) -> list[str]:
    """What migrating to the full inventory applies: the repair, then every
    migration written after it (0086 onward), in order."""
    return sorted(path.name for path in full.glob("*.sql") if path.name >= REPAIR)


def _human(conn, user_id, role="admin"):
    conn.execute(
        "INSERT INTO users(id, email, name, role) VALUES (?, ?, ?, ?)",
        (user_id, f"u{user_id}@example.invalid", f"User {user_id}", role),
    )


def _trigger_sql(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
        "AND name = 'agent_cursors_advance_only'"
    ).fetchone()
    return None if row is None else row[0]


def test_repair_clamps_a_poisoned_cursor_and_keeps_the_trigger(
    tmp_path, monkeypatch, migration_inventory_through
):
    full = _baseline(tmp_path, monkeypatch, migration_inventory_through)
    with closing(db.connect(tmp_path / "poisoned.db")) as conn:
        db.migrate(conn)
        trigger_before = _trigger_sql(conn)
        _human(conn, 1)
        _human(conn, 2, role="member")
        for n in range(3):
            activity.record(
                conn,
                actor_id=1,
                verb="noted",
                target_kind="user",
                target_id=n + 1,
                commit=True,
            )
        newest = conn.execute("SELECT MAX(id) FROM activity").fetchone()[0]
        conn.executemany(
            "INSERT INTO agent_cursors(user_id, name, after_id) VALUES (?, 'desk', ?)",
            [(1, 999999), (2, newest - 1)],
        )
        conn.commit()

        monkeypatch.setattr(db, "MIGRATIONS_DIR", full)
        assert db.migrate(conn) == _repair_and_later(full)
        assert db.migration_status(conn).current

        positions = dict(
            conn.execute("SELECT user_id, after_id FROM agent_cursors").fetchall()
        )
        # The poisoned cursor is clamped to the newest event; a sane one is untouched.
        assert positions == {1: newest, 2: newest - 1}

        # The advance-only trigger is back, byte for byte as 0073 wrote it.
        assert _trigger_sql(conn) == trigger_before
        with pytest.raises(sqlite3.IntegrityError, match="may not move backward"):
            conn.execute("UPDATE agent_cursors SET after_id = 1 WHERE user_id = 1")
        conn.rollback()

        # The upgraded schema is exactly the packaged one (the doctor's drift check).
        recovery.validate_current_database(conn)

        # The repaired seat can see change again and move forward again.
        actor = users.get_user(conn, 1)
        for n in range(2):
            activity.record(
                conn,
                actor_id=1,
                verb="noted",
                target_kind="user",
                target_id=n + 1,
                commit=True,
            )
        board = desk.build_desk(conn, actor=actor)
        assert board["signals"]["events_since_cursor"] == 2
        latest = board["signals"]["latest_visible_event_id"]
        moved = desk_commands.advance_cursor(conn, actor=actor, after_id=latest)
        assert moved["after_id"] == latest
        assert desk.build_desk(conn, actor=actor)["signals"]["events_since_cursor"] == 0


def test_repair_on_an_empty_trail_forgets_the_position(
    tmp_path, monkeypatch, migration_inventory_through
):
    # 0073's CHECK (after_id > 0) forbids a zero position, and with no event at
    # all every stored position is past the end. The honest remaining state is
    # "never acknowledged": the desk then reads null, not a blind zero.
    full = _baseline(tmp_path, monkeypatch, migration_inventory_through)
    with closing(db.connect(tmp_path / "empty.db")) as conn:
        db.migrate(conn)
        _human(conn, 1)
        conn.execute(
            "INSERT INTO agent_cursors(user_id, name, after_id) VALUES (1, 'desk', 7)"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 0

        monkeypatch.setattr(db, "MIGRATIONS_DIR", full)
        assert db.migrate(conn) == _repair_and_later(full)
        assert conn.execute("SELECT COUNT(*) FROM agent_cursors").fetchone()[0] == 0
        assert _trigger_sql(conn) is not None
