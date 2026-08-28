"""Tests for the gated command palette (MWS-18).

The palette is a browser convenience over existing command-owned authorization.
These tests exercise the server-side projection and the thin transport adapters:
keyboard/focus markup, gated action rendering, stale identity refusals,
duplicate-submit idempotency, and audit-event emission.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from athena.aegis import lease_commands
from athena.core import approvals, approvals_api, db
from athena.main import create_app


def _create_user(client, email, name, password, role="member", actor_id=None):
    """Create a user through the API, optionally acting as an existing admin."""
    headers = {}
    if actor_id is not None:
        headers["X-Athena-Actor"] = str(actor_id)
    resp = client.post(
        "/users",
        json={"email": email, "name": name, "password": password, "role": role},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_user_and_login(client, email, name, password, role="member", actor_id=None):
    """Create a user and establish a browser session."""
    user = _create_user(client, email, name, password, role, actor_id=actor_id)
    _login(client, email, password)
    return user


def _login(client, email, password):
    """Establish a browser session for an existing user."""
    response = client.post("/login", data={"email": email, "password": password})
    assert response.status_code in (200, 302), response.text


def _csrf_from_page(client, path="/aegis"):
    """Extract the session CSRF token from the rendered base layout."""
    response = client.get(path)
    assert response.status_code == 200, response.text
    m = re.search(r'data-csrf="([^"]*)"', response.text)
    assert m is not None, "CSRF token not found in rendered page"
    return m.group(1)


def _create_assigned_issue(client, assignee_id):
    """Create a backlog issue assigned to the target user via the REST API."""
    issue = client.post(
        "/issues",
        json={"title": "Palette target issue"},
        headers={"X-Athena-Actor": str(assignee_id)},
    ).json()
    client.put(
        f"/issues/{issue['id']}/assignee",
        json={"assignee_id": assignee_id},
        headers={"X-Athena-Actor": str(assignee_id)},
    )
    return issue


def _activity_verbs(conn: sqlite3.Connection, target_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT verb FROM activity WHERE target_kind = 'issue' AND target_id = ? ORDER BY id",
        (target_id,),
    ).fetchall()
    return [r["verb"] for r in rows]


def test_palette_markup_present_for_signed_in_user(tmp_path):
    app = create_app(tmp_path / "palette.db")
    with TestClient(app) as client:
        _create_user_and_login(client, "a@example.com", "A", "secret123", role="member")
        response = client.get("/aegis")
    assert response.status_code == 200
    assert '<div id="palette"' in response.text
    assert 'class="palette-input"' in response.text
    assert 'role="dialog"' in response.text
    assert 'aria-modal="true"' in response.text
    assert "<kbd>" in response.text
    assert "/static/palette.js?v=" in response.text
    assert "data-csrf=" in response.text


def test_palette_markup_absent_when_signed_out(tmp_path):
    app = create_app(tmp_path / "palette.db")
    with TestClient(app) as client:
        response = client.get("/aegis")
    assert response.status_code == 200
    assert '<div id="palette"' not in response.text


def test_palette_actions_render_only_permitted_issue_actions(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        member = _create_user(
            client,
            "m@example.com",
            "M",
            "secret",
            role="member",
            actor_id=admin["id"],
        )
        _ = _create_user(
            client,
            "v@example.com",
            "V",
            "secret",
            role="viewer",
            actor_id=admin["id"],
        )
        issue = _create_assigned_issue(client, member["id"])

        # Admin is eligible to claim (admin claimant gate), so the action is offered.
        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        assert actions["issue"]["id"] == issue["id"]
        ids = {a["id"] for a in actions["actions"]}
        assert "inspect" in ids
        assert "capture" in ids  # admin can write
        assert "claim" in ids  # admin is a permitted claimant
        assert "yield" not in ids
        assert "complete" not in ids

        # The assignee sees claim (and eventually yield/complete after claiming).
        client.post("/logout")
        _login(client, "m@example.com", "secret")
        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        ids = {a["id"] for a in actions["actions"]}
        assert "claim" in ids
        assert "inspect" in ids
        assert "yield" not in ids
        assert "complete" not in ids

        # A viewer sees nothing that writes.
        client.post("/logout")
        _login(client, "v@example.com", "secret")
        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        ids = {a["id"] for a in actions["actions"]}
        assert "inspect" in ids
        assert "capture" not in ids
        assert "claim" not in ids
        assert "yield" not in ids
        assert "complete" not in ids


def test_palette_claim_and_complete_record_activity(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        member = _create_user_and_login(
            client, "m@example.com", "M", "secret", role="member", actor_id=admin["id"]
        )
        issue = _create_assigned_issue(client, member["id"])
        csrf = _csrf_from_page(client)

        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        claim_action = next(a for a in actions["actions"] if a["id"] == "claim")
        etag = next(f["value"] for f in claim_action["fields"] if f["name"] == "etag")

        claim = client.post(
            f"/aegis/palette/issues/{issue['id']}/claim",
            data={"etag": etag},
            headers={"X-CSRF-Token": csrf},
        )
        assert claim.status_code == 201, claim.text
        generation = claim.json()["generation"]

        conn = db.connect(db_file)
        verbs = _activity_verbs(conn, issue["id"])
        assert "claimed" in verbs

        # After claiming, the projection switches to yield/complete.
        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        ids = {a["id"] for a in actions["actions"]}
        assert "yield" in ids
        assert "complete" in ids
        assert "claim" not in ids

        # Completing with the same generation succeeds once and records the event.
        complete = client.post(
            f"/aegis/palette/issues/{issue['id']}/complete",
            data={"generation": generation},
            headers={"X-CSRF-Token": csrf},
        )
        assert complete.status_code == 200, complete.text
        assert complete.json()["released"] is True

        verbs = _activity_verbs(conn, issue["id"])
        assert "claim_completed" in verbs

        # Re-using a spent possession token is refused by the lease fence. The
        # browser's actual pending-submit suppression is executed in the Node test.
        repeat = client.post(
            f"/aegis/palette/issues/{issue['id']}/complete",
            data={"generation": generation},
            headers={"X-CSRF-Token": csrf},
        )
        assert repeat.status_code == 409, repeat.text
        assert "no active claim" in repeat.json()["detail"]
        conn.close()


def test_palette_yield_records_handoff_and_activity(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        member = _create_user_and_login(
            client, "m@example.com", "M", "secret", role="member", actor_id=admin["id"]
        )
        issue = _create_assigned_issue(client, member["id"])
        csrf = _csrf_from_page(client)

        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        claim_action = next(a for a in actions["actions"] if a["id"] == "claim")
        etag = next(f["value"] for f in claim_action["fields"] if f["name"] == "etag")
        client.post(
            f"/aegis/palette/issues/{issue['id']}/claim",
            data={"etag": etag},
            headers={"X-CSRF-Token": csrf},
        )

        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        yield_action = next(a for a in actions["actions"] if a["id"] == "yield")
        generation = next(
            f["value"] for f in yield_action["fields"] if f["name"] == "generation"
        )

        yield_resp = client.post(
            f"/aegis/palette/issues/{issue['id']}/yield",
            data={
                "generation": generation,
                "reason": "blocked",
                "attempted_work": "tried the thing",
                "blocking_question": "what now?",
                "resume_instructions": "start here",
                "note": "yield note",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert yield_resp.status_code == 200, yield_resp.text
        body = yield_resp.json()
        assert body["issue_id"] == issue["id"]
        assert "handoff_token" in body

        conn = db.connect(db_file)
        verbs = _activity_verbs(conn, issue["id"])
        assert "claim_yielded" in verbs
        conn.close()


def test_palette_capture_creates_issue_and_activity(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        _create_user_and_login(
            client, "m@example.com", "M", "secret", role="member", actor_id=admin["id"]
        )
        csrf = _csrf_from_page(client)

        response = client.post(
            "/aegis/palette/capture",
            data={"title": "Captured from palette"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 201, response.text
        issue = response.json()
        assert issue["ref"]
        assert issue["href"]

        conn = db.connect(db_file)
        row = conn.execute(
            "SELECT verb FROM activity WHERE target_kind = 'issue' AND target_id = ?",
            (issue["id"],),
        ).fetchone()
        assert row is not None
        assert row["verb"] == "created"
        conn.close()


def test_palette_unknown_or_clipped_ref_returns_visible_refusal(tmp_path):
    app = create_app(tmp_path / "palette.db")
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        _create_user_and_login(
            client, "m@example.com", "M", "secret", role="member", actor_id=admin["id"]
        )
        response = client.get("/aegis/palette/actions?issue_ref=NOPE-999999")
        assert response.status_code == 404
        assert "no such issue" in response.json()["detail"]

        # SQLite row identities are signed 64-bit values. An oversized numeric
        # id or project sequence is a clipped identity, not a server error and
        # never a guess at a nearby row.
        for clipped in (
            "9223372036854775808",
            "ATH-9223372036854775808",
            "9" * 5_000,
            "ATH-" + "9" * 5_000,
        ):
            response = client.get(
                "/aegis/palette/actions", params={"issue_ref": clipped}
            )
            assert response.status_code == 404, response.text
            assert response.json() == {"detail": "no such issue"}


def test_palette_claim_with_stale_etag_is_refused(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        member = _create_user_and_login(
            client, "m@example.com", "M", "secret", role="member", actor_id=admin["id"]
        )
        issue = _create_assigned_issue(client, member["id"])
        csrf = _csrf_from_page(client)

        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()
        claim_action = next(a for a in actions["actions"] if a["id"] == "claim")
        stale_etag = next(
            field["value"]
            for field in claim_action["fields"]
            if field["name"] == "etag"
        )
        changed = client.patch(
            f"/issues/{issue['id']}",
            json={"priority": "high"},
            headers={"X-Athena-Actor": str(member["id"])},
        )
        assert changed.status_code == 200, changed.text

        response = client.post(
            f"/aegis/palette/issues/{issue['id']}/claim",
            data={"etag": stale_etag},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 412, response.text
        assert response.json()["code"] == "precondition_failed"


def test_palette_collapses_private_issue_identity_for_reads_and_writes(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        outsider = _create_user(
            client,
            "outsider@example.com",
            "Outsider",
            "secret123",
            role="member",
            actor_id=admin["id"],
        )
        project_response = client.post(
            "/projects",
            json={"name": "Private", "key": "PRIV"},
            headers={"X-Athena-Actor": str(admin["id"])},
        )
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()
        visibility = client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers={"X-Athena-Actor": str(admin["id"])},
        )
        assert visibility.status_code == 200, visibility.text
        issue_response = client.post(
            "/issues",
            json={"title": "Private target", "project_id": project["id"]},
            headers={"X-Athena-Actor": str(admin["id"])},
        )
        assert issue_response.status_code == 201, issue_response.text
        issue = issue_response.json()

        client.post("/logout")
        _login(client, outsider["email"], "secret123")
        csrf = _csrf_from_page(client)
        conn = db.connect(db_file)
        before = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

        projection = client.get(
            "/aegis/palette/actions", params={"issue_ref": issue["key"]}
        )
        claim = client.post(
            f"/aegis/palette/issues/{issue['id']}/claim",
            data={"etag": '"does-not-matter"'},
            headers={"X-CSRF-Token": csrf},
        )

        after = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        conn.close()

    assert projection.status_code == 404, projection.text
    assert projection.json() == {"detail": "no such issue"}
    assert claim.status_code == 404, claim.text
    assert claim.json() == {"detail": "no such issue", "code": "not_found"}
    assert after == before


def test_palette_mutations_refuse_clipped_path_identities_before_sql(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        csrf = _csrf_from_page(client)
        conn = db.connect(db_file)
        before = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

        claim = client.post(
            "/aegis/palette/issues/9223372036854775808/claim",
            data={"etag": '"stale"'},
            headers={"X-CSRF-Token": csrf},
        )
        approval = client.post(
            "/aegis/palette/approvals/9223372036854775808/decision",
            data={"decision": "approve"},
            headers={"X-CSRF-Token": csrf},
        )

        after = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        conn.close()

    assert claim.status_code == 422, claim.text
    assert approval.status_code == 422, approval.text
    assert after == before


def test_palette_approve_records_decision_activity(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        member = _create_user(
            client,
            "m@example.com",
            "M",
            "secret",
            role="member",
            actor_id=admin["id"],
        )
        # Seed a pending approval request directly through the core owner.
        conn = db.connect(db_file)
        req = approvals.open_request(
            conn,
            actor_id=member["id"],
            action_kind=approvals.ACTION_ISSUE_CLOSE,
            target_kind="issue",
            target_id=1,
            run_id=None,
        )
        conn.close()

        csrf = _csrf_from_page(client)
        response = client.post(
            f"/aegis/palette/approvals/{req.id}/decision",
            data={"decision": "approve", "note": "ok to close"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "approved"
        assert body["decided_by"] == admin["id"]

        conn = db.connect(db_file)
        row = conn.execute(
            "SELECT verb FROM activity WHERE target_kind = 'issue' AND target_id = 1 AND verb = ?",
            (approvals.VERB_APPROVED,),
        ).fetchone()
        assert row is not None
        conn.close()


def test_palette_approve_forbidden_for_non_admin(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        _create_user(
            client,
            "m@example.com",
            "M",
            "secret",
            role="member",
            actor_id=admin["id"],
        )
        _login(client, "m@example.com", "secret")
        csrf = _csrf_from_page(client)
        response = client.post(
            "/aegis/palette/approvals/1/decision",
            data={"decision": "approve"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 403


def test_palette_javascript_keyboard_focus_and_single_flight_contract():
    """Execute the behavior, not source-string assertions masquerading as JS tests."""
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["node", "--test", str(root / "tests/js/test_palette.cjs")],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_palette_projection_uses_the_command_owned_claimant_gate(tmp_path, monkeypatch):
    app = create_app(tmp_path / "palette.db")
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        issue = _create_assigned_issue(client, admin["id"])
        calls = []

        def refuse_projection(conn, candidate, actor):
            calls.append((candidate["id"], actor["id"]))
            return False

        monkeypatch.setattr(lease_commands, "claimant_is_eligible", refuse_projection)
        actions = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}").json()

    assert calls == [(issue["id"], admin["id"])]
    assert "claim" not in {action["id"] for action in actions["actions"]}


def test_palette_approval_uses_the_shared_core_http_adapter(tmp_path, monkeypatch):
    app = create_app(tmp_path / "palette.db")
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        csrf = _csrf_from_page(client)
        calls = []

        def decide_for_actor(conn, *, actor, request_id, decision, note):
            calls.append((actor["id"], request_id, decision, note))
            return {"id": request_id, "state": "approved"}

        monkeypatch.setattr(approvals_api, "decide_for_actor", decide_for_actor)
        response = client.post(
            "/aegis/palette/approvals/7/decision",
            data={"decision": "approve", "note": "reviewed"},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"id": 7, "state": "approved"}
    assert calls == [(admin["id"], 7, "approve", "reviewed")]


def test_loading_palette_actions_is_a_read_only_projection(tmp_path):
    db_file = tmp_path / "palette.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        admin = _create_user_and_login(
            client, "admin@example.com", "Admin", "secret123", role="admin"
        )
        issue = _create_assigned_issue(client, admin["id"])
        conn = db.connect(db_file)
        before = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        response = client.get(f"/aegis/palette/actions?issue_ref={issue['id']}")
        after = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        conn.close()

    assert response.status_code == 200, response.text
    assert after == before
