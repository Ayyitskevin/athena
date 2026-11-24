"""The issue page offers undo for its own latest undoable event (U9).

COCKPIT-12: undo lived on the activity page only, reached by numeric target
id. The issue page now offers Undo on the newest undoable event of its own
trail (the verb has an inverse, the event is native and not yet reversed) when
the undo engine would accept it from this viewer right now: the viewer passes
the inverse command's write gate, and the effect is still in force. When it
would not, the page offers nothing; it never moves down to an older event. The form is the one the
activity page posts (``/aegis/activity/{id}/undo``, the same engine and rules);
it only adds where to come back to.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from support import ADMIN, add_user, bootstrap, events, login, make_app

from athena.core import db

_UNDO_FORM = re.compile(r'action="/aegis/activity/(\d+)/undo"')


def _offered(client: TestClient, issue_id: int) -> list[int]:
    page = client.get(f"/aegis/issues/{issue_id}")
    assert page.status_code == 200, page.text
    return [int(event_id) for event_id in _UNDO_FORM.findall(page.text)]


def _issue(client: TestClient, title: str = "undo me", **fields) -> dict:
    response = client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def _status(client: TestClient, issue_id: int, status: str) -> None:
    response = client.patch(
        f"/issues/{issue_id}", json={"status": status}, headers=ADMIN
    )
    assert response.status_code == 200, response.text


def _label(client: TestClient, issue_id: int, name: str = "bug") -> dict:
    label = client.post("/labels", json={"name": name}, headers=ADMIN).json()
    attached = client.post(
        f"/issues/{issue_id}/labels", json={"label_id": label["id"]}, headers=ADMIN
    )
    assert attached.status_code in (200, 201), attached.text
    return label


def test_the_issue_page_offers_undo_for_its_latest_undoable_event(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue = _issue(client)
        _label(client, issue["id"])
        _status(client, issue["id"], "in_progress")
        status_event = events(db_file, "changed_status")[0]["id"]
        login(client, "a@e.com")
        # One offer, for the newest undoable event: the status change.
        assert _offered(client, issue["id"]) == [status_event]
        page = client.get(f"/aegis/issues/{issue['id']}").text
        assert (
            f'<input type="hidden" name="return_to" value="/aegis/issues/{issue["id"]}">'
            in page
        )
        undone = client.post(
            f"/aegis/activity/{status_event}/undo",
            data={"return_to": f"/aegis/issues/{issue['id']}"},
            follow_redirects=False,
        )
        assert undone.status_code == 303, undone.text
        location = undone.headers["location"]
        assert location.startswith(f"/aegis/issues/{issue['id']}?")
        after = client.get(location).text
        restored = client.get(f"/issues/{issue['id']}", headers=ADMIN).json()

    assert restored["status"] == "open"
    assert f"Undid event #{status_event}" in after
    # The reversal is itself the newest undoable event (undoing it is a redo),
    # and the original row now reads "undone".
    reversal = events(db_file, "changed_status")[0]
    assert reversal["reverses_event_id"] == status_event
    assert [int(i) for i in _UNDO_FORM.findall(after)] == [reversal["id"]]
    assert "undone" in after


def test_no_offer_to_a_viewer_the_inverse_command_would_refuse(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        add_user(client, "m@e.com", name="Mo Member")
        add_user(client, "v@e.com", name="Vi Viewer", role="viewer")
        issue = _issue(client)
        _status(client, issue["id"], "in_progress")
        login(client, "a@e.com")
        assert len(_offered(client, issue["id"])) == 1  # positive control
        client.cookies.clear()
        # A member who can see the issue but is not its creator, assignee or
        # contributor: the status command would refuse them, so no offer.
        login(client, "m@e.com")
        member = _offered(client, issue["id"])
        client.cookies.clear()
        login(client, "v@e.com")
        viewer = _offered(client, issue["id"])

    assert member == []
    assert viewer == []


def test_an_effect_no_longer_in_force_is_not_offered(tmp_path):
    """The status change stopped being in force when the issue moved project
    (statuses are per project); the engine would refuse it, so the page offers
    nothing. It does not move down to the older label event: one Undo means
    the last change that can be reversed, and the label is still undone from
    the activity page."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        project = client.post(
            "/projects", json={"name": "Elsewhere", "key": "ELS"}, headers=ADMIN
        ).json()
        issue = _issue(client)
        _label(client, issue["id"])
        _status(client, issue["id"], "in_progress")
        moved = client.patch(
            f"/issues/{issue['id']}", json={"project_id": project["id"]}, headers=ADMIN
        )
        assert moved.status_code == 200, moved.text
        status_event = [
            e
            for e in events(db_file, "changed_status")
            if e["target_id"] == issue["id"]
        ][-1]["id"]
        labeled = events(db_file, "labeled")[0]["id"]
        login(client, "a@e.com")
        offered = _offered(client, issue["id"])
        refused = client.post(f"/activity/{status_event}/undo", headers=ADMIN)
        older = client.post(f"/activity/{labeled}/undo", headers=ADMIN)

    # The engine agrees: that status change cannot be undone now.
    assert refused.status_code == 409, refused.text
    assert offered == []
    # The older event is not offered here, and the engine still accepts it.
    assert older.status_code == 201, older.text


