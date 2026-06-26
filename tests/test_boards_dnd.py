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
