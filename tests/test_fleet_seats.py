"""Declared fleet seats are data, not source (U4, decision D27).

The roster and "Assign to a seat" used to know exactly the eight seats written
into ``core/fleet_roster.py``. A new instance therefore opened its Fleet page on
a "Roster drift" alert about seats it never had, every agent onboarded through
the cockpit was listed as drift, and nothing in the cockpit could declare a
seat. Now the seats live in ``fleet_seats``: an existing instance keeps its
eight by migration 0093, a new one starts with none, and an admin declares,
edits and removes seats from the cockpit and over REST, each on the trail.
"""

from __future__ import annotations

from contextlib import closing
import re

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    add_user,
    as_user,
    assert_registered_refusal,
    bearer,
    bootstrap,
    events,
    login,
    make_app,
    onboard_agent,
)

from athena.aegis import issues
from athena.core import db, fleet_roster, fleet_seat_commands, users
from athena.workflows import fleet_assign_commands

MIGRATION = "0093_fleet_seats.sql"
EIGHT = ["kevin", "claude", "codex", "kimi", "grok", "glm", "muse", "qwen"]
VERBS = ("declared_fleet_seat", "edited_fleet_seat", "removed_fleet_seat")


def _probe(unit: str, scope: str) -> dict:
    return {"load_state": "loaded", "active_state": "active", "sub_state": "running"}


@pytest.fixture(autouse=True)
def quiet_systemd(monkeypatch):
    monkeypatch.setattr(fleet_roster, "probe_systemd_unit", _probe)


def _seat_rows(conn) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT slug, name, email, unit, unit_scope, buzz_pubkey, kind "
            "FROM fleet_seats ORDER BY id"
        )
    ]


# --- the migration, both ways ---------------------------------------------------


def test_a_database_with_users_keeps_its_eight_seats(
    tmp_path, monkeypatch, migration_inventory_through
):
    packaged = db.MIGRATIONS_DIR
    migration_inventory_through("0092_coordination_budget.sql")
    assert not (db.MIGRATIONS_DIR / MIGRATION).exists()
    with closing(db.connect(tmp_path / "existing.db")) as conn:
        db.migrate(conn)
        users.create_user(
            conn, email="kevin@athena.local", name="Kevin", role="admin", commit=True
        )
        monkeypatch.setattr(db, "MIGRATIONS_DIR", packaged)
        assert MIGRATION in db.migrate(conn)
        rows = _seat_rows(conn)
        assert [row["slug"] for row in rows] == EIGHT
        by_slug = {row["slug"]: row for row in rows}
        # What DECLARED_SEATS carried, carried over exactly.
        assert by_slug["kevin"] == {
            "slug": "kevin",
            "name": "Kevin",
            "email": "kevin@athena.local",
            "unit": None,
            "unit_scope": "user",
            "buzz_pubkey": (
                "2bee90a820486a2b2e8ea97e8b058f4201cd6eac6ac15e98d9727bccd8f6f580"
            ),
            "kind": "operator",
        }
        assert by_slug["glm"]["email"] is None
        for slug in ("muse", "qwen"):
            assert by_slug[slug]["unit"] == f"buzz-seat-{slug}.service"
            assert by_slug[slug]["unit_scope"] == "system"
            assert by_slug[slug]["kind"] == "local_ollama"
        assert by_slug["claude"]["unit"] == "buzz-acp-claude.service"
        assert by_slug["claude"]["unit_scope"] == "user"
        assert all(len(row["buzz_pubkey"]) == 64 for row in rows)
        # The same seats answer the same emails as the source list did.
        assert fleet_roster.seat_slug_for_email(conn, "GROK@agents.local") == "grok"
        assert fleet_roster.seat_slug_for_email(conn, "stranger@e.com") is None
        assert set(fleet_roster.assignable_seat_slugs(conn)) == {
            "claude",
            "codex",
            "kimi",
            "grok",
            "muse",
            "qwen",
        }


def test_a_new_database_starts_with_no_seats(tmp_path):
    with closing(db.connect(tmp_path / "fresh.db")) as conn:
        assert MIGRATION in db.migrate(conn)
        assert _seat_rows(conn) == []
        # A user created after the migration does not bring the eight back.
        users.create_user(
            conn, email="kevin@athena.local", name="Kevin", role="admin", commit=True
        )
        db.migrate(conn)
        assert _seat_rows(conn) == []
        assert fleet_roster.seat_slug_for_email(conn, "grok@agents.local") is None


# --- the page on a new instance -------------------------------------------------


