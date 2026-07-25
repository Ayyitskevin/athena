"""The worker registry and its cooperative kill signal.

Athena models WHO an agent is but has never modeled WHERE it runs. These pin the
registry and, more importantly, the two words it refuses to say.

*Alive*: a heartbeat proves a worker REPORTED at a moment. A worker that goes
quiet is stale, and staleness is derived from the server clock at read time.

*Killed*: Athena cannot signal a foreign OS process. A kill is an instruction the
worker collects on its next heartbeat and answers for itself, so the operator sees
three separate facts — asked, acknowledged, stopped — and never a fabricated
fourth one.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from athena.core import db, worker_commands, workers
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="workers.db"):
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


def _beat(client, headers, **payload):
    payload.setdefault("worker_key", "worker-1")
    return client.put("/workers/heartbeat", json=payload, headers=headers)


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, target_id, detail FROM activity WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_a_worker_registers_itself_and_is_readable(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        registered = _beat(
            c,
            _bearer(agent),
            node_label="thinkpad",
            capabilities=["python", "docker", "python"],
        )
        assert registered.status_code == 200, registered.text
        body = registered.json()
        assert body["worker_key"] == "worker-1"
        assert body["node_label"] == "thinkpad"
        # De-duplicated, order preserved. Athena stores labels; it never routes on them.
        assert body["capabilities"] == ["python", "docker"]
        assert body["reporting_state"] == "reporting_recently"
        assert body["kill_state"] == "none"
        assert body["kill_requested"] is False
        # Credential metadata never rides along on a read projection.
        assert "last_token_id" not in body

        # The admin sees the fleet; the agent sees its own.
        assert [w["id"] for w in c.get("/workers", headers=H1).json()] == [body["id"]]
        assert [w["id"] for w in c.get("/workers", headers=_bearer(agent)).json()] == [
            body["id"]
        ]

    # First registration joins the trail; refreshes deliberately do not.
    assert len(_events(db_file, "worker_registered")) == 1


def test_refreshing_updates_in_place_without_flooding_the_trail(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        first = _beat(c, _bearer(agent), node_label="thinkpad").json()
        again = _beat(c, _bearer(agent), node_label="thinkpad-2").json()
        assert again["id"] == first["id"]
        assert again["node_label"] == "thinkpad-2"
        assert again["first_seen_at"] == first["first_seen_at"]
        assert len(c.get("/workers", headers=H1).json()) == 1
    # A heartbeat every few seconds is operational state. Recording each one would
    # drown the log it exists to inform.
    assert len(_events(db_file, "worker_registered")) == 1


def test_the_kill_is_a_request_the_worker_collects_and_answers(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]

        asked = c.post(f"/workers/{worker_id}/kill", headers=H1)
        assert asked.status_code == 200
        # Recorded as an instruction, NOT as an outcome.
        assert asked.json()["kill_state"] == "requested"
        assert asked.json()["kill_acknowledged_at"] is None
        assert asked.json()["stopped_at"] is None

        # The worker finds out by coming back — this is the whole mechanism.
        told = _beat(c, _bearer(agent)).json()
        assert told["kill_requested"] is True
        assert told["kill_state"] == "requested"

        heard = _beat(c, _bearer(agent), state="stopping").json()
        assert heard["kill_acknowledged_at"] is not None
        # Still reporting after acknowledging is its own reading, not "stopped".
        assert heard["kill_state"] == "acknowledged_but_reporting"
        # And it is STILL told, because it has not stopped yet.
        assert heard["kill_requested"] is True

        done = _beat(c, _bearer(agent), state="stopped").json()
        assert done["stopped_at"] is not None
        assert done["kill_state"] == "stopped"
        assert done["kill_requested"] is False

    assert len(_events(db_file, "worker_kill_requested")) == 1
    assert len(_events(db_file, "worker_kill_acknowledged")) == 1
    assert len(_events(db_file, "worker_stopped")) == 1


def test_a_silent_worker_is_stale_never_terminated(tmp_path):
    # The single most important refusal in this module: Athena cannot see a
    # process, so "it stopped answering" must never render as "it stopped".
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]
        c.post(f"/workers/{worker_id}/kill", headers=H1)

    conn = db.connect(db_file)
    later = datetime.now(UTC) + timedelta(hours=2)
    stale = workers.get_worker(conn, worker_id, now=later)
    assert stale["reporting_state"] == "stale"
    # Asked, never answered. Not "stopped", not "killed".
    assert stale["kill_state"] == "requested"
    assert stale["stopped_at"] is None
    assert stale["kill_acknowledged_at"] is None
    conn.close()


def test_a_restart_does_not_cancel_the_operator_s_instruction(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]
        c.post(f"/workers/{worker_id}/kill", headers=H1)
        _beat(c, _bearer(agent), state="stopped")

        # The process comes back under the same key. The stop it claimed is
        # contradicted by fresher evidence, but the instruction stands — a restart
        # must never quietly undo what the operator asked for.
        back = _beat(c, _bearer(agent)).json()
        assert back["stopped_at"] is None
        assert back["kill_requested_at"] is not None
        assert back["kill_state"] == "acknowledged_but_reporting"
        assert back["kill_requested"] is True


def test_a_worker_that_acknowledged_then_went_quiet_reads_as_acknowledged(tmp_path):
    # The honest middle ground: it heard, then stopped reporting. That is NOT
    # evidence it stopped — only that it stopped talking — so the registry says
    # acknowledged and stale, and leaves the conclusion to the operator.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]
        c.post(f"/workers/{worker_id}/kill", headers=H1)
        _beat(c, _bearer(agent), state="stopping")

    conn = db.connect(db_file)
    later = datetime.now(UTC) + timedelta(hours=2)
    quiet = workers.get_worker(conn, worker_id, now=later)
    assert quiet["reporting_state"] == "stale"
    assert quiet["kill_state"] == "acknowledged"
    assert quiet["stopped_at"] is None
    conn.close()


def test_re_asking_a_worker_that_claimed_it_stopped_clears_the_stale_claim(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]
        c.post(f"/workers/{worker_id}/kill", headers=H1)
        _beat(c, _bearer(agent), state="stopped")

        # The operator asks again — evidently they do not believe the claim, or it
        # came back. The registry must not read "stopped" while an instruction is
        # live, so the old claim goes and the new request stands alone.
        again = c.post(f"/workers/{worker_id}/kill", headers=H1).json()
        assert again["stopped_at"] is None
        assert again["kill_acknowledged_at"] is None
        assert again["kill_state"] == "requested"
        assert _beat(c, _bearer(agent)).json()["kill_requested"] is True
    assert len(_events(db_file, "worker_kill_requested")) == 2


def test_asking_twice_does_not_reset_how_long_it_has_been_ignoring_you(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]
        first = c.post(f"/workers/{worker_id}/kill", headers=H1).json()
        second = c.post(f"/workers/{worker_id}/kill", headers=H1).json()
        # That timestamp is the number the operator is watching.
        assert second["kill_requested_at"] == first["kill_requested_at"]
    assert len(_events(db_file, "worker_kill_requested")) == 1


def test_a_request_can_be_withdrawn_until_it_is_acknowledged(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]
        c.post(f"/workers/{worker_id}/kill", headers=H1)

        withdrawn = c.delete(f"/workers/{worker_id}/kill", headers=H1)
        assert withdrawn.status_code == 200
        assert withdrawn.json()["kill_state"] == "none"
        assert _beat(c, _bearer(agent)).json()["kill_requested"] is False

        # Once the worker has heard, "never mind" is a claim about the world Athena
        # cannot make — it may already be shutting down.
        c.post(f"/workers/{worker_id}/kill", headers=H1)
        _beat(c, _bearer(agent), state="stopping")
        too_late = c.delete(f"/workers/{worker_id}/kill", headers=H1)
        assert too_late.status_code == 422
        assert "acknowledged" in too_late.json()["detail"]

        # Withdrawing when nothing was asked is a no-op, not an error.
        other = _beat(c, _bearer(agent), worker_key="worker-2").json()
        assert c.delete(f"/workers/{other['id']}/kill", headers=H1).status_code == 200
    assert len(_events(db_file, "worker_kill_cancelled")) == 1


def test_a_worker_stopping_on_its_own_acknowledges_nothing(tmp_path):
    # Shutting down for its own reasons is legitimate; there is no instruction to
    # acknowledge, so nothing is recorded as one.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        heard = _beat(c, _bearer(agent), state="stopping").json()
        assert heard["kill_state"] == "none"
        assert heard["kill_acknowledged_at"] is None
        stopped = _beat(c, _bearer(agent), state="stopped").json()
        assert stopped["stopped_at"] is not None
        assert stopped["kill_state"] == "none"
    assert _events(db_file, "worker_kill_acknowledged") == []
    assert len(_events(db_file, "worker_stopped")) == 1


def test_only_a_credential_holding_agent_may_heartbeat(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        # A human session or the trusted actor header carries no token: a worker is
        # a process holding a credential, and fiction must not enter the registry.
        assert _beat(c, H1).status_code == 403
        # A read-only token cannot write its own presence either.
        reader = _agent(c, email="ro@e.com", scopes=("read",))
        assert _beat(c, _bearer(reader)).status_code == 403
        # Nor can a HUMAN's token: the registry describes agent processes, and a
        # person's laptop session is not one of them.
        human = c.post(
            "/users",
            json={"email": "kev@e.com", "name": "Kev", "password": "pw"},
            headers=H1,
        ).json()
        human_token = c.post(
            "/tokens",
            json={"name": "laptop", "scopes": ["read", "issue:write"]},
            headers={"X-Athena-Actor": str(human["id"])},
        ).json()
        assert (
            _beat(c, {"Authorization": f"Bearer {human_token['token']}"}).status_code
            == 403
        )
        # A revoked token stops reporting immediately — the check is re-resolved
        # inside the write transaction, not trusted from the request.
        agent = _agent(c, email="live@e.com")
        assert _beat(c, _bearer(agent)).status_code == 200
        c.delete(f"/users/{agent['user']['id']}/tokens", headers=H1)
        assert _beat(c, _bearer(agent)).status_code == 401


def test_a_paused_agent_cannot_be_told_anything(tmp_path):
    # Pause freezes the credential, so a paused worker cannot heartbeat and
    # therefore cannot learn it was asked to stop. Order matters, and the operator
    # should see the request sitting unanswered rather than assume it landed.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]
        c.put(
            f"/users/{agent['user']['id']}/paused",
            json={"paused": True},
            headers=H1,
        )
        c.post(f"/workers/{worker_id}/kill", headers=H1)

        assert _beat(c, _bearer(agent)).status_code == 403
        assert c.get(f"/workers/{worker_id}", headers=H1).json()["kill_state"] == (
            "requested"
        )

        # Resumed, it collects the instruction it could not hear before.
        c.put(
            f"/users/{agent['user']['id']}/paused",
            json={"paused": False},
            headers=H1,
        )
        assert _beat(c, _bearer(agent)).json()["kill_requested"] is True


def test_one_agent_cannot_see_or_stop_another_s_workers(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        mine = _agent(c)
        theirs = _agent(c, email="luna@e.com")
        my_worker = _beat(c, _bearer(mine)).json()["id"]
        their_worker = _beat(c, _bearer(theirs), worker_key="worker-1").json()["id"]
        # The same key under two agents is two workers, not a collision.
        assert my_worker != their_worker

        assert [w["id"] for w in c.get("/workers", headers=_bearer(mine)).json()] == [
            my_worker
        ]
        # Naming another agent's id does not widen the view.
        assert (
            c.get(
                f"/workers?agent_id={theirs['user']['id']}", headers=_bearer(mine)
            ).json()
            == []
        )
        # A worker belonging to someone else is the same 404 a missing one gives.
        assert c.get(f"/workers/{their_worker}", headers=_bearer(mine)).status_code == (
            404
        )
        assert c.get("/workers/999999", headers=_bearer(mine)).status_code == 404
        # An ADMIN acting through a token that lacks the admin scope is refused
        # too: the role is not the whole answer, the credential's reach is.
        weak = c.post(
            "/tokens",
            json={"name": "weak", "scopes": ["read", "issue:write"]},
            headers=H1,
        ).json()
        assert (
            c.post(
                f"/workers/{my_worker}/kill",
                headers={"Authorization": f"Bearer {weak['token']}"},
            ).status_code
            == 403
        )
        # And a non-admin cannot leave an instruction at all.
        assert (
            c.post(f"/workers/{their_worker}/kill", headers=_bearer(mine)).status_code
            == 403
        )
        assert (
            c.post(f"/workers/{my_worker}/kill", headers=_bearer(mine)).status_code
            == 403
        )


def test_input_is_bounded_and_the_registry_has_a_ceiling(tmp_path, monkeypatch):
    from athena import config

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        assert _beat(c, _bearer(agent), worker_key="   ").status_code == 422
        # worker_key is opaque at the schema layer (like a run id), so the command
        # is the real validation boundary — non-strings and control characters have
        # to be refused THERE, not merely by Pydantic.
        assert _beat(c, _bearer(agent), worker_key=17).status_code == 422
        assert _beat(c, _bearer(agent), worker_key="a\u0007b").status_code == 422
        assert _beat(c, _bearer(agent), node_label="a\u0007b").status_code == 422
        assert _beat(c, _bearer(agent), node_label="x" * 201).status_code == 422
        assert _beat(c, _bearer(agent), capabilities=["a" * 101]).status_code == 422
        assert (
            _beat(c, _bearer(agent), capabilities=[f"c{n}" for n in range(21)])
        ).status_code == 422
        assert _beat(c, _bearer(agent), state="exploded").status_code == 422

        monkeypatch.setattr(config, "WORKER_MAX_PER_AGENT", 1)
        assert _beat(c, _bearer(agent), worker_key="one").status_code == 200
        # A looping or compromised token may refresh what it has forever, but it
        # cannot keep growing the registry.
        capped = _beat(c, _bearer(agent), worker_key="two")
        assert capped.status_code == 429
        assert _beat(c, _bearer(agent), worker_key="one").status_code == 200


def test_worker_events_are_admin_only_on_the_trail(tmp_path):
    # Node labels and kill instructions are operator infrastructure. The event
    # visibility clause is a closed whitelist, so an unlisted target kind is
    # admin-only by default — fail-closed, and worth pinning.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent), node_label="thinkpad").json()["id"]
        c.post(f"/workers/{worker_id}/kill", headers=H1)

        admin_feed = c.get("/activity", headers=H1).json()
        assert any(e["verb"] == "worker_kill_requested" for e in admin_feed)
        agent_feed = c.get("/activity", headers=_bearer(agent)).json()
        assert all(not e["verb"].startswith("worker_") for e in agent_feed)


def test_the_cockpit_shows_workers_and_leaves_the_instruction(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        agent = _agent(c)
        worker_id = _beat(
            c, _bearer(agent), node_label="thinkpad", capabilities=["docker"]
        ).json()["id"]

        page = c.get("/admin/agents")
        assert "thinkpad" in page.text
        assert "docker" in page.text
        assert "Ask to stop" in page.text

        asked = c.post(f"/admin/workers/{worker_id}/kill", follow_redirects=False)
        assert asked.status_code == 303
        # The notice says what actually happened: asked, not stopped.
        assert "asked%20to%20stop" in asked.headers["location"]
        page = c.get("/admin/agents")
        assert "Told to stop" in page.text

        withdrawn = c.post(
            f"/admin/workers/{worker_id}/kill",
            data={"cancel": "1"},
            follow_redirects=False,
        )
        assert "notice=" in withdrawn.headers["location"]
        assert c.get(f"/workers/{worker_id}", headers=H1).json()["kill_state"] == "none"

        # A refusal comes back as a notice, not an error page.
        c.post(f"/admin/workers/{worker_id}/kill", follow_redirects=False)
        _beat(c, _bearer(agent), state="stopping")
        refused = c.post(
            f"/admin/workers/{worker_id}/kill",
            data={"cancel": "1"},
            follow_redirects=False,
        )
        assert "error=" in refused.headers["location"]


def test_command_guards_below_the_transport(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        worker_id = _beat(c, _bearer(agent)).json()["id"]

    conn = db.connect(db_file)
    with pytest.raises(worker_commands.WorkerCommandError) as anonymous:
        worker_commands.heartbeat(conn, actor=None, worker_key="x")
    assert anonymous.value.kind == "unauthorized"
    # Shapes Pydantic screens out over HTTP still have to be refused here: the
    # command is the validation boundary, not the transport.
    with pytest.raises(worker_commands.WorkerCommandError) as bad_label:
        worker_commands.heartbeat(
            conn,
            actor={
                "id": 2,
                "is_agent": True,
                "_token_id": 1,
                "_token_scopes": ["admin"],
            },
            worker_key="x",
            node_label=17,
        )
    assert bad_label.value.kind == "invalid"
    with pytest.raises(worker_commands.WorkerCommandError) as no_admin:
        worker_commands.request_kill(conn, actor=None, worker_id=worker_id)
    assert no_admin.value.kind == "unauthorized"
    with pytest.raises(worker_commands.WorkerCommandError) as missing:
        worker_commands.request_kill(
            conn, actor={"id": 1, "role": "admin"}, worker_id=999999
        )
    assert missing.value.kind == "not_found"
    with pytest.raises(worker_commands.WorkerCommandError) as cancel_missing:
        worker_commands.cancel_kill(
            conn, actor={"id": 1, "role": "admin"}, worker_id=999999
        )
    assert cancel_missing.value.kind == "not_found"
    # Capability shapes are validated in the data layer, before anything is stored.
    with pytest.raises(ValueError):
        workers.join_capabilities("python")
    with pytest.raises(ValueError):
        workers.join_capabilities([1])
    with pytest.raises(ValueError):
        workers.join_capabilities(["a,b"])
    assert workers.join_capabilities(None) == ""
    assert workers.join_capabilities(["  ", "ok"]) == "ok"
    with pytest.raises(ValueError):
        workers.list_workers(conn, limit=0)
    with pytest.raises(ValueError):
        workers.get_worker(conn, worker_id, stale_seconds=0)
    conn.close()
