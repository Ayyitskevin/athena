"""Drag-to-change-status on the Aegis board.

The drag gesture itself is browser JS (board-dnd.js) and not exercised here; what
these tests pin down is the contract the gesture rides on:

  * the board renders the hooks the script needs — a draggable card tagged with its
    issue id, a column tagged with its status, and the session CSRF token — but only
    makes cards draggable for a signed-in user;
  * the move endpoint applies a status change and records it on the activity trail,
    then re-renders the board so the card lands in its new column;
  * every move carries the card's canonical issue ETag, so a stale board cannot
    overwrite a newer status and both HTMX and native users see a conflict notice;
  * it is a write: logged-out is 401, a missing CSRF token is 403, and a move by
    someone who isn't the issue's creator/assignee (or to a status the issue's
    project doesn't have) is refused by re-rendering UNCHANGED — the card snaps back;
  * the board groups primary assignees into agent/human/unassigned swimlanes and
    composes search/status/project/sprint filters without widening visibility;
  * every move path preserves all active filters, so a move doesn't blow the
    board's current view away.
"""

import html
import re
from urllib.parse import parse_qs, urlparse

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


def _lane(body: str, key: str) -> str:
    """Return one top-level swimlane's markup (sections are never nested)."""
    start = body.index(f'data-lane="{key}"')
    return body[start : body.index("</section>", start)]


def _board_etag(body: str, issue_id: int) -> str:
    """Return the browser-decoded validator from one card's move form."""
    form = re.search(
        rf'<form class="board-move"[^>]+'
        rf'action="/aegis/boards/move/{issue_id}">(.*?)</form>',
        body,
        re.DOTALL,
    )
    assert form is not None
    match = re.search(r'name="if_match" value="([^"]+)"', form.group(1))
    assert match is not None
    return html.unescape(match.group(1))


# --- markup: the hooks the drag script needs --------------------------------


def test_board_renders_drag_hooks_for_signed_in_user(tmp_path):
    with TestClient(create_app(tmp_path / "m.db")) as client:
        _admin(client)
        iss = _issue(client, "draggable card")
        label = client.post(
            "/labels", json={"name": "guarded", "color": "#336699"}, headers=H1
        ).json()
        client.post(
            f"/issues/{iss['id']}/labels",
            json={"label_id": label["id"]},
            headers=H1,
        )
        csrf = _login(client)
        body = client.get("/aegis/boards").text
        assert f'data-issue-id="{iss["id"]}"' in body
        assert 'draggable="true"' in body
        assert 'data-status="open"' in body
        assert csrf in body  # the native move form carries the session CSRF
        assert "/static/board-dnd.js" in body
        assert (
            _board_etag(body, iss["id"])
            == client.get(f"/issues/{iss['id']}", headers=H1).headers["etag"]
        )


def test_board_cards_not_draggable_when_logged_out(tmp_path):
    # WHY: the board is an open read, but a logged-out visitor can't write — so the
    # cards must not advertise a drag affordance they can't use.
    with TestClient(create_app(tmp_path / "lo.db")) as client:
        _admin(client)
        _issue(client, "look only")
        body = client.get("/aegis/boards").text
        assert "look only" in body  # still readable
        assert 'draggable="true"' not in body


def test_board_filters_are_one_scoped_native_get_form(tmp_path):
    # WHY: global hx-include selectors also match every card's stale hidden fields.
    # Scoping to the filter form keeps HTMX deterministic and leaves a no-JS GET path.
    with TestClient(create_app(tmp_path / "filters-markup.db")) as client:
        _admin(client)
        _issue(client, "filterable")
        _login(client)
        body = client.get("/aegis/boards").text

        assert 'action="/aegis/boards"' in body
        assert 'class="header-actions board-filters"' in body
        assert 'id="board-request-coordinator"' in body
        assert 'hx-sync="this:queue all"' in body
        assert 'hx-target=".board"' in body
        assert 'hx-swap="outerHTML"' in body
        assert body.count('hx-include="closest form"') == 5
        assert body.count('hx-sync="this:queue all"') == 1
        # Card forms stay native for no-JS use; JS sends through the stable source.
        assert 'hx-post="/aegis/boards/move/' not in body
        assert 'hx-include="[name=' not in body
        for control in (
            "board-search",
            "board-status",
            "board-project",
            "board-sprint",
        ):
            assert f'for="{control}"' in body
            assert f'id="{control}"' in body

        script = client.get("/static/board-dnd.js").text
        assert "form.requestSubmit();" in script
        assert 'document.addEventListener("submit"' in script
        assert "source: coordinator" in script
        assert 'target: ".board"' not in script
        assert "window.htmx.ajax" in script