def test_a_new_instance_shows_no_drift_and_offers_to_declare(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client, email="admin@e.com", name="Admin")
        onboard_agent(client, email="sol@e.com", name="Sol")
        login(client, "admin@e.com")
        page = client.get("/admin/fleet")
        assert page.status_code == 200, page.text
        assert "Roster drift" not in page.text
        assert "That is drift" not in page.text
        assert 'data-tone="danger">drift' not in page.text
        # The cockpit agent is shown, with the way to give it a seat.
        assert "sol@e.com" in page.text
        assert 'action="/admin/fleet/seats"' in page.text
        body = client.get("/admin/fleet.json").json()
        assert body["seats"] == []
        assert body["drift"] == []
        assert body["summary"]["declared"] == 0
        assert body["summary"]["drift"] == 0
        assert [row["email"] for row in body["undeclared_athena_agents"]] == [
            "sol@e.com"
        ]


def test_with_seats_an_undeclared_agent_is_drift_and_its_row_offers_a_seat(
    tmp_path,
):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client, email="admin@e.com", name="Admin")
        onboard_agent(client, email="sol@e.com", name="Sol")
        onboard_agent(client, email="terra@e.com", name="Terra")
        made = client.post(
            "/fleet/seats",
            json={"slug": "sol", "name": "Sol", "email": "sol@e.com", "kind": "seat"},
            headers=ADMIN,
        )
        assert made.status_code == 201, made.text
        login(client, "admin@e.com")
        page = client.get("/admin/fleet")
        assert "Roster drift" in page.text
        assert "terra@e.com is not a declared seat" in page.text
        # The offer on Terra's row: a declare form carrying her address.
        assert re.search(
            r'<form method="post" action="/admin/fleet/seats"[^>]*>'
            r'(?:(?!</form>).)*value="terra@e.com"',
            page.text,
            re.S,
        ), page.text
        body = client.get("/admin/fleet.json").json()
        assert [row["slug"] for row in body["seats"]] == ["sol"]
        assert body["drift"] == [
            "athena agent terra@e.com is not a declared seat — "
            "declare it or offboard it"
        ]


# --- REST -------------------------------------------------------------------------


