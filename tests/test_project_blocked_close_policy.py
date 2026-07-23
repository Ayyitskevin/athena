"""Project governance configuration for the agent blocked-close policy.

The policy is inert until a human creator/admin explicitly enables it.  These tests
pin the configuration boundary: forward-compatible schema defaults, one canonical
command behind REST/browser, command-owned human authorization, exact strong ETags,
and a flag + audit fact that cannot half-commit.
"""

from __future__ import annotations

import html as html_lib
import re
import shutil
import sqlite3

from fastapi.testclient import TestClient
import pytest

from athena.aegis import project_activity, project_commands, projects
from athena.core import activity, db, etag, tokens, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="project-policy.db"):
    path = tmp_path / name
    return create_app(path), path


def _bootstrap(client: TestClient, *, password: str | None = None) -> dict:
    payload = {"email": "admin@example.com", "name": "Admin"}
    if password is not None:
        payload["password"] = password
    response = client.post("/users", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _user(
    client: TestClient,
    email: str,
    *,
    role: str = "member",
    is_agent: bool = False,
    password: str | None = None,
) -> dict:
    payload: dict[str, object] = {
        "email": email,
        "name": email,
        "role": role,
        "is_agent": is_agent,
    }
    if password is not None:
        payload["password"] = password
    response = client.post("/users", json=payload, headers=H1)
    assert response.status_code == 201, response.text
    return response.json()


def _project(client: TestClient, *, actor_id: int = 1, name: str = "Ops") -> dict:
    response = client.post(
        "/projects",
        json={"name": name, "key": name[:3].upper()},
        headers={"X-Athena-Actor": str(actor_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _get_project(
    client: TestClient, project_id: int, *, actor_id: int = 1
) -> tuple[dict, str]:
    response = client.get(
        f"/projects/{project_id}",
        headers={"X-Athena-Actor": str(actor_id)},
    )
    assert response.status_code == 200, response.text
    return response.json(), response.headers["ETag"]


def _policy_events(db_file) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return activity.list_activity(
            conn, verb="project_blocked_close_policy_changed", limit=100
        )
    finally:
        conn.close()


def test_0060_preserves_existing_projects_disabled_and_checks_boolean(
    tmp_path, monkeypatch
):
    source = db.MIGRATIONS_DIR
    staged = tmp_path / "migrations"
    staged.mkdir()
    for migration in source.glob("*.sql"):
        if migration.name < "0060_project_blocked_close_policy.sql":
            shutil.copy2(migration, staged / migration.name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)

    conn = db.connect(tmp_path / "upgrade.db")
    assert db.migrate(conn)[-1].startswith("0059_")
    conn.execute("INSERT INTO users (id, email, name) VALUES (1, 'a@example.com', 'A')")
    legacy = projects.create_project(conn, name="Legacy", key="LEG", created_by=1)
    assert "block_agent_closes_when_blocked" not in legacy

    shutil.copy2(
        source / "0060_project_blocked_close_policy.sql",
        staged / "0060_project_blocked_close_policy.sql",
    )
    assert db.migrate(conn) == ["0060_project_blocked_close_policy.sql"]
    assert (
        projects.get_project(conn, legacy["id"])["block_agent_closes_when_blocked"]
        is False
    )
    fresh = projects.create_project(conn, name="Fresh", key="FRE", created_by=1)
    assert fresh["block_agent_closes_when_blocked"] is False
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE projects SET block_agent_closes_when_blocked = 2 WHERE id = ?",
            (legacy["id"],),
        )
    conn.rollback()
    conn.close()


def test_rest_requires_exact_etag_and_audits_one_real_change(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        project = _project(client)
        assert project["block_agent_closes_when_blocked"] is False
        public, original_tag = _get_project(client, project["id"])
        assert original_tag == etag.strong_etag("project-v1", public)

        missing = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": True},
            headers=H1,
        )
        assert missing.status_code == 428
        assert missing.json()["code"] == "precondition_required"
        assert missing.headers["ETag"] == original_tag
        assert "no-store" in missing.headers["Cache-Control"]

        wildcard = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": True},
            headers={**H1, "If-Match": "*"},
        )
        assert wildcard.status_code == 400
        assert wildcard.json()["code"] == "invalid_if_match"

        malformed_value = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": "true"},
            headers={**H1, "If-Match": original_tag},
        )
        assert malformed_value.status_code == 422

        assert (
            client.patch(
                f"/projects/{project['id']}",
                json={"description": "changed elsewhere"},
                headers=H1,
            ).status_code
            == 200
        )
        _, current_tag = _get_project(client, project["id"])
        stale = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": True},
            headers={**H1, "If-Match": original_tag},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "precondition_failed"
        assert stale.headers["ETag"] == current_tag

        changed = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": True},
            headers={
                **H1,
                "If-Match": current_tag,
                "X-Athena-Run": "policy-run",
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["block_agent_closes_when_blocked"] is True
        changed_tag = changed.headers["ETag"]
        assert changed_tag != current_tag

        noop = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": True},
            headers={**H1, "If-Match": changed_tag},
        )
        assert noop.status_code == 200
        assert noop.headers["ETag"] == changed_tag
        listed = client.get("/projects", headers=H1).json()
        assert listed[0]["block_agent_closes_when_blocked"] is True

    events = _policy_events(db_file)
    assert len(events) == 1
    assert events[0]["actor_id"] == 1
    assert events[0]["target_kind"] == "project"
    assert events[0]["target_id"] == project["id"]
    assert events[0]["run_id"] == "policy-run"
    assert events[0]["detail"] == "agent blocked-issue close policy enabled"


def test_command_owns_human_creator_or_admin_authorization_and_hides_private(
    tmp_path,
):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        creator = _user(client, "creator@example.com")
        bystander = _user(client, "bystander@example.com")
        agent_admin = _user(client, "agent@example.com", role="admin", is_agent=True)
        project = _project(client, actor_id=creator["id"])
        _, tag = _get_project(client, project["id"], actor_id=creator["id"])

        for actor in (bystander, agent_admin):
            refused = client.put(
                f"/projects/{project['id']}/policy",
                json={"block_agent_closes_when_blocked": True},
                headers={
                    "X-Athena-Actor": str(actor["id"]),
                    "If-Match": tag,
                },
            )
            assert refused.status_code == 403

        # A human admin may configure a project they did not create.
        allowed = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": True},
            headers={**H1, "If-Match": tag},
        )
        assert allowed.status_code == 200

        # Visibility is checked before If-Match parsing, so a hidden project cannot
        # be discovered by probing its policy endpoint with malformed input.
        private = client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers={"X-Athena-Actor": str(creator["id"])},
        )
        assert private.status_code == 200
        hidden = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": False},
            headers={
                "X-Athena-Actor": str(bystander["id"]),
                "If-Match": "not-an-etag",
            },
        )
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "no such project"}

    assert len(_policy_events(db_file)) == 1


