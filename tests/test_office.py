"""The Office: one chair, not a queue."""

from fastapi.testclient import TestClient

from athena.aegis import issue_commands, issues, lease_commands, office
from athena.core import db, users
from athena.main import create_app


def test_checkout_hint_is_a_branch_name_not_a_remote():
    assert (
        office.checkout_hint(issue_key="MWS-1", issue_id=1, seat_slug="grok")
        == "athena/mws-1-grok"
    )
    assert (
        office.checkout_hint(issue_key=None, issue_id=4, seat_slug="kimi")
        == "athena/issue-4-kimi"
    )


def test_standing_office_points_at_next_delegation(tmp_path):
    conn = db.connect(tmp_path / "office-stand.db")
    db.migrate(conn)
    admin = users.create_user(
        conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE
    )
    grok = users.create_user(
        conn, email="grok@agents.local", name="Grok", is_agent=True
    )
    issue = issues.create_issue(conn, title="sit", body="", created_by=admin["id"])
    packet = office.build_office(
        conn,
        actor=grok,
        inbox_items=[
            {
                "issue": {"id": issue["id"], "key": issue.get("key"), "title": "sit"},
                "issue_etag": '"tag"',
            }
        ],
    )
    assert packet["schema"] == office.SCHEMA
    assert packet["seat_slug"] == "grok"
    assert packet["seated"] is False
    assert packet["chair"] is None
    assert packet["next_to_sit"]["issue_id"] == issue["id"]
    conn.close()


def test_one_active_lease_is_the_chair(tmp_path):
    conn = db.connect(tmp_path / "office-sit.db")
    db.migrate(conn)
    admin = users.create_user(
        conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE
    )
    grok = users.create_user(
        conn, email="grok@agents.local", name="Grok", is_agent=True
    )
    issue = issues.create_issue(conn, title="chair", body="", created_by=admin["id"])
    issue_commands.add_contributor(
        conn,
        actor=admin,
        issue_id=issue["id"],
        user_id=grok["id"],
        require_agent=True,
    )
    from athena.aegis import issue_etags

    tag = issue_etags.current_etag(conn, issues.get_issue(conn, issue["id"]))
    lease_commands.claim_issue(
        conn,
        actor=grok,
        issue_id=issue["id"],
        if_match=[tag],
        paths=["src/athena/aegis/office.py"],
    )
    packet = office.build_office(conn, actor=grok)
    assert packet["seated"] is True
    assert packet["chair"]["issue_id"] == issue["id"]
    assert packet["chair"]["declared_paths"] == ["src/athena/aegis/office.py"]
    assert packet["chair"]["checkout_hint"].startswith("athena/")
    occ = office.build_occupancy(conn)
    assert len(occ) == 1
    assert occ[0]["seat_slug"] == "grok"
    conn.close()


def test_office_http_and_desk_include_packet(tmp_path):
    app = create_app(tmp_path / "office-http.db")
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "admin@e.com", "name": "Admin", "password": "secret"},
        )
        onboarded = client.post(
            "/users/onboard_agent",
            json={
                "name": "Grok",
                "email": "grok@agents.local",
                "scopes": ["read", "issue:write"],
            },
            headers={"X-Athena-Actor": "1"},
        ).json()
        headers = {"Authorization": f"Bearer {onboarded['token']['token']}"}
        cubicle = client.get("/office", headers=headers)
        assert cubicle.status_code == 200, cubicle.text
        body = cubicle.json()
        assert body["schema"] == office.SCHEMA
        assert body["seat_slug"] == "grok"
        assert body["seated"] is False
        desk = client.get("/desk", headers=headers).json()
        assert desk["office"]["seat_slug"] == "grok"
        assert desk["office"]["protocol"]["claim_one_issue"] is True