def test_board_columns_include_unoccupied_valid_destinations(tmp_path):
    # WHY: narrowing to cards that are all open must not erase the in-progress and
    # done destinations and turn drag-to-move into a dead-end.
    with TestClient(create_app(tmp_path / "destinations.db")) as client:
        _admin(client)
        _issue(client, "only-open-card")
        _login(client)
        body = client.get("/aegis/boards").text

        for key in ("agent", "human", "unassigned"):
            lane = _lane(body, key)
            for status_name in ("open", "in_progress", "done"):
                assert lane.count(f'data-status="{status_name}"') == 1


def test_board_swimlanes_use_primary_assignee_kind(tmp_path):
    # WHY: an unassigned issue has a NULL actor kind; it must not be mistaken for a
    # human (False). The API projection and board grouping must tell the same truth.
    with TestClient(create_app(tmp_path / "lanes.db")) as client:
        _admin(client)
        human = client.post(
            "/users",
            json={"email": "human@e.com", "name": "Human"},
            headers=H1,
        ).json()
        agent = client.post(
            "/users",
            json={"email": "agent@e.com", "name": "Agent", "is_agent": True},
            headers=H1,
        ).json()
        human_issue = _issue(client, "human-owned-card")
        agent_issue = _issue(client, "agent-owned-card")
        unassigned_issue = _issue(client, "unassigned-card", status="done")

        human_assigned = client.put(
            f"/issues/{human_issue['id']}/assignee",
            json={"assignee_id": human["id"]},
            headers=H1,
        ).json()
        agent_assigned = client.put(
            f"/issues/{agent_issue['id']}/assignee",
            json={"assignee_id": agent["id"]},
            headers=H1,
        ).json()
        assert human_assigned["assignee_is_agent"] is False
        assert agent_assigned["assignee_is_agent"] is True
        assert unassigned_issue["assignee_is_agent"] is None

        listed = {
            issue["id"]: issue["assignee_is_agent"]
            for issue in client.get("/issues", headers=H1).json()
        }
        assert listed == {
            human_issue["id"]: False,
            agent_issue["id"]: True,
            unassigned_issue["id"]: None,
        }
        assert (
            client.get(f"/issues/{agent_issue['id']}", headers=H1).json()[
                "assignee_is_agent"
            ]
            is True
        )

        body = client.get("/aegis/boards").text
        expected = {
            "agent": "agent-owned-card",
            "human": "human-owned-card",
            "unassigned": "unassigned-card",
        }
        for key, title in expected.items():
            lane = _lane(body, key)
            assert title in lane
            assert body.count(title) == 1
            # Every lane gets the same status targets, even when one is empty.
            assert lane.count('data-status="open"') == 1
            assert lane.count('data-status="done"') == 1
            for other_title in set(expected.values()) - {title}:
                assert other_title not in lane


