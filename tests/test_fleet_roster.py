"""Declared fleet roster: systemd verdict + Athena rows, never 'alive'."""

from fastapi.testclient import TestClient

from athena.core import db, fleet_roster, users
from athena.main import create_app


def _probe(unit: str, scope: str) -> dict:
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


def test_seat_slug_for_declared_email():
    assert fleet_roster.seat_slug_for_email("grok@agents.local") == "grok"
    assert fleet_roster.seat_slug_for_email("GROK@agents.local") == "grok"
    assert fleet_roster.seat_slug_for_email("stranger@e.com") is None
    assert fleet_roster.seat_slug_for_email(None) is None


def test_local_workers_declared_with_system_units():
    # The 2026-08-21 drift repair: muse/qwen are onboarded (email + pubkey) and
    # their hardened SYSTEM units are declared as such, so the probe stops
    # reporting them "missing" against the wrong systemd manager. The retired
    # nemotron seat is gone — its leftover account belongs in undeclared drift.
    by_slug = {str(s["slug"]): s for s in fleet_roster.DECLARED_SEATS}
    assert "nemotron" not in by_slug
    for slug in ("muse", "qwen"):
        seat = by_slug[slug]
        assert seat["email"] == f"{slug}@agents.local"
        assert seat["unit"] == f"buzz-seat-{slug}.service"
        assert seat["unit_scope"] == "system"
        assert seat["buzz_pubkey"], f"{slug} must carry its Buzz pubkey"
    # Cloud seats stay user-scope by omission.
    assert "unit_scope" not in by_slug["claude"]
    assert set(fleet_roster.assignable_seat_slugs()) >= {"muse", "qwen"}


def test_probe_scope_routed_from_declaration(tmp_path):
    conn = db.connect(tmp_path / "scope.db")
    db.migrate(conn)
    seen: list[tuple[str, str]] = []

    def probe(unit: str, scope: str) -> dict:
        seen.append((unit, scope))
        return {"load_state": "loaded", "active_state": "active", "sub_state": "x"}

    declared = (
        {
            "slug": "muse",
            "name": "Muse",
            "email": None,
            "unit": "buzz-seat-muse.service",
            "unit_scope": "system",
            "buzz_pubkey": "cc" * 32,
            "kind": "local_ollama",
        },
        {
            "slug": "claude",
            "name": "Claude",
            "email": None,
            "unit": "buzz-acp-claude.service",
            "buzz_pubkey": "aa" * 32,
            "kind": "seat",
        },
    )
    roster = fleet_roster.build_roster(conn, probe=probe, declared=declared)
    assert ("buzz-seat-muse.service", "system") in seen
    assert ("buzz-acp-claude.service", "user") in seen
    by_slug = {seat["slug"]: seat for seat in roster["seats"]}
    assert by_slug["muse"]["unit_scope"] == "system"
    assert by_slug["claude"]["unit_scope"] == "user"
    conn.close()


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
