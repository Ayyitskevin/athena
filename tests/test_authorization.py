"""Tests for issue WRITE authorization — Athena's per-issue access rule.

The contract (creator-or-assignee): an issue may be modified only by the user who
created it OR its current assignee. Everyone else authenticated is read-only and
gets a 403. An UNASSIGNED issue can be modified only by its creator until someone
is assigned, and because the check runs against the CURRENT assignee, an assignee
who reassigns the issue away immediately loses their own write access.

These are the load-bearing tests: they prove authorization is enforced on every
write path (edit, status, assign) at both the REST API and the web layer, not
merely advertised in a predicate. Reads and commenting stay open to all and are
verified to NOT 403.
"""
from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import db
from athena.main import create_app


def _seed_three_users(db_file):
    conn = db.connect(db_file)
    for email, name in (
        ("ann@e.com", "Ann"),    # user 1 — creator
        ("bob@e.com", "Bob"),    # user 2 — assignee
        ("cy@e.com", "Cy"),      # user 3 — bystander
    ):
        conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _make_issue(client, actor="1") -> int:
    r = client.post("/issues", json={"title": "t"}, headers={"X-Athena-Actor": actor})
    assert r.status_code == 201
    return r.json()["id"]


def _assign(client, issue_id, assignee_id, actor="1"):
    r = client.put(
        f"/issues/{issue_id}/assignee",
        json={"assignee_id": assignee_id},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 200, r.text
    return r


# --- unit: the predicate itself -------------------------------------------


def test_can_modify_creator_true():
    issue = {"created_by": 1, "assignee_id": None}
    assert issues.can_modify(issue, 1) is True


def test_can_modify_assignee_true():
    issue = {"created_by": 1, "assignee_id": 2}
    assert issues.can_modify(issue, 2) is True


def test_can_modify_other_false():
    issue = {"created_by": 1, "assignee_id": 2}
    assert issues.can_modify(issue, 3) is False


def test_can_modify_unassigned_only_creator():
    issue = {"created_by": 1, "assignee_id": None}
    assert issues.can_modify(issue, 2) is False


# --- REST API: edit (PATCH) -----------------------------------------------


def test_creator_can_edit(tmp_path):
    db_file = tmp_path / "a.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        r = client.patch(
            f"/issues/{issue_id}",
            json={"title": "by creator"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["title"] == "by creator"


def test_assignee_can_edit(tmp_path):
    # WHY: the assignee is the issue's current owner; once assigned they must be
    # able to drive it, not just the original reporter.
    db_file = tmp_path / "b.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        _assign(client, issue_id, 2, actor="1")
        r = client.patch(
            f"/issues/{issue_id}",
            json={"title": "by assignee"},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 200
        assert r.json()["title"] == "by assignee"


def test_bystander_cannot_edit(tmp_path):
    # WHY (load-bearing): a third authenticated user who is neither creator nor
    # assignee is forbidden, and the issue is left untouched.
    db_file = tmp_path / "c.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        _assign(client, issue_id, 2, actor="1")
        r = client.patch(
            f"/issues/{issue_id}",
            json={"title": "hijacked"},
            headers={"X-Athena-Actor": "3"},
        )
        assert r.status_code == 403
        current = client.get("/issues").json()[0]
        assert current["title"] == "t"  # unchanged


def test_edit_missing_issue_is_404(tmp_path):
    db_file = tmp_path / "d.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        r = client.patch(
            "/issues/999", json={"title": "x"}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 404


# --- REST API: status + assign share the same gate ------------------------


def test_bystander_cannot_change_status(tmp_path):
    db_file = tmp_path / "e.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        r = client.patch(
            f"/issues/{issue_id}",
            json={"status": "done"},
            headers={"X-Athena-Actor": "3"},
        )
        assert r.status_code == 403


def test_bystander_cannot_assign(tmp_path):
    # WHY: assignment is a write too — a bystander can't grab an issue for anyone.
    db_file = tmp_path / "f.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        r = client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 3},
            headers={"X-Athena-Actor": "3"},
        )
        assert r.status_code == 403


def test_unassigned_issue_only_creator_can_assign(tmp_path):
    # WHY: until someone is assigned, the creator is the sole owner — a third
    # user can't make the first assignment.
    db_file = tmp_path / "g.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        r = client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 2},
            headers={"X-Athena-Actor": "3"},
        )
        assert r.status_code == 403


def test_assignee_loses_access_after_reassigning_away(tmp_path):
    # WHY (load-bearing): the gate checks the CURRENT assignee, so an assignee who
    # hands the issue to someone else immediately drops to read-only. This is the
    # dynamic edge the predicate exists to get right.
    db_file = tmp_path / "h.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        _assign(client, issue_id, 2, actor="1")  # creator assigns Bob
        # Bob (now the assignee) reassigns the issue to Cy.
        _assign(client, issue_id, 3, actor="2")
        # Bob can no longer modify it — he's neither creator nor current assignee.
        r = client.patch(
            f"/issues/{issue_id}",
            json={"title": "bob again"},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 403


def test_reads_and_comments_stay_open_to_bystander(tmp_path):
    # WHY: authorization is for WRITES only. A bystander must still be able to read
    # the issue and leave a comment — those paths do not pass through the gate.
    db_file = tmp_path / "i.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue_id = _make_issue(client, actor="1")
        assert client.get(f"/issues/{issue_id}/comments").status_code == 200
        r = client.post(
            f"/issues/{issue_id}/comments",
            json={"body": "outside opinion"},
            headers={"X-Athena-Actor": "3"},
        )
        assert r.status_code == 201


# --- Web (browser session) ------------------------------------------------


def _login(client, email, name):
    # User creation is authenticated after the first (bootstrap) user; act as
    # user 1, who always exists once the first _login has run.
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_bystander_cannot_change_status_or_edit(tmp_path):
    # WHY: the web write paths must obey the same creator-or-assignee rule as the
    # API. Cy, logged in but unrelated to the issue, is forbidden from both.
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator
        _login(client, "bob@e.com", "Bob")  # user 2
        _login(client, "cy@e.com", "Cy")    # user 3 — bystander (current session)
        # Ann creates the issue via the API as actor 1.
        issue_id = _make_issue(client, actor="1")

        status = client.post(
            f"/aegis/issues/{issue_id}/status", data={"status": "done"}
        )
        assert status.status_code == 403
        edit = client.post(
            f"/aegis/issues/{issue_id}/edit", data={"title": "cy was here"}
        )
        assert edit.status_code == 403
        # The edit form itself is gated too.
        form = client.get(f"/aegis/issues/{issue_id}/edit")
        assert form.status_code == 403


def test_web_detail_hides_controls_for_bystander(tmp_path):
    # WHY: don't render actions the user can't perform. A bystander sees the note,
    # not the status/assignee forms; the creator sees the forms.
    db_file = tmp_path / "web2.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator
        issue_id = _make_issue(client, actor="1")
        # Creator's view: the status form is present.
        creator_view = client.get(f"/aegis/issues/{issue_id}").text
        assert 'action="/aegis/issues/%d/status"' % issue_id in creator_view

        _login(client, "cy@e.com", "Cy")  # user 3 — bystander session
        bystander_view = client.get(f"/aegis/issues/{issue_id}").text
        assert 'action="/aegis/issues/%d/status"' % issue_id not in bystander_view
        assert "delegated contributor" in bystander_view


def test_web_creator_can_change_status(tmp_path):
    db_file = tmp_path / "web3.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator (current session)
        issue_id = _make_issue(client, actor="1")
        r = client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "done"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert client.get("/issues").json()[0]["status"] == "done"