def test_board_project_backlog_and_sprint_filters_compose(tmp_path):
    with TestClient(create_app(tmp_path / "placement-filters.db")) as client:
        _admin(client)
        first = client.post(
            "/projects", json={"name": "First", "key": "ONE"}, headers=H1
        ).json()
        second = client.post(
            "/projects", json={"name": "Second", "key": "TWO"}, headers=H1
        ).json()
        first_sprint = client.post(
            f"/projects/{first['id']}/sprints",
            json={"name": "Sprint A"},
            headers=H1,
        ).json()
        second_sprint = client.post(
            f"/projects/{second['id']}/sprints",
            json={"name": "Sprint B"},
            headers=H1,
        ).json()
        in_first_sprint = _issue(client, "first-sprint-card", project_id=first["id"])
        _issue(client, "first-unsprinted-card", project_id=first["id"])
        in_second_sprint = _issue(client, "second-sprint-card", project_id=second["id"])
        _issue(client, "global-backlog-card")
        for issue, sprint in (
            (in_first_sprint, first_sprint),
            (in_second_sprint, second_sprint),
        ):
            client.put(
                f"/issues/{issue['id']}/sprint",
                json={"sprint_id": sprint["id"]},
                headers=H1,
            )

        by_project = client.get("/aegis/boards", params={"project": first["id"]}).text
        assert "first-sprint-card" in by_project
        assert "first-unsprinted-card" in by_project
        assert "second-sprint-card" not in by_project
        assert "global-backlog-card" not in by_project
        assert f'value="{first["id"]}" selected' in by_project

        by_sprint = client.get(
            "/aegis/boards", params={"sprint": first_sprint["id"]}
        ).text
        assert "first-sprint-card" in by_sprint
        assert "first-unsprinted-card" not in by_sprint
        assert "second-sprint-card" not in by_sprint
        assert f'value="{first_sprint["id"]}" selected' in by_sprint
        assert "ONE · Sprint A" in by_sprint

        backlog_only = client.get("/aegis/boards", params={"project": "none"}).text
        assert "global-backlog-card" in backlog_only
        assert "first-sprint-card" not in backlog_only

        mismatch = client.get(
            "/aegis/boards",
            params={"project": second["id"], "sprint": first_sprint["id"]},
        ).text
        assert "No issues match these filters." in mismatch
        for title in (
            "first-sprint-card",
            "first-unsprinted-card",
            "second-sprint-card",
            "global-backlog-card",
        ):
            assert title not in mismatch


