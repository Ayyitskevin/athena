"""Steering by exception: the cockpit must show the operator what needs them.

Three failures, one theme — an exception surface that quietly hides exceptions is
worse than none, because it reads as reassurance.

1. Active work ordered active-claims-first and expired-last, so on a busy fleet the
   attention-bearing rows were exactly the ones the limit dropped, while the
   returned-items summary could truthfully say "0 need attention" about a page it
   had filtered clean of them.
2. Refusals — failed logins, revoked tokens, scope denials, paused accounts — were
   recorded and rendered nowhere, readable only by an operator who already knew the
   four verb names.
3. Every exception surface lived on its own page, so steering by exception meant
   knowing all six places to look.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from athena.aegis import fleet_attention, fleet_work
from athena.core import db, security_events
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="cockpit.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", scopes=("read", "issue:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _claimed_issue(client, agent, *, title, reporting=True):
    issue = client.post("/issues", json={"title": title}, headers=H1).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    read = client.get(f"/issues/{issue['id']}", headers=_bearer(agent))
    claimed = client.post(
        f"/issues/{issue['id']}/claim",
        headers={
            **_bearer(agent),
            "If-Match": read.headers["ETag"],
            "X-Athena-Run": f"run-{issue['id']}",
        },
    )
    assert claimed.status_code in (200, 201), claimed.text
    if reporting:
        # A claim with no cooperative check-in is itself an attention reason
        # (checkin_missing), so a genuinely quiet row has to report.
        beat = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": f"run-{issue['id']}"},
            headers=_bearer(agent),
        )
        assert beat.status_code == 200, beat.text
    return issue


def _expire_lease(db_file, issue_id):
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE issue_leases SET expires_at = datetime('now', '-1 hour') "
        "WHERE issue_id = ?",
        (issue_id,),
    )
    conn.commit()
    conn.close()


def test_attention_rows_lead_the_page_instead_of_being_clipped(tmp_path):
    # The regression this fixes: with the limit at 1 and one expired claim among
    # several active ones, the OLD ordering returned an active row and a summary
    # reading "0 need attention" — about the one page that excluded the only claim
    # that did.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        for index in range(3):
            _claimed_issue(c, agent, title=f"quiet {index}")
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])

        page = c.get("/fleet/active-work?limit=1", headers=H1).json()
        assert len(page["items"]) == 1
        assert page["items"][0]["issue"]["id"] == urgent["id"]
        assert page["items"][0]["attention_state"] == "needs_attention"
        assert page["summary"]["needs_attention_count"] == 1
        # And the page says how many rows the decision saw, so "1 of 1 returned"
        # cannot be read as "1 in the whole fleet".
        assert page["examined_count"] == 1
        assert page["clipped"] is True
        assert page["visible_total"] == 4


def test_the_attention_filter_returns_exactly_the_attention_rows(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        quiet = _claimed_issue(c, agent, title="quiet")
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])

        needs = c.get(
            "/fleet/active-work?attention_state=needs_attention", headers=H1
        ).json()
        assert [item["issue"]["id"] for item in needs["items"]] == [urgent["id"]]
        assert needs["attention_state"] == "needs_attention"
        # The filter is applied to the EXACT state computed per row, not to the
        # coarse SQL ordering predicate, so nothing merely attention-prone leaks in.
        assert all(
            item["attention_state"] == "needs_attention" for item in needs["items"]
        )

        observed = c.get(
            "/fleet/active-work?attention_state=observed", headers=H1
        ).json()
        assert quiet["id"] in [item["issue"]["id"] for item in observed["items"]]
        assert all(item["attention_state"] == "observed" for item in observed["items"])

        # A value that is not a state is refused rather than silently ignored.
        assert (
            c.get("/fleet/active-work?attention_state=urgent", headers=H1).status_code
            == 422
        )
        with pytest.raises(fleet_work.ActiveWorkQueryError):
            fleet_work.parse_attention_state("nope")
        assert fleet_work.parse_attention_state("") is None


def test_the_cockpit_can_filter_to_attention(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        agent = _agent(c)
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])
        _claimed_issue(c, agent, title="quiet")

        page = c.get("/admin/agents/runs?attention_state=needs_attention")
        assert page.status_code == 200
        assert "urgent" in page.text
        assert "quiet" not in page.text
        # The control exists on the page, not only in the URL.
        assert 'name="attention_state"' in c.get("/admin/agents/runs").text
        # The same empty-select shape the agent filter accepts.
        assert c.get("/admin/agents/runs?attention_state=").status_code == 200
        assert c.get("/admin/agents/runs?attention_state=bogus").status_code == 400


def test_refusals_are_readable_without_knowing_the_verb_names(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        # A failed login and a scope denial: two different boundaries, both refused.
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "wrong"},
            follow_redirects=False,
        )
        reader = _agent(c, email="ro@e.com", scopes=("read",))
        c.post("/issues", json={"title": "nope"}, headers=_bearer(reader))

        events = c.get("/security/events", headers=H1).json()
        verbs = {event["verb"] for event in events}
        assert "login_failed" in verbs
        assert "scope_denied" in verbs
        # Each names the account whose boundary was hit.
        denial = next(e for e in events if e["verb"] == "scope_denied")
        assert denial["actor_email"] == "ro@e.com"
        assert denial["actor_is_agent"] is True

        counts = c.get("/security/counts", headers=H1).json()
        assert counts["login_failed"] >= 1
        assert counts["scope_denied"] >= 1
        # Zero-filled, so a quiet fleet reads as an explicit zero, not a gap.
        assert counts["paused_account_refused"] == 0

        narrowed = c.get("/security/events?verb=login_failed", headers=H1).json()
        assert {event["verb"] for event in narrowed} == {"login_failed"}
        # The verb vocabulary is closed: this surface cannot be turned into a
        # general activity reader wearing a security name.
        assert c.get("/security/events?verb=created", headers=H1).status_code == 422


def test_the_security_surface_is_admin_only(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = c.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "password": "pw"},
            headers=H1,
        ).json()
        member_headers = {"X-Athena-Actor": str(member["id"])}
        # A list of who has been probing is operator intelligence, not history.
        assert c.get("/security/events", headers=member_headers).status_code == 403
        assert c.get("/security/counts", headers=member_headers).status_code == 403
        assert c.get("/security/events").status_code == 401


def test_the_rollup_counts_every_exception_surface(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]

        # One of each: an expired claim, a pending approval, an unanswered kill,
        # a budget ceiling hit, and a refusal.
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])
        c.put(
            f"/approvals/policies/{agent_id}",
            json={"action_kind": "issue.close"},
            headers=H1,
        )
        gated = c.post(
            "/issues", json={"title": "gated"}, headers=_bearer(agent)
        ).json()
        c.patch(
            f"/issues/{gated['id']}", json={"status": "done"}, headers=_bearer(agent)
        )
        worker_id = c.put(
            "/workers/heartbeat",
            json={"worker_key": "w-1"},
            headers=_bearer(agent),
        ).json()["id"]
        c.post(f"/workers/{worker_id}/kill", headers=H1)
        c.put(
            f"/users/{agent_id}/budget",
            json={"window": "day", "action_limit": 0},
            headers=H1,
        )
        c.post("/issues", json={"title": "over budget"}, headers=_bearer(agent))
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "wrong"},
            follow_redirects=False,
        )

    conn = db.connect(db_file)
    rollup = fleet_attention.build_attention(conn)
    counts = {signal["key"]: signal["count"] for signal in rollup["signals"]}
    assert counts["claims_needing_attention"] >= 1
    assert counts["pending_approvals"] == 1
    assert counts["unanswered_kills"] == 1
    assert counts["budget_exhaustions"] == 1
    assert counts["security_refusals"] >= 1
    assert rollup["total"] == sum(counts.values())
    # Each number says where it lives, so the card is navigable rather than decorative.
    assert all(signal["href"].startswith("/") for signal in rollup["signals"])
    assert all(signal["scope"] for signal in rollup["signals"])

    # The window is stated, and it bounds the event-counted signals: a refusal from
    # last month is not this morning's alarm.
    old = fleet_attention.build_attention(
        conn, now=datetime.now(UTC) + timedelta(days=30)
    )
    old_counts = {signal["key"]: signal["count"] for signal in old["signals"]}
    assert old_counts["security_refusals"] == 0
    assert old_counts["budget_exhaustions"] == 0
    # Standing state is NOT window-bounded — an approval nobody answered is still
    # waiting a month later, and hiding it would be the bug this card exists to fix.
    assert old_counts["pending_approvals"] == 1
    assert old_counts["unanswered_kills"] == 1

    with pytest.raises(ValueError):
        fleet_attention.build_attention(conn, window_hours=0)
    with pytest.raises(ValueError):
        fleet_attention.build_attention(conn, window_hours="24")
    # A naive datetime is read as UTC, like every other server-clock comparison.
    naive = fleet_attention.build_attention(conn, now=datetime(2999, 1, 1))
    assert naive["since"].startswith("2998-12-31")
    conn.close()


def test_the_dashboard_shows_the_rollup_to_admins_only(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        agent = _agent(c)
        urgent = _claimed_issue(c, agent, title="urgent")
        _expire_lease(db_file, urgent["id"])

        page = c.get("/aegis/dashboard")
        assert "Fleet attention" in page.text
        assert "Claims needing attention" in page.text
        # And the nav now reaches the surfaces that had no link at all.
        assert "/admin/agents/runs" in page.text
        assert "/admin/security" in page.text

    # A non-admin sees no rollup: every input is an admin-scoped read.
    with TestClient(app) as other:
        other.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "password": "pw"},
            headers=H1,
        )
        other.post(
            "/login",
            data={"email": "m@e.com", "password": "pw"},
            follow_redirects=False,
        )
        page = other.get("/aegis/dashboard")
        assert page.status_code == 200
        assert "Fleet attention" not in page.text


def test_a_quiet_fleet_says_so_without_claiming_health(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        page = c.get("/aegis/dashboard")
        assert "Nothing is asking for you right now" in page.text
        # The distinction the whole projection is built on.
        assert "not a promise that every" in page.text

    conn = db.connect(db_file)
    assert fleet_attention.build_attention(conn)["total"] == 0
    conn.close()


def test_the_security_page_lists_and_filters_refusals(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "wrong"},
            follow_redirects=False,
        )
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        page = c.get("/admin/security")
        assert page.status_code == 200
        assert "login_failed" in page.text
        assert c.get("/admin/security?verb=login_failed").status_code == 200
        # An unknown verb falls back to "any" rather than erroring — the same
        # lenient stance the activity feed's selects take with hand-edited URLs.
        assert c.get("/admin/security?verb=nonsense").status_code == 200


def test_security_reads_validate_their_bounds(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    with pytest.raises(ValueError):
        security_events.list_failures(conn, limit=0)
    with pytest.raises(ValueError):
        security_events.list_failures(conn, limit="50")
    with pytest.raises(ValueError):
        security_events.list_failures(conn, verb="created")
    assert security_events.list_failures(conn, since="2999-01-01 00:00:00") == []
    assert set(security_events.failure_counts(conn)) == set(
        security_events.SECURITY_VERBS
    )
    conn.close()
