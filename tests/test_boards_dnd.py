"""Drag-to-change-status on the Aegis board.

The drag gesture itself is browser JS (board-dnd.js) and not exercised here; what
these tests pin down is the contract the gesture rides on:

  * the board renders the hooks the script needs — a draggable card tagged with its
    issue id, a column tagged with its status, and the session CSRF token — but only
    makes cards draggable for a signed-in user;
  * the move endpoint applies a status change and records it on the activity trail,
    then re-renders the board so the card lands in its new column;
  * it is a write: logged-out is 401, a missing CSRF token is 403, and a move by
    someone who isn't the issue's creator/assignee (or to a status the issue's
    project doesn't have) is refused by re-rendering UNCHANGED — the card snaps back;
  * the re-render keeps the active search/status filter, so a move doesn't blow the
    board's current view away.
"""
from athena.core import db
from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}
HX = {"HX-Request": "true"}


def _admin(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})


def _bob(client):
    # A second real login (has a password) who is neither creator nor assignee.
    client.post(
        "/users", json={"email": "b@e.com", "name": "Bob", "password": "pw"}, headers=H1
    )


def _login(client, email="a@e.com"):
    client.post("/login", data={"email": email, "password": "pw"})
    return client.cookies.get("athena_csrf")


def _issue(client, title, **kw):
    return client.post("/issues", json={"title": title, **kw}, headers=H1).json()


# --- markup: the hooks the drag script needs --------------------------------


def test_board_renders_drag_hooks_for_signed_in_user(tmp_path):
    with TestClient(create_app(tmp_path / "m.db")) as client:
        _admin(client)
        iss = _issue(client, "draggable card")
        csrf = _login(client)
        body = client.get("/aegis/boards").text
        assert f'data-issue-id="{iss["id"]}"' in body
        assert 'draggable="true"' in body
        assert 'data-status="open"' in body
        assert csrf in body  # the board carries the session CSRF for the POST
        assert "/static/board-dnd.js" in body


def test_board_cards_not_draggable_when_logged_out(tmp_path):
    # WHY: the board is an open read, but a logged-out visitor can't write — so the
    # cards must not advertise a drag affordance they can't use.
    with TestClient(create_app(tmp_path / "lo.db")) as client:
        _admin(client)
        _issue(client, "look only")
        body = client.get("/aegis/boards").text
        assert "look only" in body  # still readable
        assert 'draggable="true"' not in body


# --- the move endpoint ------------------------------------------------------


def test_move_changes_status_and_records(tmp_path):
    with TestClient(create_app(tmp_path / "mv.db")) as client:
        _admin(client)
        iss = _issue(client, "shipit")
        csrf = _login(client)
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "in_progress", "csrf_token": csrf},
            headers=HX,
        )
        assert r.status_code == 200
        # The card is re-rendered under its new column...
        assert 'data-status="in_progress"' in r.text and "shipit" in r.text
        # ...the issue really moved...
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "in_progress"
        # ...and the transition is on the trail, same as every other status change.
        acts = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()
        assert any(a["verb"] == "changed_status" for a in acts)


def test_move_requires_login(tmp_path):
    with TestClient(create_app(tmp_path / "li.db")) as client:
        _admin(client)
        iss = _issue(client, "x")
        # No session cookie at all → 401 (the UI never offers the drag here).
        assert (
            client.post(
                f"/aegis/boards/move/{iss['id']}", data={"new_status": "done"}, headers=HX
            ).status_code
            == 401
        )


def test_move_requires_csrf(tmp_path):
    with TestClient(create_app(tmp_path / "csrf.db")) as client:
        _admin(client)
        iss = _issue(client, "x")
        _login(client)
        # Logged in but no CSRF token → 403, the browser-write guard.
        assert (
            client.post(
                f"/aegis/boards/move/{iss['id']}", data={"new_status": "done"}, headers=HX
            ).status_code
            == 403
        )


def test_move_write_gate_snaps_back(tmp_path):
    # WHY: only the issue's creator/assignee may move it. A move by anyone else must
    # not apply — the board re-renders unchanged (200), so the card snaps back rather
    # than the page being replaced by an error.
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        _admin(client)
        _bob(client)
        iss = _issue(client, "alice owns this")  # created by user 1 (Alice)
        csrf = _login(client, email="b@e.com")  # act as Bob
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "done", "csrf_token": csrf},
            headers=HX,
        )
        assert r.status_code == 200
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "open"