def test_command_reloads_actor_and_rolls_back_flag_when_audit_fails(
    tmp_path, monkeypatch
):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        project = _project(client)
        _, tag = _get_project(client, project["id"])

    conn = db.connect(db_file)
    actor_snapshot = users.get_user(conn, 1)
    assert actor_snapshot is not None

    def assert_denied(candidate):
        with pytest.raises(project_commands.ProjectPolicyCommandError) as denied:
            project_commands.set_blocked_close_policy(
                conn,
                actor=candidate,
                project_id=project["id"],
                enabled=True,
                if_match=[tag],
            )
        assert denied.value.kind == "forbidden"

    users.set_agent(conn, 1, True)
    assert_denied(actor_snapshot)

    users.set_agent(conn, 1, False)
    users.set_role(conn, 1, users.VIEWER_ROLE)
    assert_denied(actor_snapshot)
    users.set_role(conn, 1, users.ADMIN_ROLE)

    users.set_paused(conn, 1, True)
    assert_denied(actor_snapshot)
    users.set_paused(conn, 1, False)

    scoped_actor = {**actor_snapshot, "_token_scopes": (tokens.READ_SCOPE,)}
    assert_denied(scoped_actor)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(
        project_activity,
        "record_project_blocked_close_policy_changed",
        fail_audit,
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        project_commands.set_blocked_close_policy(
            conn,
            actor=users.get_user(conn, 1),
            project_id=project["id"],
            enabled=True,
            if_match=[tag],
        )
    assert (
        projects.get_project(conn, project["id"])["block_agent_closes_when_blocked"]
        is False
    )
    conn.close()
    assert _policy_events(db_file) == []


def test_browser_governance_form_round_trips_etag_and_rejects_stale_save(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client, password="secret")
        project = _project(client)
        login = client.post(
            "/login",
            data={"email": "admin@example.com", "password": "secret"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")

        page = client.get(f"/aegis/projects/{project['id']}/access")
        assert page.status_code == 200
        assert "Agent close policy" in page.text
        match = re.search(r'name="if_match" value="([^"]+)"', page.text)
        assert match is not None
        reviewed_tag = html_lib.unescape(match.group(1))

        enabled = client.post(
            f"/aegis/projects/{project['id']}/policy",
            data={
                "block_agent_closes_when_blocked": "true",
                "if_match": reviewed_tag,
            },
            follow_redirects=False,
        )
        assert enabled.status_code == 303
        assert (
            "blocked issues are protected"
            in client.get(f"/aegis/projects/{project['id']}/access").text
        )

        stale = client.post(
            f"/aegis/projects/{project['id']}/policy",
            data={
                "block_agent_closes_when_blocked": "false",
                "if_match": reviewed_tag,
            },
            follow_redirects=False,
        )
        assert stale.status_code == 412
        assert "Reload and try again" in stale.text
        assert stale.headers["ETag"] != reviewed_tag
        assert (
            _get_project(client, project["id"])[0]["block_agent_closes_when_blocked"]
            is True
        )
