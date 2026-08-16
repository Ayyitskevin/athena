"""Declared fleet roster: systemd verdict + Athena rows, never 'alive'."""

from fastapi.testclient import TestClient

from athena.core import db, fleet_roster, users
from athena.main import create_app


def _probe(unit: str) -> dict:
    states = {
        "buzz-acp-claude.service": {
            "load_state": "loaded",
            "active_state": "active",
            "sub_state": "running",
        },
        "buzz-acp-grok.service": {
            "load_state": "not-found",
            "active_state": "inactive",
            "sub_state": "dead",
        },
    }
    return states.get(
        unit,
        {
            "load_state": "loaded",
            "active_state": "inactive",
            "sub_state": "dead",
        },
    )


def test_unit_verdict_words():
    assert fleet_roster.unit_verdict({"load_state": "not-found"}) == "missing"
    assert fleet_roster.unit_verdict({"active_state": "active"}) == "active"
    assert fleet_roster.unit_verdict({"active_state": "unobserved"}) == "unobserved"
    assert fleet_roster.unit_verdict({"active_state": "inactive"}) == "inactive"


def test_roster_joins_athena_and_flags_undeclared(tmp_path):
    conn = db.connect(tmp_path / "roster.db")
    db.migrate(conn)
    users.create_user(conn, email="claude@agents.local", name="Claude", is_agent=True)
    users.create_user(conn, email="stray@agents.local", name="Stray", is_agent=True)
    declared = (
        {
            "slug": "claude",
            "name": "Claude",
            "email": "claude@agents.local",
            "unit": "buzz-acp-claude.service",
            "buzz_pubkey": "aa" * 32,
            "kind": "seat",
        },
        {
            "slug": "grok",
            "name": "Grok",
            "email": "grok@agents.local",
            "unit": "buzz-acp-grok.service",
            "buzz_pubkey": "bb" * 32,
            "kind": "seat",
        },
    )
    roster = fleet_roster.build_roster(conn, probe=_probe, declared=declared)
    assert roster["schema"] == fleet_roster.SCHEMA
    assert "alive" in roster["semantics"]["does_not_assert"]
    by_slug = {seat["slug"]: seat for seat in roster["seats"]}
    assert by_slug["claude"]["unit_verdict"] == "active"
    assert by_slug["claude"]["athena"]["id"] == 1
    assert by_slug["grok"]["unit_verdict"] == "missing"
    assert by_slug["grok"]["athena"] is None
    assert roster["undeclared_athena_agents"][0]["email"] == "stray@agents.local"
    assert roster["summary"]["unit_active"] == 1
    assert roster["summary"]["unit_missing"] == 1
    assert roster["summary"]["undeclared_agents"] == 1
    conn.close()


def test_admin_fleet_page_and_json(tmp_path):
    app = create_app(tmp_path / "fleet-admin.db")
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "admin@e.com", "name": "Admin", "password": "secret"},
        )
        client.post(
            "/users/onboard_agent",
            json={"name": "Grok", "scopes": ["read"]},
            headers={"X-Athena-Actor": "1"},
        )
        anon = client.get("/admin/fleet")
        assert anon.status_code == 401
        client.post(
            "/users",
            json={
                "email": "member@e.com",
                "name": "Member",
                "password": "secret",
                "role": "member",
            },
            headers={"X-Athena-Actor": "1"},
        )
        client.post(
            "/login",
            data={"email": "member@e.com", "password": "secret"},
            follow_redirects=False,
        )
        denied = client.get("/admin/fleet")
        assert denied.status_code == 403
        client.cookies.clear()
        client.post(
            "/login",
            data={"email": "admin@e.com", "password": "secret"},
            follow_redirects=False,
        )
        page = client.get("/admin/fleet")
        assert page.status_code == 200, page.text
        assert "Fleet roster" in page.text
        assert "buzz-acp-grok.service" in page.text
        assert (
            "does not claim anyone" in page.text.lower()
            or "does not claim" in page.text
        )
        payload = client.get("/admin/fleet.json")
        assert payload.status_code == 200
        body = payload.json()
        assert body["schema"] == fleet_roster.SCHEMA
        slugs = {seat["slug"] for seat in body["seats"]}
        assert {"claude", "codex", "kimi", "grok"} <= slugs
        grok = next(seat for seat in body["seats"] if seat["slug"] == "grok")
        assert grok["athena"]["email"] == "grok@agents.local"