def test_a_vanished_label_is_not_offered(tmp_path):
    """The label inverse resolves the label by its unique name; once the label is
    gone the engine refuses (tests/test_undo.py), so the page offers nothing."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue = _issue(client)
        label = _label(client, issue["id"])
        login(client, "a@e.com")
        assert len(_offered(client, issue["id"])) == 1
        conn = db.connect(db_file)
        conn.execute("DELETE FROM labels WHERE id = ?", (label["id"],))
        conn.commit()
        conn.close()
        assert _offered(client, issue["id"]) == []


def test_the_return_target_is_only_an_issue_page(tmp_path):
    """The activity feed's own undo still returns to the feed, and a return
    target that is not an issue page is ignored rather than followed."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue = _issue(client)
        _status(client, issue["id"], "in_progress")
        _status(client, issue["id"], "done")
        newest, older = [e["id"] for e in events(db_file, "changed_status")][:2]
        login(client, "a@e.com")
        offsite = client.post(
            f"/aegis/activity/{newest}/undo",
            data={"return_to": "https://evil.example/aegis/issues/1"},
            follow_redirects=False,
        )
        relative = client.post(
            f"/aegis/activity/{newest}/undo",
            data={"return_to": "//evil.example/aegis/issues/1"},
            follow_redirects=False,
        )
        # A path that only STARTS like an issue page is not one.
        longer = client.post(
            f"/aegis/activity/{newest}/undo",
            data={"return_to": f"/aegis/issues/{issue['id']}/history"},
            follow_redirects=False,
        )
        feed = client.post(f"/aegis/activity/{older}/undo", follow_redirects=False)

    assert offsite.status_code == 303
    assert offsite.headers["location"].startswith("/aegis/activity?")
    assert relative.status_code == 303
    assert relative.headers["location"].startswith("/aegis/activity?")
    assert longer.status_code == 303
    assert longer.headers["location"].startswith("/aegis/activity?")
    assert feed.status_code == 303
    assert feed.headers["location"].startswith("/aegis/activity?")


def test_an_undo_that_needs_approval_is_not_offered_and_a_click_asks(tmp_path):
    """A member gated on ``issue.close`` reopens their own issue. Undoing the
    reopen would close it, which the gate turns into an approval request, so
    the page offers no Undo; a click anyway (the activity page's form) returns
    to the page saying approval was requested, not the API's 202 JSON. Once an
    admin approves, the page offers it, and the click undoes."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com", name="Mo Member")
        as_member = {"X-Athena-Actor": str(member["id"])}
        issue = client.post("/issues", json={"title": "gated"}, headers=as_member)
        issue_id = issue.json()["id"]
        closed = client.patch(
            f"/issues/{issue_id}", json={"status": "done"}, headers=as_member
        )
        assert closed.status_code == 200, closed.text
        gated = client.put(
            f"/approvals/policies/{member['id']}",
            json={"action_kind": "issue.close"},
            headers=ADMIN,
        )
        assert gated.status_code == 200, gated.text
        reopened = client.patch(
            f"/issues/{issue_id}", json={"status": "open"}, headers=as_member
        )
        assert reopened.status_code == 200, reopened.text
        reopen_event = events(db_file, "changed_status")[0]["id"]

        login(client, "m@e.com")
        before = _offered(client, issue_id)
        clicked = client.post(
            f"/aegis/activity/{reopen_event}/undo",
            data={"return_to": f"/aegis/issues/{issue_id}"},
            follow_redirects=False,
        )
        landed = client.get(clicked.headers["location"]).text
        pending = client.get("/approvals", headers=ADMIN).json()
        asked = [r for r in pending if r["target_id"] == issue_id]
        approved = client.post(
            f"/approvals/{asked[0]['id']}/decision",
            json={"decision": "approve"},
            headers=ADMIN,
        )
        assert approved.status_code == 200, approved.text
        after_approval = _offered(client, issue_id)
        undone = client.post(
            f"/aegis/activity/{reopen_event}/undo",
            data={"return_to": f"/aegis/issues/{issue_id}"},
            follow_redirects=False,
        )
        status = client.get(f"/issues/{issue_id}", headers=ADMIN).json()["status"]

    assert before == []
    assert clicked.status_code == 303, clicked.text
    assert clicked.headers["location"].startswith(f"/aegis/issues/{issue_id}?")
    assert "needs approval" in landed and "Nothing was undone yet" in landed
    assert [r["state"] for r in asked] == ["pending"]
    assert after_approval == [reopen_event]
    assert undone.status_code == 303 and "Undid+event" in undone.headers["location"]
    assert status == "done"
