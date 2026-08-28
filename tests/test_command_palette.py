"""Tests for the gated command palette (MWS-18).

The palette is a browser convenience over existing command-owned authorization.
These tests exercise the server-side projection and the thin transport adapters:
keyboard/focus markup, gated action rendering, stale identity refusals,
duplicate-submit idempotency, and audit-event emission.
"""

from __future__ import annotations

import re
import sqlite3

from fastapi.testclient import TestClient

from athena.core import approvals, db
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


def _create_user_and_login(
    client, email, name, password, role="member", actor_id=None
):
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
    assert '<kbd>' in response.text
    assert "/static/palette.js?v=" in response.text
    assert 'data-csrf=' in response.text


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
        assert "claim" in ids    # admin is a permitted claimant
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

        # Re-using the spent generation is refused (duplicate-submit guard at the command).
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
        generation = next(f["value"] for f in yield_action["fields"] if f["name"] == "generation")

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

        response = client.post(
            f"/aegis/palette/issues/{issue['id']}/claim",
            data={"etag": '"stale"'},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code in (412, 428), response.text


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
