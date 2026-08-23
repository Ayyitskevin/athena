"""Event-source commands authorize themselves.

Registering an event source hands a stranger-facing credential to whoever holds
the secret — a trust decision, which is why the routes have always required the
admin role AND an admin-scoped token. But that gate lived only in the transport's
`admin_actor` dependency: the commands took a bare actor id and trusted their
caller, so anything reaching them directly minted or revoked inbound-history
credentials unchecked.

The commands now own the rule (`_admin_or_refuse`, mirroring
`run_control_commands._admin`). These tests call them with **no transport in
front** — the only vantage from which the move is observable. The wire statuses
are already pinned by the forge route tests and did not change.
"""

from fastapi.testclient import TestClient
import pytest

from athena.core import db, event_source_commands
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}

ADMIN = {"id": 1, "role": "admin"}
MEMBER = {"id": 2, "role": "member"}
#: An admin ROLE whose bearer token carries only the read scope — the exact
#: combination the docstring in forge_api singles out as insufficient.
READ_SCOPED_ADMIN = {"id": 1, "role": "admin", "_token_scopes": frozenset({"read"})}


@pytest.fixture()
def conn(tmp_path):
    app = create_app(tmp_path / "esauth.db")
    with TestClient(app) as c:
        c.post(
            "/users",
            json={"email": "a@e.com", "name": "Ann", "password": "pw"},
            headers=H1,
        )
        c.post("/users", json={"email": "b@e.com", "name": "Bob"}, headers=H1)
    connection = db.connect(tmp_path / "esauth.db")
    yield connection
    connection.close()


def _register(conn, actor, name="forge"):
    return event_source_commands.register_source(
        conn, actor=actor, name=name, kind="github", host="github.com"
    )


def test_a_non_admin_cannot_register_a_source(conn):
    with pytest.raises(event_source_commands.EventSourceCommandError) as caught:
        _register(conn, MEMBER)
    assert caught.value.kind == "forbidden"
    assert "admin role" in caught.value.detail


def test_an_admin_role_with_a_read_scoped_token_is_refused(conn):
    # WHY: the role says who you are; the scope says what THIS credential may do.
    # An admin holding a read token must not mint an inbound-history credential.
    with pytest.raises(event_source_commands.EventSourceCommandError) as caught:
        _register(conn, READ_SCOPED_ADMIN)
    assert caught.value.kind == "forbidden"
    assert "token scope" in caught.value.detail


def test_no_actor_is_unauthorized_not_forbidden(conn):
    with pytest.raises(event_source_commands.EventSourceCommandError) as caught:
        _register(conn, None)
    assert caught.value.kind == "unauthorized"


def test_the_admin_path_still_works_and_the_gate_covers_all_three_commands(conn):
    source = _register(conn, ADMIN)
    assert source["name"] == "forge" and "secret" in source

    with pytest.raises(event_source_commands.EventSourceCommandError) as caught:
        event_source_commands.set_source_enabled(
            conn, actor=MEMBER, source_id=source["id"], enabled=False
        )
    assert caught.value.kind == "forbidden"
    with pytest.raises(event_source_commands.EventSourceCommandError) as caught:
        event_source_commands.delete_source(conn, actor=MEMBER, source_id=source["id"])
    assert caught.value.kind == "forbidden"

    paused = event_source_commands.set_source_enabled(
        conn, actor=ADMIN, source_id=source["id"], enabled=False
    )
    assert paused["enabled"] == 0
    assert (
        event_source_commands.delete_source(conn, actor=ADMIN, source_id=source["id"])
        is True
    )
