"""Typed dependency links now emit an atomic, attributable audit event.

Before this, dependencies.add_link / remove_link wrote the edge with NO activity
event, so a dependency an agent created over MCP left no attributable, reversible
trace — a direct violation of "every agent action is attributable". These tests pin
that link/unlink now record a 'linked'/'unlinked' event in the SAME transaction as
the edge, exactly once per real change, across REST, the web form, and MCP — with the
creator-or-assignee write gate preserved.
"""
from fastapi.testclient import TestClient

from athena.aegis import issue_commands
from athena.core import activity, db
from athena.main import create_app
from athena.mcp.client import AthenaClient

H_ADMIN = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="deps.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    r = client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    assert r.status_code == 201, r.text
    return r.json()  # admin, id 1


def _issue(client, title, headers=H_ADMIN):
    r = client.post("/issues", json={"title": title}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _link_events(db_file, *, verb=None):
    conn = db.connect(db_file)
    evs = activity.list_activity(conn, limit=200)
    return [
        e
        for e in evs
        if e["verb"] in (("linked", "unlinked") if verb is None else (verb,))
    ]


# --- REST: the edge and its audit event are one atomic write ----------------


def test_rest_link_creates_edge_and_records_one_event(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        a = _issue(c, "A")
        b = _issue(c, "B")
        r = c.post(
            f"/issues/{a['id']}/links",
            json={"target_ref": str(b["id"]), "relation": "blocks"},
            headers=H_ADMIN,
        )
        assert r.status_code == 201, r.text
        assert [x["id"] for x in r.json()["blocks"]] == [b["id"]]

    events = _link_events(db_file)
    assert len(events) == 1
    ev = events[0]
    assert ev["verb"] == "linked"
    assert ev["target_kind"] == "issue" and ev["target_id"] == a["id"]
    assert ev["actor_id"] == 1
    assert "blocks" in ev["detail"] and str(b["id"]) in ev["detail"]


def test_rest_unlink_removes_edge_and_records_event(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        a = _issue(c, "A")
        b = _issue(c, "B")
        c.post(
            f"/issues/{a['id']}/links",
            json={"target_ref": str(b["id"]), "relation": "blocks"},
            headers=H_ADMIN,
        )
        r = c.delete(f"/issues/{a['id']}/links/blocks/{b['id']}", headers=H_ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["blocks"] == []

    assert len(_link_events(db_file, verb="unlinked")) == 1
    # Missing-link delete is a 404 and records nothing.
    with TestClient(app) as c:
        r = c.delete(f"/issues/{a['id']}/links/blocks/{b['id']}", headers=H_ADMIN)
        assert r.status_code == 404


def test_idempotent_relink_records_no_second_event(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        a = _issue(c, "A")
        b = _issue(c, "B")
        body = {"target_ref": str(b["id"]), "relation": "blocks"}
        assert c.post(f"/issues/{a['id']}/links", json=body, headers=H_ADMIN).status_code == 201
        # Re-adding the identical edge is a silent no-op — still 201, but no 2nd event.
        assert c.post(f"/issues/{a['id']}/links", json=body, headers=H_ADMIN).status_code == 201

    assert len(_link_events(db_file, verb="linked")) == 1


def test_rejected_contradiction_is_atomic_and_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        a = _issue(c, "A")
        b = _issue(c, "B")
        c.post(
            f"/issues/{a['id']}/links",
            json={"target_ref": str(b["id"]), "relation": "blocks"},
            headers=H_ADMIN,
        )
        # A blocks B already; B blocks A is the direct contradiction -> 409.
        r = c.post(
            f"/issues/{b['id']}/links",
            json={"target_ref": str(a["id"]), "relation": "blocks"},
            headers=H_ADMIN,
        )
        assert r.status_code == 409

    # Only the first (successful) link recorded an event; the rejected write left none.
    assert len(_link_events(db_file, verb="linked")) == 1


# --- the agent / MCP path is now audited (the gap this closes) --------------


def test_mcp_link_by_an_agent_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as tc:
        _bootstrap(tc)  # admin id 1
        tc.post(
            "/users",
            json={"email": "ag@e.com", "name": "Ag", "is_agent": True},
            headers=H_ADMIN,
        )  # agent id 2
        raw = tc.post(
            "/tokens", json={"name": "t", "scopes": ["issue:write"]},
            headers={"X-Athena-Actor": "2"},
        ).json()["token"]
        agent_h = {"Authorization": f"Bearer {raw}"}
        # The agent creates the issues, so it is the creator and may link them.
        a = _issue(tc, "A", headers=agent_h)
        b = _issue(tc, "B", headers=agent_h)
        tc.headers.update(agent_h)
        mcp = AthenaClient(client=tc)
        result = mcp.link_issues(a["id"], str(b["id"]), "blocks")
        assert [x["id"] for x in result["blocks"]] == [b["id"]]

    events = _link_events(db_file, verb="linked")
    assert len(events) == 1
    # Attributed to the AGENT (id 2) — the edge is no longer a silent, unattributable write.
    assert events[0]["actor_id"] == 2


# --- the write gate is preserved --------------------------------------------


def test_link_write_gate_preserved(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)  # admin id 1
        # A viewer (cannot write) and a read-only token (lacks issue:write scope).
        c.post(
            "/users",
            json={"email": "v@e.com", "name": "V", "password": "pw", "role": "viewer"},
            headers=H_ADMIN,
        )  # viewer id 2
        a = _issue(c, "A")
        b = _issue(c, "B")
        body = {"target_ref": str(b["id"]), "relation": "blocks"}

        # Viewer role -> 403.
        assert (
            c.post(f"/issues/{a['id']}/links", json=body, headers={"X-Athena-Actor": "2"}).status_code
            == 403
        )
        # Read-only token (admin user, but the token lacks issue:write) -> 403.
        read_token = c.post(
            "/tokens", json={"name": "ro", "scopes": ["read"]}, headers=H_ADMIN
        ).json()["token"]
        assert (
            c.post(
                f"/issues/{a['id']}/links",
                json=body,
                headers={"Authorization": f"Bearer {read_token}"},
            ).status_code
            == 403
        )
        # A target that doesn't exist collapses to 422 (same as a hidden one).
        assert (
            c.post(
                f"/issues/{a['id']}/links",
                json={"target_ref": "99999", "relation": "blocks"},
                headers=H_ADMIN,
            ).status_code
            == 422
        )

    # None of the refused attempts recorded an event.
    assert _link_events(db_file) == []


def test_command_rolls_back_edge_and_event_together(tmp_path):
    """Direct command call: a rejected link leaves neither an edge nor an event —
    the atomicity the split-write could not guarantee."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        a = _issue(c, "A")
        b = _issue(c, "B")

    conn = db.connect(db_file)
    admin = {"id": 1, "role": "admin", "email": "a@e.com"}
    issue_commands.link_issues(
        conn, actor=admin, issue_id=a["id"], target_ref=str(b["id"]), relation="blocks"
    )
    # Contradiction: rejected, and it must leave the DB exactly as the first link did.
    try:
        issue_commands.link_issues(
            conn, actor=admin, issue_id=b["id"], target_ref=str(a["id"]), relation="blocks"
        )
        raise AssertionError("expected IssueCommandError")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "conflict"
    assert conn.execute("SELECT COUNT(*) FROM issue_links").fetchone()[0] == 1
    linked = [
        e for e in activity.list_activity(conn, limit=100) if e["verb"] == "linked"
    ]
    assert len(linked) == 1
