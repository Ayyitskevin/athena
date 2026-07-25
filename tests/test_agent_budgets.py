"""Durable per-agent action budgets — the bounded half of the operator's loop.

VISION.md promises every agent action is "attributable, reversible, and BOUNDED".
Bounding was only the in-process token rate limiter, which forgets everything on
restart — a burst guard, not a budget. These pin the durable ceiling: the charge
lands inside the metered command's transaction (so a refused or failed write
spends nothing), it survives a restart, concurrent writes cannot both spend the
last unit, an exact idempotent retry is not charged twice, refusals reach the
trail, and metering stays strictly opt-in.
"""

from datetime import timedelta

from fastapi.testclient import TestClient

from athena.core import budgets, db
from athena.core._util import utc_now
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="budgets.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _set_budget(client, user_id, limit, window="day"):
    r = client.put(
        f"/users/{user_id}/budget",
        json={"window": window, "action_limit": limit},
        headers=H1,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, target_id, detail FROM activity WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _issue_count(db_file):
    conn = db.connect(db_file)
    n = conn.execute("SELECT COUNT(*) AS n FROM issues").fetchone()["n"]
    conn.close()
    return n


def test_supported_windows_are_a_closed_set():
    # The schema CHECK and the API Literal must agree with the module's own table;
    # a new window has to be added in all three places deliberately.
    assert sorted(budgets.WINDOWS) == ["day", "hour"]


def test_unbudgeted_actors_are_unlimited(tmp_path):
    # Metering is opt-in: applying the migration changes nothing until an operator
    # sets a ceiling, exactly like the blocked-close policy.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        for n in range(5):
            assert (
                c.post(
                    "/issues", json={"title": f"free {n}"}, headers=_bearer(agent)
                ).status_code
                == 201
            )
        assert c.get(f"/users/{agent['user']['id']}/budget", headers=H1).json() is None
    assert _issue_count(db_file) == 5


def test_budget_refuses_the_write_at_zero_and_records_it(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _set_budget(c, agent_id, 2)

        for n in range(2):
            assert (
                c.post(
                    "/issues", json={"title": f"in budget {n}"}, headers=_bearer(agent)
                ).status_code
                == 201
            )
        refused = c.post(
            "/issues", json={"title": "over budget"}, headers=_bearer(agent)
        )
        assert refused.status_code == 429
        body = refused.json()
        assert body["code"] == "agent_budget_exhausted"
        assert body["budget"]["remaining"] == 0
        # An agent can back off by the server's own stated interval.
        assert int(refused.headers["Retry-After"]) > 0

    # The refused write left no issue behind — the charge and the row are one unit.
    assert _issue_count(db_file) == 2
    # The refusal is on the trail: an agent hitting its ceiling is a decision the
    # operator should see, not silence.
    exhausted = _events(db_file, "agent_budget_exhausted")
    assert [e["actor_id"] for e in exhausted] == [agent["user"]["id"]]
    assert "POST /issues" in exhausted[0]["detail"]


def test_a_failed_write_spends_nothing(tmp_path):
    # The charge lives inside the command's transaction, so a write rejected AFTER
    # the charge (here: an unknown status) rolls the charge back with it. Otherwise
    # an agent could be drained by its own validation errors.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _set_budget(c, agent_id, 3)
        rejected = c.post(
            "/issues",
            json={"title": "bad", "status": "no-such-status"},
            headers=_bearer(agent),
        )
        assert rejected.status_code == 422
        assert c.get(f"/users/{agent_id}/budget", headers=H1).json()["action_used"] == 0


def test_idempotent_retry_is_charged_once(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _set_budget(c, agent_id, 5)
        keyed = {**_bearer(agent), "Idempotency-Key": "one-logical-write"}
        first = c.post("/issues", json={"title": "retried"}, headers=keyed)
        replay = c.post("/issues", json={"title": "retried"}, headers=keyed)
        assert first.status_code == 201 and replay.status_code == 201
        assert replay.headers["idempotent-replay"] == "true"
        # The replay never reaches the command, so the budget is spent exactly once
        # for the one logical write.
        assert c.get(f"/users/{agent_id}/budget", headers=H1).json()["action_used"] == 1
    assert _issue_count(db_file) == 1


def test_concurrent_writes_cannot_both_spend_the_last_unit(tmp_path):
    # The read-check-write runs under the command's BEGIN IMMEDIATE, so SQLite's
    # single-writer reservation serializes two racing charges.
    import threading

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _set_budget(c, agent_id, 1)

        results: list[int] = []
        start = threading.Barrier(2)

        def attempt(n):
            client = TestClient(app)
            start.wait()
            results.append(
                client.post(
                    "/issues", json={"title": f"race {n}"}, headers=_bearer(agent)
                ).status_code
            )

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(results) == [201, 429]
    assert _issue_count(db_file) == 1


def test_budget_survives_restart_and_rolls_into_a_fresh_window(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _set_budget(c, agent_id, 1, window="hour")
        assert (
            c.post("/issues", json={"title": "spent"}, headers=_bearer(agent))
        ).status_code == 201
        assert (
            c.post("/issues", json={"title": "refused"}, headers=_bearer(agent))
        ).status_code == 429

    # A new process sees the same spent budget — this is the durable half the
    # in-process rate limiter never had.
    with TestClient(create_app(db_file)) as c2:
        assert (
            c2.post("/issues", json={"title": "still refused"}, headers=_bearer(agent))
        ).status_code == 429

    # Age the window past its length: the next charge resets and re-anchors it.
    conn = db.connect(db_file)
    stale = (utc_now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE agent_budgets SET window_started_at = ? WHERE user_id = ?",
        (stale, agent["user"]["id"]),
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_file)) as c3:
        assert (
            c3.post("/issues", json={"title": "new window"}, headers=_bearer(agent))
        ).status_code == 201


def test_set_is_audited_idempotent_and_preserves_consumption(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        _set_budget(c, agent_id, 1)
        c.post("/issues", json={"title": "spend it"}, headers=_bearer(agent))
        # Re-setting the identical ceiling records nothing new.
        _set_budget(c, agent_id, 1)
        # Raising the limit releases the agent WITHOUT gifting a fresh window:
        # consumption is preserved.
        raised = _set_budget(c, agent_id, 3)
        assert raised["action_used"] == 1 and raised["remaining"] == 2
        assert (
            c.post("/issues", json={"title": "released"}, headers=_bearer(agent))
        ).status_code == 201

        assert c.delete(f"/users/{agent_id}/budget", headers=H1).status_code == 204
        assert c.get(f"/users/{agent_id}/budget", headers=H1).json() is None

    assert [
        (e["actor_id"], e["detail"]) for e in _events(db_file, "agent_budget_set")
    ] == [
        (1, "1 actions per day"),
        (1, "3 actions per day"),
    ]
    assert [e["target_id"] for e in _events(db_file, "agent_budget_cleared")] == [
        agent["user"]["id"]
    ]


def test_budget_reads_are_self_or_admin_and_writes_are_admin_only(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        other = _agent(c, email="other@e.com")
        _set_budget(c, agent_id, 4)

        # An agent discovers its OWN ceiling by asking — the whole point of
        # publishing it — including through whoami.
        own = c.get(f"/users/{agent_id}/budget", headers=_bearer(agent))
        assert own.status_code == 200 and own.json()["action_limit"] == 4
        me = c.get("/users/me", headers=_bearer(agent)).json()
        assert me["budget"]["action_limit"] == 4

        # But not someone else's, and it cannot set its own ceiling.
        assert (
            c.get(f"/users/{agent_id}/budget", headers=_bearer(other)).status_code
            == 403
        )
        assert (
            c.put(
                f"/users/{agent_id}/budget",
                json={"window": "day", "action_limit": 999},
                headers=_bearer(agent),
            ).status_code
            == 403
        )
        # An unbudgeted actor reports null rather than a fabricated ceiling.
        assert c.get("/users/me", headers=_bearer(other)).json()["budget"] is None


def test_invalid_budget_input_is_refused(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        agent_id = agent["user"]["id"]
        assert (
            c.put(
                f"/users/{agent_id}/budget",
                json={"window": "fortnight", "action_limit": 5},
                headers=H1,
            ).status_code
            == 422
        )
        assert (
            c.put(
                f"/users/{agent_id}/budget",
                json={"window": "day", "action_limit": -1},
                headers=H1,
            ).status_code
            == 422
        )
        assert (
            c.put(
                "/users/9999/budget",
                json={"window": "day", "action_limit": 5},
                headers=H1,
            ).status_code
            == 404
        )
        # Zero is legitimate: a hard freeze on metered writes that leaves reads working.
        assert _set_budget(c, agent_id, 0)["action_limit"] == 0
        assert (
            c.post("/issues", json={"title": "frozen"}, headers=_bearer(agent))
        ).status_code == 429
        assert c.get("/issues", headers=_bearer(agent)).status_code == 200


def test_page_writes_are_metered_too(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        onboarded = c.post(
            "/users/onboard_agent",
            json={
                "email": "doc@e.com",
                "name": "Doc",
                "scopes": ["read", "docs:write"],
            },
            headers=H1,
        ).json()
        agent_id = onboarded["user"]["id"]
        space = c.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1).json()
        _set_budget(c, agent_id, 1)
        assert (
            c.post(
                f"/spaces/{space['id']}/pages",
                json={"title": "First"},
                headers=_bearer(onboarded),
            ).status_code
            == 201
        )
        assert (
            c.post(
                f"/spaces/{space['id']}/pages",
                json={"title": "Second"},
                headers=_bearer(onboarded),
            ).status_code
            == 429
        )


def test_automation_rule_firings_are_not_metered(tmp_path):
    # A budget bounds what AGENTS initiate. It must never silently stop a rule the
    # operator configured, so rule firings go through the unmetered command path.
    from athena.aegis import automation

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = c.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "role": "member"},
            headers=H1,
        ).json()
        conn = db.connect(db_file)
        automation.create_rule(
            conn,
            name="assign",
            trigger_verb="created",
            action_type="assign",
            action_params={"user_id": member["id"]},
            created_by=1,
        )
        conn.commit()
        # Budget the automation system actor to zero — the most hostile setting.
        system_id = automation.system_actor_id(conn)
        conn.commit()
        conn.close()
        _set_budget(c, system_id, 0)

        issue = c.post("/issues", json={"title": "auto"}, headers=H1).json()
        conn = db.connect(db_file)
        fired = automation.process_pending(
            conn,
            executor=lambda cx, r, e: automation.execute_action(
                cx, r, e, actor_id=automation.system_actor_id(cx)
            ),
        )
        conn.commit()
        conn.close()
    assert fired == 1
    assigned = _events(db_file, "assigned")
    assert [e["target_id"] for e in assigned] == [issue["id"]]
