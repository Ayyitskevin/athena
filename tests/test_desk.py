"""Stage F-1 — the Desk: one bounded read that orients an agent.

The contract under test is composition and honesty, not novelty. Every lane
must be the owning surface's own read with the CALLER's visibility, so the desk
can never show an agent something the owning tool would hide; every list must
carry its bound; and the cursor must behave like a bookmark that only moves
forward — never a lock, never a queue, never a claim about history.

The distinction this suite exists to keep alive: a cursor that has never been
set reads as `null`, not `0`. "I have never looked" and "nothing has happened
since I looked" are different facts, and collapsing them would make a fresh
agent believe it was caught up.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena.aegis import desk, desk_commands
from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError

H1 = {"X-Athena-Actor": "1"}
PASSWORD = "pw-long-enough"


def _app(tmp_path, name="desk.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post(
        "/users", json={"email": "a@e.com", "name": "Ann", "password": PASSWORD}
    )


def _agent(client, email="sol@e.com", name="Sol", scopes=("read", "issue:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _desk(client, headers):
    response = client.get("/desk", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- shape and zero state ---------------------------------------------------


def test_a_fresh_agent_desk_is_empty_and_says_so_without_lying(tmp_path):
    # WHY: the first call an agent ever makes must be legible. Every lane is
    # present and empty rather than absent, and the cursor is null — NOT zero,
    # which would read as "you are caught up" to an agent that has never read.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        grok = _agent(client, email="grok@agents.local", name="Grok")
        board = _desk(client, _bearer(sol))
        grok_desk = _desk(client, _bearer(grok))
        assert grok_desk["identity"]["seat_slug"] == "grok"

    assert board["schema"] == desk.SCHEMA
    assert board["identity"]["id"] == sol["user"]["id"]
    assert board["identity"]["is_agent"] is True
    assert board["identity"]["paused"] is False
    assert board["identity"]["is_admin"] is False
    assert board["identity"]["budget"] is None  # unbudgeted, not zero
    assert board["identity"]["approval_required"] == []
    assert sorted(board["identity"]["scopes"]) == ["issue:write", "read"]
    # sol@e.com is not a declared fleet seat
    assert board["identity"]["seat_slug"] is None
    assert board["work"]["protocol"]["claim_one_issue"] is True
    assert board["work"]["protocol"]["do_not_touch_other_work"] is True
    assert board["office"]["schema"] == "athena.office.v1"
    assert board["office"]["seated"] is False
    assert board["asks"]["run_controls"]["items"] == []
    assert board["asks"]["worker_kill_requests"]["items"] == []
    assert board["asks"]["claim_handoffs"]["items"] == []
    assert board["work"]["delegations"]["items"] == []
    assert board["work"]["leases"] == {
        "items": [],
        "total": 0,
        "limit": desk.LEASES_LIMIT,
        "meaning": board["work"]["leases"]["meaning"],
    }
    assert board["signals"]["unread_notifications"] == 0
    assert board["cursor"] is None
    # The distinction this whole test exists for.
    assert board["signals"]["events_since_cursor"] is None


def test_every_lane_reports_what_it_is_and_what_it_bounds(tmp_path):
    # WHY: a bounded read that does not say it is bounded is a read that lies
    # by omission. Each list carries its limit, and the claim-like lanes carry
    # the owning surface's epistemic wording rather than a summary of it.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        board = _desk(client, H1)

    assert board["asks"]["run_controls"]["limit"] == desk.CONTROLS_LIMIT
    assert "not yet answered" in board["asks"]["run_controls"]["meaning"]
    assert "cannot signal a process" in board["asks"]["worker_kill_requests"]["meaning"]
    assert "acknowledged" in board["asks"]["claim_handoffs"]["meaning"]
    assert board["work"]["delegations"]["limit"] == desk.DELEGATIONS_LIMIT
    assert "clock's verdict" in board["work"]["leases"]["meaning"]


# --- the lanes, each populated ----------------------------------------------


def test_an_open_control_reaches_the_agent_it_was_addressed_to(tmp_path):
    # WHY: the asks lane is the reason the desk exists — an operator asked
    # something of this run, and the agent must see it without knowing the
    # controls API. Settling it clears the lane on the next read.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        client.post(
            "/issues",
            json={"title": "tagged"},
            headers={**bearer, "X-Athena-Run": "g1"},
        )
        created = client.post(
            "/run-controls",
            json={"run_id": "g1", "kind": "steer", "payload": "use approach B"},
            headers=H1,
        )
        assert created.status_code == 201, created.text

        board = _desk(client, bearer)
        controls = board["asks"]["run_controls"]["items"]
        assert [c["kind"] for c in controls] == ["steer"]
        assert controls[0]["payload"] == "use approach B"

        settled = client.post(
            f"/run-controls/{created.json()['id']}/complete",
            json={"summary": "did it"},
            headers=bearer,
        )
        assert settled.status_code == 200, settled.text
        assert _desk(client, bearer)["asks"]["run_controls"]["items"] == []


def test_a_kill_request_appears_for_the_worker_owner_only_while_unconfirmed(tmp_path):
    # WHY: "the operator asked your worker to stop" is the most urgent thing a
    # desk can carry, and it must disappear when the WORKER answers — not when
    # anyone else declares it handled.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        beat = client.put(
            "/workers/heartbeat", json={"worker_key": "w-1"}, headers=bearer
        )
        assert beat.status_code == 200, beat.text
        assert (
            client.post(f"/workers/{beat.json()['id']}/kill", headers=H1).status_code
            == 200
        )

        asked = _desk(client, bearer)["asks"]["worker_kill_requests"]["items"]
        assert len(asked) == 1
        assert asked[0]["worker_key"] == "w-1"
        assert asked[0]["kill_state"] == "requested"
        assert asked[0]["last_reported_at"]  # what the worker REPORTED

        for state in ("stopping", "stopped"):
            client.put(
                "/workers/heartbeat",
                json={"worker_key": "w-1", "state": state},
                headers=bearer,
            )
        assert _desk(client, bearer)["asks"]["worker_kill_requests"]["items"] == []


def test_leases_show_what_i_hold_with_the_clocks_verdict(tmp_path):
    # WHY: `active` is derived at read time from expires_at, never stored, so a
    # lapsed lease reads as lapsed the instant it lapses. An expired row stays
    # VISIBLE on purpose — losing a lease silently is worse than seeing it.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        issue = client.post("/issues", json={"title": "work"}, headers=H1).json()
        delegated = client.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": sol["user"]["id"]},
            headers=H1,
        )
        assert delegated.status_code == 201, delegated.text
        # Claiming is optimistic-concurrency gated: the current issue ETag must
        # ride along, so two agents cannot claim the same snapshot.
        etag = client.get(f"/issues/{issue['id']}", headers=bearer).headers["ETag"]
        claimed = client.post(
            f"/issues/{issue['id']}/claim", headers={**bearer, "If-Match": etag}
        )
        assert claimed.status_code in (200, 201), claimed.text

        board = _desk(client, bearer)
        held = board["work"]["leases"]
        assert held["total"] == 1
        assert held["items"][0]["issue_id"] == issue["id"]
        assert held["items"][0]["active"] is True
        assert held["items"][0]["generation"]  # 0057 fencing rides along

    # Expire it in the past: same row, and the derived flag flips with no write.
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE issue_leases SET expires_at = '2000-01-01 00:00:00' WHERE issue_id = ?",
        (issue["id"],),
    )
    conn.commit()
    from athena.aegis import leases

    lapsed = leases.leases_held_by(conn, holder_id=sol["user"]["id"])
    conn.close()
    assert len(lapsed) == 1 and lapsed[0]["active"] is False


def test_notifications_and_delegations_reach_the_desk(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        issue = client.post("/issues", json={"title": "for you"}, headers=H1).json()
        delegated = client.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": sol["user"]["id"]},
            headers=H1,
        )
        assert delegated.status_code == 201, delegated.text

        board = _desk(client, bearer)
        # A delegation item nests the issue it is about.
        assert [d["issue"]["id"] for d in board["work"]["delegations"]["items"]] == [
            issue["id"]
        ]
        item = board["work"]["delegations"]["items"][0]
        assert item["issue_etag"]
        assert item["how_to_claim"]["from"] == "issue_etag"
        assert item["complete_does_not_close_issue"] is True
        assert item["runbook"]["exists"] is False
        assert board["signals"]["unread_notifications"] >= 1
        assert board["signals"]["notifications"]


# --- visibility --------------------------------------------------------------


def test_a_desk_is_yours_alone(tmp_path):
    # WHY: there is no parameter naming another reader, and no lane may leak
    # one. Another agent's controls and delegations must be invisible here even
    # though both agents exist in the same workspace.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        mira = _agent(client, email="mira@e.com", name="Mira")
        sol_bearer, mira_bearer = _bearer(sol), _bearer(mira)
        client.post(
            "/issues",
            json={"title": "sol's"},
            headers={**sol_bearer, "X-Athena-Run": "sol-run"},
        )
        client.post(
            "/run-controls",
            json={"run_id": "sol-run", "kind": "steer", "payload": "for sol"},
            headers=H1,
        )

        assert len(_desk(client, sol_bearer)["asks"]["run_controls"]["items"]) == 1
        mira_board = _desk(client, mira_bearer)
        assert mira_board["asks"]["run_controls"]["items"] == []
        assert mira_board["identity"]["id"] == mira["user"]["id"]


def test_a_human_operator_gets_a_desk_too(tmp_path):
    # WHY: the desk is agent-shaped but not agent-only; a human reading theirs
    # must not hit an agent-only query. Workers are an agent concept, so that
    # lane is empty for a human rather than an error.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        board = _desk(client, H1)
    assert board["identity"]["is_agent"] is False
    assert board["identity"]["is_admin"] is True
    assert board["asks"]["worker_kill_requests"]["items"] == []


def test_anonymous_callers_have_no_desk(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        assert client.get("/desk").status_code == 401
        assert client.post("/desk/cursor", json={"after_id": 1}).status_code == 401


def test_a_paused_agent_is_refused_at_identity_like_every_other_call(tmp_path):
    # WHY: pause is enforced at identity resolution for EVERY authenticated
    # call (docs/RUN_CONTROLS.md, WORKERS.md). The desk inherits that rather
    # than carving an exception — a paused agent cannot read its way around it.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        assert client.get("/desk", headers=bearer).status_code == 200
        assert (
            client.put(
                f"/users/{sol['user']['id']}/paused", json={"paused": True}, headers=H1
            ).status_code
            == 200
        )
        refused = client.get("/desk", headers=bearer)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "account is paused"


# --- the cursor --------------------------------------------------------------


def test_the_cursor_moves_forward_is_idempotent_and_never_rewinds(tmp_path):
    # WHY: a cursor is a bookmark. Re-acknowledging the same position is a
    # no-op (a retry must be safe); moving below it is refused, because
    # unsaying an acknowledgement is a claim about history, not a position.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        # A brand-new agent can see none of the bootstrap trail, so its
        # latest-visible id is honestly null. Give it one event of its own.
        client.post("/issues", json={"title": "mine"}, headers=bearer)
        board = _desk(client, bearer)
        latest = board["signals"]["latest_visible_event_id"]
        assert latest is not None

        first = client.post("/desk/cursor", json={"after_id": latest}, headers=bearer)
        assert first.status_code == 200, first.text
        assert first.json()["after_id"] == latest

        after = _desk(client, bearer)
        assert after["cursor"]["after_id"] == latest
        assert after["signals"]["events_since_cursor"] == 0  # now a real zero

        again = client.post("/desk/cursor", json={"after_id": latest}, headers=bearer)
        assert again.status_code == 200
        assert again.json()["after_id"] == latest

        rewind = client.post("/desk/cursor", json={"after_id": 1}, headers=bearer)
        assert rewind.status_code == 409
        assert "may not move backward" in rewind.json()["detail"]
        assert _desk(client, bearer)["cursor"]["after_id"] == latest


def test_the_database_refuses_a_rewind_even_without_the_command(tmp_path):
    # WHY: the command's 409 is the clean answer; the trigger is the backstop
    # for any writer that skips it. Both exist because the property is about
    # what the row may claim, not about one code path being careful.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        client.post("/issues", json={"title": "mine"}, headers=bearer)
        latest = _desk(client, bearer)["signals"]["latest_visible_event_id"]
        client.post("/desk/cursor", json={"after_id": latest}, headers=bearer)

    conn = db.connect(db_file)
    with pytest.raises(sqlite3.IntegrityError, match="may not move backward"):
        conn.execute(
            "UPDATE agent_cursors SET after_id = 1 WHERE user_id = ?",
            (sol["user"]["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        conn.execute(
            "UPDATE agent_cursors SET user_id = 1 WHERE user_id = ?",
            (sol["user"]["id"],),
        )
    conn.close()


def test_events_since_cursor_counts_only_what_this_reader_may_see(tmp_path):
    # WHY: the count and the drain must agree. It uses the same visibility gate
    # GET /events applies, so an agent never sees "3 new" and then drains two.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        client.post("/issues", json={"title": "mine"}, headers=bearer)
        latest = _desk(client, bearer)["signals"]["latest_visible_event_id"]
        client.post("/desk/cursor", json={"after_id": latest}, headers=bearer)

        for title in ("one", "two", "three"):
            client.post("/issues", json={"title": title}, headers=H1)

        board = _desk(client, bearer)
        visible = client.get(
            "/events", params={"after": latest, "limit": 200}, headers=bearer
        ).json()
        assert board["signals"]["events_since_cursor"] == len(visible["events"])
        assert board["signals"]["events_since_cursor_capped"] is False


def test_the_since_count_admits_its_ceiling(tmp_path):
    # WHY: an exact five-figure backlog is noise; "500+, drain from here" is
    # actionable. When the cap bites the projection SAYS so rather than
    # reporting a precise-looking number it did not compute.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        # Give the agent visible events of its own to count.
        for title in ("one", "two"):
            client.post("/issues", json={"title": title}, headers=bearer)
    conn = db.connect(db_file)
    try:
        monkeyed = desk.EVENTS_SINCE_CAP
        from athena.core import users as core_users

        actor = core_users.get_user(conn, sol["user"]["id"])
        count, capped = desk._events_since(conn, actor=actor, after_id=0)
        assert capped is False and count >= 2
        # Force the ceiling with a tiny cap rather than 500 fixture rows.
        desk.EVENTS_SINCE_CAP = 1
        count, capped = desk._events_since(conn, actor=actor, after_id=0)
        assert (count, capped) == (1, True)
    finally:
        desk.EVENTS_SINCE_CAP = monkeyed
        conn.close()


def test_cursor_bounds_are_refused_before_the_driver_sees_them(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        for bad in (0, -1, 2**63):
            assert (
                client.post(
                    "/desk/cursor", json={"after_id": bad}, headers=bearer
                ).status_code
                == 422
            ), bad
        assert (
            client.post(
                "/desk/cursor", json={"after_id": "seven"}, headers=bearer
            ).status_code
            == 422
        )


def test_the_command_guards_below_the_transport(tmp_path):
    # WHY: the route's schema is one gate; a direct caller must hit the same
    # refusals, because the command owns the rule.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
    conn = db.connect(db_file)
    actor = {"id": sol["user"]["id"], "role": "member", "is_agent": True}
    try:
        for bad in (0, -3, True, "9", 2**63):
            with pytest.raises(desk_commands.DeskCommandError) as caught:
                desk_commands.advance_cursor(conn, actor=actor, after_id=bad)
            assert caught.value.kind == "invalid"
        with pytest.raises(desk_commands.DeskCommandError) as unknown:
            desk_commands.advance_cursor(conn, actor=actor, after_id=5, name="nope")
        assert unknown.value.kind == "invalid"
        desk_commands.advance_cursor(conn, actor=actor, after_id=5)
        with pytest.raises(desk_commands.DeskCommandError) as back:
            desk_commands.advance_cursor(conn, actor=actor, after_id=4)
        assert back.value.kind == "conflict"
        assert desk_commands.STATUS_BY_KIND[back.value.kind] == 409
    finally:
        conn.close()


def test_advancing_the_cursor_writes_no_activity_event(tmp_path):
    # WHY: a read receipt is personal state (COMMAND_MIGRATION.md), so it must
    # not appear in fleet history. If it did, every agent's polling would
    # pollute the trail the trail exists to keep clean.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        client.post("/issues", json={"title": "mine"}, headers=bearer)
        latest = _desk(client, bearer)["signals"]["latest_visible_event_id"]

        # Baseline AFTER the setup writes, so the only thing between the two
        # counts is the cursor advance itself.
        conn = db.connect(db_file)
        before = conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()["n"]
        conn.close()
        assert (
            client.post(
                "/desk/cursor", json={"after_id": latest}, headers=bearer
            ).status_code
            == 200
        )

    conn = db.connect(db_file)
    after = conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()["n"]
    conn.close()
    assert after == before


# --- MCP parity ---------------------------------------------------------------


def test_mcp_reads_the_desk_and_moves_the_cursor(tmp_path):
    # WHY: steering rule 1 — no capability without an MCP path. The tools hit
    # the same REST surface, so parity is structural rather than asserted twice.
    app, _ = _app(tmp_path)
    with TestClient(app) as tc:
        _bootstrap(tc)
        sol = _agent(tc)
        tc.headers.update(_bearer(sol))
        api = AthenaClient(client=tc)

        board = api.my_desk()
        assert board["schema"] == desk.SCHEMA
        assert board["cursor"] is None

        tc.post("/issues", json={"title": "mine"})
        board = api.my_desk()
        latest = board["signals"]["latest_visible_event_id"]
        moved = api.advance_desk_cursor(after_id=latest)
        assert moved["after_id"] == latest
        assert api.my_desk()["cursor"]["after_id"] == latest

        with pytest.raises(AthenaError) as refused:
            api.advance_desk_cursor(after_id=1)
        assert refused.value.status_code == 409