def test_board_filter_options_and_crafted_queries_respect_visibility(tmp_path):
    # WHY: project/sprint dropdowns and direct query strings are both read oracles.
    # A hidden container's names and cards must remain indistinguishable from absent.
    with TestClient(create_app(tmp_path / "visibility.db")) as client:
        _admin(client)
        public = client.post(
            "/projects", json={"name": "Public Project", "key": "PUB"}, headers=H1
        ).json()
        private = client.post(
            "/projects",
            json={"name": "Secret Project Marker", "key": "SEC"},
            headers=H1,
        ).json()
        public_sprint = client.post(
            f"/projects/{public['id']}/sprints",
            json={"name": "Public Sprint"},
            headers=H1,
        ).json()
        private_sprint = client.post(
            f"/projects/{private['id']}/sprints",
            json={"name": "Secret Sprint Marker"},
            headers=H1,
        ).json()
        public_issue = _issue(client, "public-card", project_id=public["id"])
        private_issue = _issue(client, "secret-card-marker", project_id=private["id"])
        for issue, sprint in (
            (public_issue, public_sprint),
            (private_issue, private_sprint),
        ):
            client.put(
                f"/issues/{issue['id']}/sprint",
                json={"sprint_id": sprint["id"]},
                headers=H1,
            )
        assert (
            client.put(
                f"/projects/{private['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )

        anonymous = client.get("/aegis/boards").text
        private_project_probe = client.get(
            "/aegis/boards", params={"project": private["id"]}
        ).text
        private_sprint_probe = client.get(
            "/aegis/boards", params={"sprint": private_sprint["id"]}
        ).text
        assert "public-card" in anonymous
        assert "Public Project" in anonymous
        assert "Public Sprint" in anonymous
        for secret in (
            "secret-card-marker",
            "Secret Project Marker",
            "Secret Sprint Marker",
        ):
            assert secret not in anonymous
            assert secret not in private_project_probe
            assert secret not in private_sprint_probe

        _login(client)
        creator_view = client.get("/aegis/boards").text
        assert "secret-card-marker" in creator_view
        assert "Secret Project Marker" in creator_view
        assert "Secret Sprint Marker" in creator_view


# --- the move endpoint ------------------------------------------------------


def test_move_changes_status_and_records(tmp_path):
    with TestClient(create_app(tmp_path / "mv.db")) as client:
        _admin(client)
        iss = _issue(client, "shipit")
        csrf = _login(client)
        before_tag = _board_etag(client.get("/aegis/boards").text, iss["id"])
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={
                "new_status": "in_progress",
                "if_match": before_tag,
                "csrf_token": csrf,
            },
            headers=HX,
        )
        assert r.status_code == 200
        # The card is re-rendered under its new column...
        assert 'data-status="in_progress"' in r.text and "shipit" in r.text
        # ...the issue really moved...
        assert (
            client.get(f"/issues/{iss['id']}", headers=H1).json()["status"]
            == "in_progress"
        )
        # ...and the transition is on the trail, same as every other status change.
        acts = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()
        assert any(a["verb"] == "changed_status" for a in acts)
        assert _board_etag(r.text, iss["id"]) != before_tag


def test_stale_htmx_move_fails_closed_and_refreshes_board(tmp_path):
    # WHY: a board left open in one tab must not overwrite a newer status from
    # another writer. HTMX follows the explicit conflict redirect to fresh state.
    with TestClient(create_app(tmp_path / "stale-hx.db")) as client:
        _admin(client)
        iss = _issue(client, "concurrent card")
        csrf = _login(client)
        active_filters = {"search": "concurrent"}
        stale_tag = _board_etag(
            client.get("/aegis/boards", params=active_filters).text, iss["id"]
        )

        newer = client.patch(
            f"/issues/{iss['id']}",
            json={"status": "done"},
            headers={**H1, "If-Match": stale_tag},
        )
        assert newer.status_code == 200
        events_after_newer_write = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()

        stale_move = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={
                "new_status": "in_progress",
                "if_match": stale_tag,
                "csrf_token": csrf,
                **active_filters,
            },
            headers=HX,
        )
        assert stale_move.status_code == 412
        assert stale_move.headers["etag"] == newer.headers["etag"]
        redirect = urlparse(stale_move.headers["hx-redirect"])
        assert redirect.path == "/aegis/boards"
        assert parse_qs(redirect.query, keep_blank_values=True) == {
            "search": ["concurrent"],
            "status": [""],
            "project": [""],
            "sprint": [""],
            "move_conflict": ["stale"],
        }
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "done"
        assert (
            client.get(
                f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
            ).json()
            == events_after_newer_write
        )

        refreshed = client.get(stale_move.headers["hx-redirect"])
        assert refreshed.status_code == 200
        assert 'role="alert"' in refreshed.text
        assert "Athena refreshed the board" in refreshed.text
        assert _board_etag(refreshed.text, iss["id"]) == newer.headers["etag"]


def test_move_without_validator_cannot_write(tmp_path):
    # WHY: omission must not silently restore the former last-write-wins path.
    with TestClient(create_app(tmp_path / "missing-etag.db")) as client:
        _admin(client)
        iss = _issue(client, "guarded card")
        csrf = _login(client)

        response = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={"new_status": "done", "csrf_token": csrf},
            headers=HX,
        )

        assert response.status_code == 412
        assert "move_conflict=stale" in response.headers["hx-redirect"]
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "open"