def test_an_admin_declares_edits_and_removes_a_seat_over_rest(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app, headers=ADMIN) as client:
        bootstrap(client)
        agent = onboard_agent(client, email="sol@e.com", name="Sol")
        sol = bearer(agent)
        assert client.get("/desk", headers=sol).json()["identity"]["seat_slug"] is None

        key = {"Idempotency-Key": "declare-sol-1"}
        body = {
            "slug": "Sol",
            "name": "Sol",
            "email": "SOL@e.com",
            "kind": "seat",
            "unit": "buzz-acp-sol.service",
            "buzz_pubkey": "ab" * 32,
        }
        made = client.post("/fleet/seats", json=body, headers=key)
        assert made.status_code == 201, made.text
        seat = made.json()
        assert seat["slug"] == "sol"  # the key is lower case
        assert seat["email"] == "sol@e.com"
        assert seat["unit_scope"] == "user"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", seat["created_at"])
        # The same key replays; it does not declare twice.
        again = client.post("/fleet/seats", json=body, headers=key)
        assert again.status_code == 201
        assert again.json() == seat
        assert len(events(db_file, "declared_fleet_seat")) == 1

        assert client.get("/desk", headers=sol).json()["identity"]["seat_slug"] == (
            "sol"
        )

        edited = client.patch(
            "/fleet/seats/sol",
            json={"name": "Sol Prime", "unit_scope": "system", "buzz_pubkey": None},
            headers={"Idempotency-Key": "edit-sol-1"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["name"] == "Sol Prime"
        assert edited.json()["unit_scope"] == "system"
        assert edited.json()["buzz_pubkey"] is None
        assert edited.json()["unit"] == "buzz-acp-sol.service"  # absent: kept

        listed = client.get("/fleet/seats").json()
        assert [row["slug"] for row in listed["items"]] == ["sol"]
        assert listed["has_more"] is False and listed["next_cursor"] is None

        removed = client.delete(
            "/fleet/seats/sol", headers={"Idempotency-Key": "remove-sol-1"}
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["slug"] == "sol"
        assert client.get("/fleet/seats").json()["items"] == []
        assert client.get("/desk", headers=sol).json()["identity"]["seat_slug"] is None
        gone = client.delete("/fleet/seats/sol")
        assert gone.status_code == 404
        assert_registered_refusal({**gone.json(), "status_code": 404})

    trail = events(db_file, *VERBS)
    assert [row["verb"] for row in reversed(trail)] == list(VERBS)
    assert all(row["actor_id"] == 1 for row in trail)
    assert all(row["target_kind"] == "fleet_seat" for row in trail)
    assert "sol" in trail[-1]["detail"] and "sol@e.com" in trail[-1]["detail"]
    assert "Sol Prime" in trail[1]["detail"]


def test_the_seat_list_pages_in_d2(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, headers=ADMIN) as client:
        bootstrap(client)
        for slug in ("one", "two", "three"):
            made = client.post(
                "/fleet/seats", json={"slug": slug, "name": slug, "kind": "seat"}
            )
            assert made.status_code == 201, made.text
        first = client.get("/fleet/seats?limit=2").json()
        assert [row["slug"] for row in first["items"]] == ["one", "two"]
        assert first["has_more"] is True
        second = client.get(
            "/fleet/seats", params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        assert [row["slug"] for row in second["items"]] == ["three"]
        assert second["has_more"] is False and second["next_cursor"] is None
        bad = client.get("/fleet/seats?cursor=nonsense")
        assert bad.status_code == 422
        assert bad.json()["code"] == "validation_error"


def test_only_an_admin_manages_or_reads_seats(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client, email="admin@e.com", name="Admin")
        add_user(client, "m@e.com", name="Member")
        agent = onboard_agent(client, scopes=("read", "issue:write"))
        made = client.post(
            "/fleet/seats", json={"slug": "sol", "name": "Sol"}, headers=ADMIN
        )
        assert made.status_code == 201, made.text
        for headers in (as_user(2), bearer(agent)):
            for method, path, body in (
                ("GET", "/fleet/seats", None),
                ("POST", "/fleet/seats", {"slug": "x", "name": "X"}),
                ("PATCH", "/fleet/seats/sol", {"name": "Y"}),
                ("DELETE", "/fleet/seats/sol", None),
            ):
                answer = client.request(method, path, json=body, headers=headers)
                assert answer.status_code == 403, (method, path, answer.text)
                assert answer.json()["code"] == "forbidden"
        # The cockpit refuses a member too, and changes nothing.
        login(client, "m@e.com")
        for path, data in (
            ("/admin/fleet/seats", {"slug": "x", "name": "X", "kind": "seat"}),
            ("/admin/fleet/seats/sol", {"name": "Y", "kind": "seat"}),
            ("/admin/fleet/seats/sol/remove", {}),
        ):
            assert client.post(path, data=data).status_code == 403, path
        rows = client.get("/fleet/seats", headers=ADMIN).json()["items"]
        assert [(row["slug"], row["name"]) for row in rows] == [("sol", "Sol")]
    assert len(events(db_file, *VERBS)) == 1


def test_the_command_itself_refuses_a_non_admin(tmp_path):
    # The command owns the rule, not only its transports.
    with closing(db.connect(tmp_path / "command.db")) as conn:
        db.migrate(conn)
        member = users.create_user(
            conn, email="m@e.com", name="Member", role="member", commit=True
        )
        with pytest.raises(fleet_seat_commands.FleetSeatCommandError) as refused:
            fleet_seat_commands.declare_seat(conn, actor=member, slug="x", name="X")
        assert refused.value.kind == "forbidden"
        assert _seat_rows(conn) == []


def test_a_taken_slug_or_email_is_refused_with_a_registered_code(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app, headers=ADMIN) as client:
        bootstrap(client)
        for body in (
            {"slug": "sol", "name": "Sol", "email": "sol@e.com"},
            {"slug": "terra", "name": "Terra", "email": "terra@e.com"},
        ):
            assert client.post("/fleet/seats", json=body).status_code == 201
        refused = [
            client.post("/fleet/seats", json={"slug": "SOL", "name": "Again"}),
            client.post(
                "/fleet/seats",
                json={"slug": "luna", "name": "Luna", "email": "Sol@E.com"},
            ),
            client.patch("/fleet/seats/terra", json={"email": "sol@e.com"}),
        ]
        for answer in refused:
            assert answer.status_code == 409, answer.text
            assert answer.json()["code"] == "conflict"
            assert_registered_refusal({**answer.json(), "status_code": 409})
        invalid = [
            client.post("/fleet/seats", json={"slug": "no spaces", "name": "X"}),
            client.post("/fleet/seats", json={"slug": "x", "name": "X", "kind": "pet"}),
            # A unit is handed to systemctl as an argument: never an option.
            client.post(
                "/fleet/seats", json={"slug": "x", "name": "X", "unit": "--all"}
            ),
            client.post(
                "/fleet/seats", json={"slug": "x", "name": "X", "buzz_pubkey": "zz"}
            ),
            client.post("/fleet/seats", json={"slug": "x", "name": "X", "extra": 1}),
        ]
        for answer in invalid:
            assert answer.status_code == 422, answer.text
            assert answer.json()["code"] == "validation_error"
    assert len(events(db_file, *VERBS)) == 2


# --- the cockpit ----------------------------------------------------------------


def test_an_admin_declares_edits_and_removes_a_seat_in_the_cockpit(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client, email="admin@e.com", name="Admin")
        onboard_agent(client, email="sol@e.com", name="Sol")
        login(client, "admin@e.com")
        page = client.get("/admin/fleet")
        assert "Declare a seat" in page.text

        declared = client.post(
            "/admin/fleet/seats",
            data={
                "slug": "sol",
                "name": "Sol",
                "email": "sol@e.com",
                "kind": "seat",
                "unit": "",
                "unit_scope": "user",
                "buzz_pubkey": "",
            },
            follow_redirects=False,
        )
        assert declared.status_code == 303, declared.text
        page = client.get(declared.headers["location"])
        assert "Declared seat sol." in page.text
        assert 'action="/admin/fleet/seats/sol"' in page.text
        remove_form = re.search(
            r'<form method="post" action="/admin/fleet/seats/sol/remove"[^>]*>',
            page.text,
        )
        assert remove_form and "data-confirm=" in remove_form.group(0)
        assert "Roster drift" not in page.text

        taken = client.post(
            "/admin/fleet/seats",
            data={"slug": "sol", "name": "Again", "kind": "seat"},
        )
        assert taken.status_code == 409
        assert "already declared" in taken.text

        edited = client.post(
            "/admin/fleet/seats/sol",
            data={
                "name": "Sol Prime",
                "email": "sol@e.com",
                "kind": "seat",
                "unit": "buzz-acp-sol.service",
                "unit_scope": "system",
                "buzz_pubkey": "",
            },
            follow_redirects=False,
        )
        assert edited.status_code == 303, edited.text
        page = client.get("/admin/fleet")
        assert "Sol Prime" in page.text
        assert "buzz-acp-sol.service" in page.text

        removed = client.post("/admin/fleet/seats/sol/remove", follow_redirects=False)
        assert removed.status_code == 303, removed.text
        assert client.get("/admin/fleet.json").json()["seats"] == []
        # Without the CSRF header the cockpit form is refused.
        del client.headers["X-CSRF-Token"]
        refused = client.post(
            "/admin/fleet/seats", data={"slug": "x", "name": "X", "kind": "seat"}
        )
        assert refused.status_code == 403
    assert [row["verb"] for row in reversed(events(db_file, *VERBS))] == list(VERBS)


# --- the readers follow the table -------------------------------------------------


def test_assign_to_a_seat_follows_the_table(tmp_path):
    with closing(db.connect(tmp_path / "assign.db")) as conn:
        db.migrate(conn)
        admin = users.create_user(
            conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE, commit=True
        )
        users.create_user(
            conn, email="grok@agents.local", name="Grok", is_agent=True, commit=True
        )
        issue = issues.create_issue(
            conn, title="slice", body="", created_by=admin["id"], commit=True
        )

        def assign():
            return fleet_assign_commands.assign_issue_to_seat(
                conn,
                actor=admin,
                issue_id=issue["id"],
                seat_slug="grok",
                radio=lambda **kwargs: {"status": "skipped"},
            )

        assert fleet_roster.athena_assignable_seats(conn) == []
        with pytest.raises(fleet_assign_commands.AssignError, match="unknown seat"):
            assign()
        fleet_seat_commands.declare_seat(
            conn,
            actor=admin,
            slug="grok",
            name="Grok",
            email="grok@agents.local",
            kind="seat",
        )
        assert [s["slug"] for s in fleet_roster.athena_assignable_seats(conn)] == [
            "grok"
        ]
        assert assign()["seat"] == "grok"
        fleet_seat_commands.remove_seat(conn, actor=admin, slug="grok")
        with pytest.raises(fleet_assign_commands.AssignError, match="unknown seat"):
            assign()


def test_work_context_and_office_seat_slug_follow_the_table(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, headers=ADMIN) as client:
        bootstrap(client)
        agent = onboard_agent(client, email="sol@e.com", name="Sol")
        sol = bearer(agent)
        issue = client.post("/issues", json={"title": "slice"}).json()

        def slugs():
            context = client.get(f"/issues/{issue['id']}/work-context", headers=sol)
            assert context.status_code == 200, context.text
            office_packet = client.get("/office", headers=sol).json()
            return context.json()["seat_slug"], office_packet["seat_slug"]

        assert slugs() == (None, None)
        made = client.post(
            "/fleet/seats",
            json={"slug": "sol", "name": "Sol", "email": "sol@e.com"},
        )
        assert made.status_code == 201, made.text
        assert slugs() == ("sol", "sol")
