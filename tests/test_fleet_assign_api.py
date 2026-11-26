"""Assigning an issue to a declared fleet seat over REST and MCP (decision 8, T1).

The cockpit could hand a slice to a seat by its roster slug and radio it over
Buzz (Admin > Fleet, a chair on the floor); REST had only the two writes
underneath, so a conductor agent had to know the account id and could not wake
the seat. ``POST /issues/{id}/assign-seat`` is the same command,
``fleet_assign_commands.assign_issue_to_seat``, over REST, and the conductor
profile's ``assign_issue_to_seat`` tool is that route over MCP.

Who may: a write role holding the ``issue:write`` scope (``admin`` includes it),
then the command's own issue gate (creator, assignee, delegated contributor or
admin), which is what the floor's chair form lets a writer do. The C10 review
found an admin-scope token answered 401 or 404 on the cockpit path; here it is
answered. An issue the caller cannot see reads exactly as a missing one (404),
and a seat whose agent cannot see the issue's project is refused the way an
unknown account is. The Buzz radio is injected and never reaches a network.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    add_user,
    as_user,
    bootstrap,
    bearer,
    declare_seat,
    events,
    make_app,
    mcp_server_for,
    mint_token,
    onboard_agent,
)

from athena.core import buzz_radio, db

_EVENT_ID = "62170b3ec5be2b49d2add83bf3936148923fc477734fbb953e4e2739a31835ef"
_PERMALINK = f"buzz://message?id={_EVENT_ID}&channel=command-deck"


@pytest.fixture
def pings(monkeypatch):
    """Every radio call the assign makes, answered as a sent ping. The command
    reads ``buzz_radio.send_assignment`` when it runs, so nothing leaves."""
    calls: list[dict] = []

    def radio(**kwargs):
        calls.append(kwargs)
        return {
            "status": "sent",
            "detail": "posted to command-deck",
            "channel": "command-deck",
            "event_id": _EVENT_ID,
            "permalink": _PERMALINK,
        }

    monkeypatch.setattr(buzz_radio, "send_assignment", radio)
    return calls


def _world(client: TestClient) -> dict:
    """An admin (1), the seat Grok (an agent account with a declared seat), and
    a conductor agent with an issue:write token."""
    bootstrap(client, email="admin@e.com", name="Admin")
    grok = onboard_agent(client, email="grok@agents.local", name="Grok")
    declare_seat(client, "grok", email="grok@agents.local", buzz_pubkey="a" * 64)
    conductor = onboard_agent(client, email="cond@agents.local", name="Conductor")
    return {"grok": grok["user"], "conductor": conductor}


def _issue(client: TestClient, headers, **fields) -> dict:
    response = client.post(
        "/issues", json={"title": "slice", **fields}, headers=dict(headers)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _assign(client: TestClient, issue_id: int, headers, **body):
    return client.post(
        f"/issues/{issue_id}/assign-seat",
        json={"seat_slug": "grok", **body},
        headers=dict(headers),
    )


def test_a_conductor_hands_a_slice_to_a_seat_by_slug_and_radios_it(tmp_path, pings):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        cast = _world(client)
        as_conductor = bearer(cast["conductor"])
        issue = _issue(client, as_conductor)

        answer = _assign(client, issue["id"], as_conductor, note="take it")

        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["issue_id"] == issue["id"]
        assert body["seat"] == "grok" and body["seat_name"] == "Grok"
        assert body["agent_id"] == cast["grok"]["id"]
        assert body["radio"]["status"] == "sent"
        assert body["receipt"]["status"] == "recorded"
        assert body["receipt"]["permalink"] == _PERMALINK
        # The desk write: the seat's account is the assignee and a delegate,
        # and the answer carries the issue's new tag for the next guarded write.
        read = client.get(f"/issues/{issue['id']}", headers=ADMIN)
        assert answer.headers["ETag"] == read.headers["ETag"]
        fresh = read.json()
        assert fresh["assignee_id"] == cast["grok"]["id"]
        people = client.get(f"/issues/{issue['id']}/contributors", headers=ADMIN).json()
        assert [p["user_id"] for p in people] == [cast["grok"]["id"]]
        # The seat was radioed once, by name, with the note.
        assert len(pings) == 1
        assert pings[0]["seat_name"] == "Grok"
        assert pings[0]["note"] == "take it"
        assert pings[0]["buzz_pubkey"] == "a" * 64
    verbs = [row["verb"] for row in events(db_file, "assigned", "delegated")]
    assert sorted(verbs) == ["assigned", "delegated"]
    assert len(events(db_file, buzz_radio.VERB_RADIOED)) == 1


def test_an_admin_scope_token_is_answered_not_401_or_404(tmp_path, pings):
    # C10: the cockpit path is a browser session, so an admin's token met 401
    # or 404 there. The REST twin answers the admin token as the admin.
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _world(client)
        issue = _issue(client, ADMIN)
        admin_token = mint_token(client, scopes=["admin"])
        answer = _assign(client, issue["id"], bearer(admin_token))
        assert answer.status_code == 200, answer.text
        assert answer.json()["seat"] == "grok"


def test_the_scope_and_role_are_checked_before_the_command(tmp_path, pings):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _world(client)
        viewer = add_user(client, "v@e.com", role="viewer")
        issue = _issue(client, ADMIN)
        read_only = mint_token(client, scopes=["read"])
        docs_only = mint_token(client, scopes=["read", "docs:write"])
        for token in (read_only, docs_only):
            refused = _assign(client, issue["id"], bearer(token))
            assert refused.status_code == 403, refused.text
            assert refused.json()["code"] == "scope_denied"
        refused = _assign(client, issue["id"], as_user(viewer["id"]))
        assert refused.status_code == 403
        assert refused.json()["code"] == "forbidden"
        no_one = client.post(
            f"/issues/{issue['id']}/assign-seat", json={"seat_slug": "grok"}
        )
        assert no_one.status_code == 401
        assert (
            client.get(f"/issues/{issue['id']}", headers=ADMIN).json()["assignee_id"]
            is None
        )
    assert pings == []


def test_a_writer_the_issue_gate_refuses_is_forbidden(tmp_path, pings):
    # The floor offers its chairs to every writer; the command then decides
    # whether this writer may act on this issue. REST is the same.
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _world(client)
        member = add_user(client, "m@e.com")
        issue = _issue(client, ADMIN)
        refused = _assign(client, issue["id"], as_user(member["id"]))
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "forbidden"
    assert pings == []


def test_an_issue_the_caller_cannot_see_reads_as_missing(tmp_path, pings):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        cast = _world(client)
        project = client.post(
            "/projects",
            json={"name": "Secret", "key": "SEC", "visibility": "private"},
            headers=ADMIN,
        ).json()
        # The seat may work the project; the conductor cannot see it.
        client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": cast["grok"]["id"]},
            headers=ADMIN,
        )
        hidden = _issue(client, ADMIN, project_id=project["id"])
        as_conductor = bearer(cast["conductor"])
        blind = _assign(client, hidden["id"], as_conductor)
        missing = _assign(client, 10**6, as_conductor)
        assert blind.status_code == 404, blind.text
        assert (blind.status_code, blind.json()) == (
            missing.status_code,
            missing.json(),
        )
        assert blind.json()["code"] == "not_found"
        # The control: who can see the issue assigns it through the same route.
        assert _assign(client, hidden["id"], ADMIN).status_code == 200
    assert len(pings) == 1


def test_a_seat_whose_agent_cannot_see_the_project_is_refused_like_an_unknown_account(
    tmp_path, pings
):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        cast = _world(client)
        project = client.post(
            "/projects",
            json={"name": "Secret", "key": "SEC", "visibility": "private"},
            headers=ADMIN,
        ).json()
        # The conductor may work the project; the seat's agent may not.
        joined = client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": cast["conductor"]["user"]["id"]},
            headers=ADMIN,
        )
        assert joined.status_code == 201, joined.text
        as_conductor = bearer(cast["conductor"])
        issue = _issue(client, as_conductor, project_id=project["id"])

        blind_seat = _assign(client, issue["id"], as_conductor)
        unknown_account = client.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": 10**6},
            headers=as_conductor,
        )
        assert blind_seat.status_code == 422, blind_seat.text
        assert (blind_seat.status_code, blind_seat.json()) == (
            unknown_account.status_code,
            unknown_account.json(),
        )
        # Nothing landed: no assignee, no delegate, no ping.
        assert (
            client.get(f"/issues/{issue['id']}", headers=ADMIN).json()["assignee_id"]
            is None
        )
        assert (
            client.get(f"/issues/{issue['id']}/contributors", headers=ADMIN).json()
            == []
        )

        # Given membership, the same seat takes the same slice.
        client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": cast["grok"]["id"]},
            headers=ADMIN,
        )
        assert _assign(client, issue["id"], as_conductor).status_code == 200
    assert len(pings) == 1


@pytest.mark.parametrize(
    "slug, detail",
    [("nope", "unknown seat 'nope'"), ("kevin", "not the operator")],
)
def test_a_seat_the_command_will_not_take_is_refused_with_a_code(
    tmp_path, pings, slug, detail
):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        cast = _world(client)
        declare_seat(client, "kevin", email="admin@e.com", kind="operator")
        as_conductor = bearer(cast["conductor"])
        issue = _issue(client, as_conductor)
        refused = _assign(client, issue["id"], as_conductor, seat_slug=slug)
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "validation_error"
        assert detail in refused.json()["detail"]
    assert pings == []


def test_a_failed_radio_never_rolls_back_the_assign(tmp_path, monkeypatch, caplog):
    # The radio's own reason can name a key file's path or carry the CLI's
    # stderr: the server log keeps it, the bearer caller gets a fixed sentence.
    reason = "cannot read key file: [Errno 13] Permission denied: '/etc/buzz/k'"
    forged = "\nWARNING athena: forged line"
    monkeypatch.setattr(
        buzz_radio,
        "send_assignment",
        lambda **kwargs: {"status": "failed", "detail": reason + forged},
    )
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        cast = _world(client)
        as_conductor = bearer(cast["conductor"])
        issue = _issue(client, as_conductor)
        with caplog.at_level("WARNING", logger="athena"):
            answer = _assign(client, issue["id"], as_conductor)
        assert answer.status_code == 200, answer.text
        assert answer.json()["radio"] == {
            "status": "failed",
            "detail": "the radio failed; the server log has the reason",
            "permalink": None,
        }
        assert "/etc/buzz" not in answer.text
        logged = [r.getMessage() for r in caplog.records if reason in r.getMessage()]
        # One line: the reason's own line break is escaped, not followed.
        assert logged and all("\n" not in line for line in logged)
        assert answer.json()["receipt"]["status"] == "skipped"
        assert (
            client.get(f"/issues/{issue['id']}", headers=ADMIN).json()["assignee_id"]
            == cast["grok"]["id"]
        )
    assert events(db_file, buzz_radio.VERB_RADIOED) == []


def test_the_conductor_profile_assigns_to_a_seat_over_mcp(tmp_path, pings):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        cast = _world(client)
        issue = _issue(client, bearer(cast["conductor"]))
        server = mcp_server_for(
            app, cast["conductor"], run_id="run-assign", profile="conductor"
        )
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert "assign_issue_to_seat" in names
        answer = asyncio.run(
            server.call_tool(
                "assign_issue_to_seat",
                {"issue_id": issue["id"], "seat_slug": "grok", "note": "go"},
            )
        )
        body = json.loads(answer[0][0].text)
        assert body["seat"] == "grok"
        assert body["agent_id"] == cast["grok"]["id"]
        assert body["radio"]["status"] == "sent"
        assert (
            client.get(f"/issues/{issue['id']}", headers=ADMIN).json()["assignee_id"]
            == cast["grok"]["id"]
        )
        # A seat profile does not carry it: handing out work is the conductor's.
        seat = mcp_server_for(app, cast["conductor"], run_id="run-seat", profile="seat")
        assert "assign_issue_to_seat" not in {
            tool.name for tool in asyncio.run(seat.list_tools())
        }
    assert [ping["note"] for ping in pings] == ["go"]


def test_the_command_answers_a_hidden_and_a_missing_issue_alike(tmp_path):
    # The command's own refusal for a missing issue says what the issue
    # commands say for a hidden one, so no transport can tell them apart.
    from athena.workflows import fleet_assign_commands

    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        _world(client)
    conn = db.connect(db_file)
    try:
        admin = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
        with pytest.raises(fleet_assign_commands.AssignError) as missing:
            fleet_assign_commands.assign_issue_to_seat(
                conn,
                actor=admin,
                issue_id=10**6,
                seat_slug="grok",
                radio=lambda **kwargs: {"status": "skipped"},
            )
        assert missing.value.kind == "not_found"
        assert missing.value.detail == "no such issue"
    finally:
        conn.close()