def test_move_requires_login(tmp_path):
    with TestClient(create_app(tmp_path / "li.db")) as client:
        _admin(client)
        iss = _issue(client, "x")
        # No session cookie at all → 401 (the UI never offers the drag here).
        assert (
            client.post(
                f"/aegis/boards/move/{iss['id']}",
                data={"new_status": "done"},
                headers=HX,
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
                f"/aegis/boards/move/{iss['id']}",
                data={"new_status": "done"},
                headers=HX,
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
            json={
                "email": "c@e.com",
                "name": "Carol",
                "password": "pw",
                "role": "member",
            },
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


def test_invalid_project_filter_fails_before_move(tmp_path):
    # WHY: filter validation must happen before the command. Otherwise a forged form
    # can successfully mutate the issue and only then fail while rendering a 400.
    with TestClient(create_app(tmp_path / "invalid-project.db")) as client:
        _admin(client)
        iss = _issue(client, "must-stay-open")
        csrf = _login(client)
        before_events = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()

        for malformed in ("not-an-id", "²", str(1 << 63)):
            assert (
                client.get("/aegis/boards", params={"project": malformed}).status_code
                == 400
            )
            moved = client.post(
                f"/aegis/boards/move/{iss['id']}",
                data={
                    "new_status": "done",
                    "project": malformed,
                    "csrf_token": csrf,
                },
                headers=HX,
            )
            assert moved.status_code == 400
            assert (
                client.get(f"/issues/{iss['id']}", headers=H1).json()["status"]
                == "open"
            )
            assert (
                client.get(
                    f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
                ).json()
                == before_events
            )


def test_invalid_sprint_filter_is_lenient_for_reads_but_strict_before_move(tmp_path):
    # WHY: read-only queries retain the issue-list compatibility behavior, but
    # malformed write context must never accompany a mutation.
    with TestClient(create_app(tmp_path / "invalid-sprint.db")) as client:
        _admin(client)
        iss = _issue(client, "must-stay-open")
        csrf = _login(client)
        before_events = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()

        for malformed in ("not-an-id", "²", str(1 << 63)):
            read = client.get("/aegis/boards", params={"sprint": malformed})
            assert read.status_code == 200
            assert "must-stay-open" in read.text

            moved = client.post(
                f"/aegis/boards/move/{iss['id']}",
                data={
                    "new_status": "done",
                    "sprint": malformed,
                    "csrf_token": csrf,
                },
                headers=HX,
            )
            assert moved.status_code == 400
            assert (
                client.get(f"/issues/{iss['id']}", headers=H1).json()["status"]
                == "open"
            )
            assert (
                client.get(
                    f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
                ).json()
                == before_events
            )


def test_move_preserves_active_filter(tmp_path):
    # WHY: a move from a filtered board must keep every filter — re-rendering a
    # wider board would yank the operator's view out from under them.
    with TestClient(create_app(tmp_path / "filt.db")) as client:
        _admin(client)
        project = client.post(
            "/projects", json={"name": "Scoped", "key": "SCO"}, headers=H1
        ).json()
        other_project = client.post(
            "/projects", json={"name": "Other", "key": "OTH"}, headers=H1
        ).json()
        sprint = client.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "Now"},
            headers=H1,
        ).json()
        keep = _issue(client, "keepme in sprint", project_id=project["id"])
        client.put(
            f"/issues/{keep['id']}/sprint",
            json={"sprint_id": sprint["id"]},
            headers=H1,
        )
        _issue(client, "keepme outside sprint", project_id=project["id"])
        _issue(client, "keepme other project", project_id=other_project["id"])
        _issue(client, "other search term", project_id=project["id"])
        csrf = _login(client)
        active_filters = {
            "search": "keepme",
            "project": str(project["id"]),
            "sprint": str(sprint["id"]),
        }
        board_tag = _board_etag(
            client.get("/aegis/boards", params=active_filters).text, keep["id"]
        )
        r = client.post(
            f"/aegis/boards/move/{keep['id']}",
            data={
                "new_status": "in_progress",
                "if_match": board_tag,
                "csrf_token": csrf,
                **active_filters,
            },
            headers=HX,
        )
        assert r.status_code == 200
        assert "keepme in sprint" in r.text
        assert "keepme outside sprint" not in r.text
        assert "keepme other project" not in r.text
        assert "other search term" not in r.text
        for name, value in (
            ("search", "keepme"),
            ("status", ""),
            ("project", str(project["id"])),
            ("sprint", str(sprint["id"])),
        ):
            assert f'name="{name}" value="{value}"' in r.text


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
        board_tag = _board_etag(client.get("/aegis/boards").text, iss["id"])
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={
                "new_status": "in_progress",
                "if_match": board_tag,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/aegis/boards")
        assert (
            client.get(f"/issues/{iss['id']}", headers=H1).json()["status"]
            == "in_progress"
        )
        acts = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()
        assert any(a["verb"] == "changed_status" for a in acts)


def test_stale_keyboard_move_redirects_to_fresh_conflict_notice(tmp_path):
    # WHY: the no-JS form keeps POST/redirect/GET semantics while making a stale
    # rejection visible and preserving the newer writer's status.
    with TestClient(create_app(tmp_path / "stale-native.db")) as client:
        _admin(client)
        iss = _issue(client, "native concurrent card")
        csrf = _login(client)
        stale_tag = _board_etag(client.get("/aegis/boards").text, iss["id"])
        newer = client.patch(
            f"/issues/{iss['id']}",
            json={"status": "done"},
            headers={**H1, "If-Match": stale_tag},
        )
        assert newer.status_code == 200
        events_after_newer_write = client.get(
            f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
        ).json()

        response = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={
                "new_status": "in_progress",
                "if_match": stale_tag,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        location = urlparse(response.headers["location"])
        assert location.path == "/aegis/boards"
        assert parse_qs(location.query, keep_blank_values=True)["move_conflict"] == [
            "stale"
        ]
        assert client.get(f"/issues/{iss['id']}", headers=H1).json()["status"] == "done"
        assert (
            client.get(
                f"/activity?target_kind=issue&target_id={iss['id']}", headers=H1
            ).json()
            == events_after_newer_write
        )

        refreshed = client.get(response.headers["location"])
        assert refreshed.status_code == 200
        assert "<title>Boards — Aegis</title>" in refreshed.text
        assert "data-board-conflict" in refreshed.text
        assert _board_etag(refreshed.text, iss["id"]) == newer.headers["etag"]


def test_keyboard_move_redirect_preserves_filter(tmp_path):
    with TestClient(create_app(tmp_path / "kpf.db")) as client:
        _admin(client)
        project = client.post(
            "/projects", json={"name": "Project", "key": "PRO"}, headers=H1
        ).json()
        sprint = client.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "Sprint"},
            headers=H1,
        ).json()
        iss = _issue(client, "card", project_id=project["id"])
        client.put(
            f"/issues/{iss['id']}/sprint",
            json={"sprint_id": sprint["id"]},
            headers=H1,
        )
        csrf = _login(client)
        active_filters = {
            "search": "card",
            "status": "open",
            "project": str(project["id"]),
            "sprint": str(sprint["id"]),
        }
        board_tag = _board_etag(
            client.get("/aegis/boards", params=active_filters).text, iss["id"]
        )
        r = client.post(
            f"/aegis/boards/move/{iss['id']}",
            data={
                "new_status": "done",
                "if_match": board_tag,
                "csrf_token": csrf,
                **active_filters,
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        location = urlparse(r.headers["location"])
        assert location.path == "/aegis/boards"
        assert parse_qs(location.query, keep_blank_values=True) == {
            "search": ["card"],
            "status": ["open"],
            "project": [str(project["id"])],
            "sprint": [str(sprint["id"])],
        }


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
        proj = client.post(
            "/projects", json={"name": "Web", "key": "WEB"}, headers=H1
        ).json()
        client.post(
            f"/projects/{proj['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        client.post(
            "/issues",
            json={"title": "in project", "project_id": proj["id"]},
            headers=H1,
        )
        body = client.get("/aegis/boards").text
        # the custom status is an option on the board (for the project card's menu)
        assert 'value="review"' in body
        assert csrf in body  # board still carries the session token for the move