def test_move_by_a_demoted_viewer_snaps_back(tmp_path):
    # WHY: the board is a primary write surface and must enforce the SAME read-only-role
    # gate as the detail page (_authorize_issue_write) and REST PATCH (issue_write_actor).
    # A user who created an issue as a member but was later demoted to the viewer role
    # must not be able to move their own card — creator/assignee is necessary but not
    # sufficient; write role is also required.
    db_file = tmp_path / "viewer.db"
    with TestClient(create_app(db_file)) as client:
        _admin(client)  # user 1, admin
        client.post(
            "/users",
            json={"email": "c@e.com", "name": "Carol", "password": "pw", "role": "member"},
            headers=H1,
        )  # user 2, member
        iss = client.post(
            "/issues", json={"title": "carol's"}, headers={"X-Athena-Actor": "2"}
        ).json()  # Carol is the creator
        # Admin demotes Carol to the read-only viewer role; she stays the creator.
        conn = db.connect(db_file)
        conn.execute("UPDATE users SET role = 'viewer' WHERE email = 'c@e.com'")
        conn.commit()

        csrf = _login(client, email="c@e.com")
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "done", "csrf_token": csrf},
            headers=HX,
        )
        assert r.status_code == 200
        # Snap-back: a viewer can't move even their own card.
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "open"


def test_move_invalid_status_snaps_back(tmp_path):
    with TestClient(create_app(tmp_path / "inv.db")) as client:
        _admin(client)
        iss = _issue(client, "x")
        csrf = _login(client)
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "not_a_status", "csrf_token": csrf},
            headers=HX,
        )
        assert r.status_code == 200
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "open"


def test_move_preserves_active_filter(tmp_path):
    # WHY: a move from a filtered board must keep the filter — re-rendering the whole
    # unfiltered board would yank the view out from under the user.
    with TestClient(create_app(tmp_path / "filt.db")) as client:
        _admin(client)
        keep = _issue(client, "keepme alpha")
        _issue(client, "other beta")
        csrf = _login(client)
        r = client.post(
            f"/aegis/boards/move/{keep['id']}",
            data={"new_status": "in_progress", "csrf_token": csrf, "search": "keepme"},
            headers=HX,
        )
        assert r.status_code == 200
        assert "keepme alpha" in r.text
        assert "other beta" not in r.text  # the search filter survived the move


# --- keyboard / no-JS move (the accessible twin of the drag gesture) ---------


def test_board_renders_keyboard_move_form(tmp_path):
    # WHY: drag is mouse-only. A signed-in user must also get a real form — a status
    # select plus a Move button — so the board is operable from the keyboard and
    # without JS. Logged-out users get neither.
    with TestClient(create_app(tmp_path / "kf.db")) as client:
        _admin(client)
        _issue(client, "kbd card")
        csrf = _login(client)
        body = client.get("/aegis/boards").text
        assert 'class="board-move"' in body
        assert 'name="new_status"' in body
        assert 'action="/aegis/boards/move/' in body  # native POST fallback (no JS)
        # the select offers this card's project statuses (backlog → defaults)
        assert '<option value="open" selected>' in body
        assert 'value="in_progress"' in body and 'value="done"' in body

        # logged out: no move control at all
        client.post("/logout", data={}, headers={"X-CSRF-Token": csrf})
        out = client.get("/aegis/boards").text
        assert 'class="board-move"' not in out


def test_keyboard_move_via_form_redirects_and_applies(tmp_path):
    # WHY: the no-JS path posts a plain form (no HX header). It must apply the move,
    # record it, and 303 back to the board (so a refresh re-reads, not re-posts).
    with TestClient(create_app(tmp_path / "km.db")) as client:
        _admin(client)
        iss = _issue(client, "shipit")
        csrf = _login(client)
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "in_progress", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/aegis/boards")
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "in_progress"
        acts = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()
        assert any(a["verb"] == "changed_status" for a in acts)


def test_keyboard_move_redirect_preserves_filter(tmp_path):
    with TestClient(create_app(tmp_path / "kpf.db")) as client:
        _admin(client)
        iss = _issue(client, "card")
        csrf = _login(client)
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "done", "csrf_token": csrf, "search": "card", "status": "open"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "search=card" in r.headers["location"]


def test_keyboard_move_write_gate_snaps_back(tmp_path):
    # WHY: same write gate as the drag path — a non-owner's form post must not apply.
    # The no-JS path still 303s (back to an unchanged board), it doesn't error-page.
    with TestClient(create_app(tmp_path / "kg.db")) as client:
        _admin(client)
        _bob(client)
        iss = _issue(client, "alice owns this")
        csrf = _login(client, email="b@e.com")  # act as Bob
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "done", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "open"


def test_card_move_options_are_the_cards_project_statuses(tmp_path):
    # WHY: each card's menu should be ITS project's statuses, so a keyboard user is
    # only offered valid targets. A project with a custom status shows it; a backlog
    # card shows the default set.
    with TestClient(create_app(tmp_path / "ko.db")) as client:
        _admin(client)
        csrf = _login(client)
        proj = client.post("/projects", json={"name": "Web", "key": "WEB"}, headers=H1).json()
        client.post(
            f"/projects/{proj['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        client.post("/issues", json={"title": "in project", "project_id": proj["id"]}, headers=H1)
        body = client.get("/aegis/boards").text
        # the custom status is an option on the board (for the project card's menu)
        assert 'value="review"' in body
        assert csrf in body  # board still carries the session token for the move
