"""One derived state per run, read from facts Athena already stores.

A run is an agent's run id. Its state is computed at read time, with no new rows:

- ``awaiting_input``: the run has an open question for a human. Either it
  yielded a claim with a blocking question nobody has resumed, or it raised an
  approval that is still pending.
- ``complete``: the run's latest claim act was ``complete_claim``.
- ``active``: the run recorded real work (the progress view's work verbs) within
  the named window.
- ``pinging``: only check-ins within that window.
- ``stale``: none of those.

Precedence runs top to bottom, so a heartbeat can never lift ``awaiting_input``
or ``stale`` to ``active``: only recorded work makes a run active, and an open
question outranks everything. Nothing enforces a response time.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from athena.aegis import fleet_attention, run_state
from athena.core import db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
ADMIN = {"id": 1, "role": "admin"}
_TS = "%Y-%m-%d %H:%M:%S"
WINDOW = run_state.ACTIVE_WINDOW_MINUTES


def _app(tmp_path, name="runstate.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _login(client):
    client.post(
        "/login", data={"email": "a@e.com", "password": "pw"}, follow_redirects=False
    )


def _agent(client, email="sol@e.com", name="Sol"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _as(agent, run):
    return {
        "Authorization": f"Bearer {agent['token']['token']}",
        "X-Athena-Run": run,
    }


def _ago(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).strftime(_TS)


def _sql(db_file, statement, params=()):
    conn = db.connect(db_file)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _claimed(client, agent, run, title, **fields):
    issue = client.post("/issues", json={"title": title, **fields}, headers=H1).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    tag = client.get(f"/issues/{issue['id']}", headers=_as(agent, run)).headers["etag"]
    claimed = client.post(
        f"/issues/{issue['id']}/claim", headers={**_as(agent, run), "If-Match": tag}
    )
    assert claimed.status_code in (200, 201), claimed.text
    return issue, claimed.json()


def _beat(client, agent, run):
    beat = client.put(
        "/agent-runs/heartbeat", json={"run_id": run}, headers=_as(agent, run)
    )
    assert beat.status_code == 200, beat.text


def _comment(client, agent, run, issue_id, body="work"):
    posted = client.post(
        f"/issues/{issue_id}/comments", json={"body": body}, headers=_as(agent, run)
    )
    assert posted.status_code == 201, posted.text


def _yield(client, agent, run, issue_id, generation, question="Which path?"):
    yielded = client.post(
        f"/issues/{issue_id}/yield",
        json={
            "generation": generation,
            "reason": "needs_input",
            "attempted_work": "Read the code.",
            "evidence": ["notes"],
            "blocking_question": question,
            "resume_instructions": "Pick a path.",
        },
        headers=_as(agent, run),
    )
    assert yielded.status_code == 201, yielded.text
    return yielded.json()


def _state(db_file, agent, run):
    conn = db.connect(db_file)
    try:
        return run_state.state_for_run(conn, agent_id=agent["user"]["id"], run_id=run)[
            "state"
        ]
    finally:
        conn.close()


def _age_run(db_file, agent, run, *, minutes):
    """Move every native event and check-in of this run back in time."""
    stamp = _ago(minutes=minutes)
    _sql(
        db_file,
        "UPDATE activity SET created_at = ? WHERE run_id = ? AND actor_id = ? "
        "AND verb NOT IN ('claim_yielded', 'claim_handoff_resumed')",
        (stamp, run, agent["user"]["id"]),
    )
    _sql(
        db_file,
        # Both columns: a CHECK keeps last_seen_at >= first_seen_at.
        "UPDATE agent_run_checkins SET first_seen_at = ?, last_seen_at = ? "
        "WHERE run_id = ?",
        (stamp, stamp, run),
    )


# ── one test per state, each with a positive control ────────────────────────


def test_active_is_recorded_work_within_the_window(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue, _ = _claimed(c, agent, "run-active", "busy")
        _comment(c, agent, "run-active", issue["id"])
        assert _state(db_file, agent, "run-active") == "active"
        # Positive control: the same work, older than the window, is stale.
        _age_run(db_file, agent, "run-active", minutes=WINDOW + 5)
        assert _state(db_file, agent, "run-active") == "stale"


def test_pinging_is_check_ins_only(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        _claimed(c, agent, "run-ping", "reporting")
        _age_run(db_file, agent, "run-ping", minutes=WINDOW + 5)
        _beat(c, agent, "run-ping")
        assert _state(db_file, agent, "run-ping") == "pinging"
        # Positive control: the same check-in, older than the window, is stale.
        _age_run(db_file, agent, "run-ping", minutes=WINDOW + 5)
        assert _state(db_file, agent, "run-ping") == "stale"


def test_stale_is_nothing_in_the_window(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue, _ = _claimed(c, agent, "run-quiet", "quiet")
        _age_run(db_file, agent, "run-quiet", minutes=WINDOW * 4)
        assert _state(db_file, agent, "run-quiet") == "stale"
        # A claim or a renewal is bookkeeping, not work: still stale.
        tag = c.get(f"/issues/{issue['id']}", headers=H1).headers["etag"]
        lease = c.get(f"/issues/{issue['id']}/lease", headers=H1).json()
        renewed = c.post(
            f"/issues/{issue['id']}/claim",
            json={"generation": lease["generation"]},
            headers={**_as(agent, "run-quiet"), "If-Match": tag},
        )
        assert renewed.status_code in (200, 201), renewed.text
        assert _state(db_file, agent, "run-quiet") == "stale"
        # Positive control: one piece of work makes it active.
        _comment(c, agent, "run-quiet", issue["id"])
        assert _state(db_file, agent, "run-quiet") == "active"


def test_awaiting_input_from_an_open_blocking_question(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        asker = _agent(c, "ask@e.com", "Asker")
        helper = _agent(c, "help@e.com", "Helper")
        issue, lease = _claimed(c, asker, "run-ask", "needs a decision")
        handoff = _yield(c, asker, "run-ask", issue["id"], lease["generation"])
        assert _state(db_file, asker, "run-ask") == "awaiting_input"
        # Positive control: once the handoff is resumed, the question is answered
        # and the run is no longer awaiting input.
        c.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": helper["user"]["id"]},
            headers=H1,
        )
        tag = c.get(f"/issues/{issue['id']}", headers=H1).headers["etag"]
        claimed = c.post(
            f"/issues/{issue['id']}/claim",
            headers={**_as(helper, "run-help"), "If-Match": tag},
        )
        assert claimed.status_code in (200, 201), claimed.text
        resumed = c.post(
            f"/issues/{issue['id']}/claim-handoffs/{handoff['handoff_token']}/resume",
            json={"generation": claimed.json()["generation"]},
            headers=_as(helper, "run-help"),
        )
        assert resumed.status_code in (200, 201), resumed.text
        assert _state(db_file, asker, "run-ask") != "awaiting_input"


def test_awaiting_input_from_a_pending_approval_the_run_raised(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue, _ = _claimed(c, agent, "run-gate", "gated")
        _sql(
            db_file,
            "INSERT INTO approval_requests (action_kind, target_kind, target_id, "
            "requested_by, run_id) VALUES ('issue.close', 'issue', ?, ?, 'run-gate')",
            (issue["id"], agent["user"]["id"]),
        )
        assert _state(db_file, agent, "run-gate") == "awaiting_input"
        # Positive control: another run's pending approval does not count.
        assert _state(db_file, agent, "run-other") != "awaiting_input"
        # And once decided, it is no longer waiting.
        conn = db.connect(db_file)
        request_id = conn.execute("SELECT id FROM approval_requests").fetchone()["id"]
        conn.close()
        decided = c.post(
            f"/approvals/{request_id}/decision",
            json={"decision": "approve"},
            headers=H1,
        )
        assert decided.status_code == 200, decided.text
        assert _state(db_file, agent, "run-gate") != "awaiting_input"


def test_complete_after_complete_claim(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue, lease = _claimed(c, agent, "run-done", "finish me")
        _beat(c, agent, "run-done")
        done = c.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": lease["generation"]},
            headers=_as(agent, "run-done"),
        )
        assert done.status_code == 200, done.text
        assert _state(db_file, agent, "run-done") == "complete"
        # The cockpit's per-run rows show it: a completed claim has left active
        # work, so the run row is where "complete" is visible.
        _login(c)
        html = " ".join(c.get("/admin/agents/runs").text.split())
        assert 'data-run-state="complete"' in html
        # Positive control: claiming again in the same run makes it not complete.
        tag = c.get(f"/issues/{issue['id']}", headers=H1).headers["etag"]
        again = c.post(
            f"/issues/{issue['id']}/claim",
            headers={**_as(agent, "run-done"), "If-Match": tag},
        )
        assert again.status_code in (200, 201), again.text
        assert _state(db_file, agent, "run-done") != "complete"


def test_a_run_still_holding_another_claim_is_not_complete(tmp_path):
    # Completing one claim while the same run still holds another is not the run
    # being complete: the latest claim act is a completion, but work is held.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        first, first_lease = _claimed(c, agent, "run-two", "first of two")
        second, second_lease = _claimed(c, agent, "run-two", "second of two")
        for issue, lease in ((first, first_lease),):
            done = c.post(
                f"/issues/{issue['id']}/complete",
                json={"generation": lease["generation"]},
                headers=_as(agent, "run-two"),
            )
            assert done.status_code == 200, done.text
        assert _state(db_file, agent, "run-two") != "complete"
        # Positive control: completing the other one too makes the run complete.
        done = c.post(
            f"/issues/{second['id']}/complete",
            json={"generation": second_lease["generation"]},
            headers=_as(agent, "run-two"),
        )
        assert done.status_code == 200, done.text
        assert _state(db_file, agent, "run-two") == "complete"


# ── a heartbeat never makes a run active ────────────────────────────────────


def test_a_heartbeat_never_turns_awaiting_input_or_stale_into_active(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue, lease = _claimed(c, agent, "run-q", "question")
        _yield(c, agent, "run-q", issue["id"], lease["generation"])
        quiet, _ = _claimed(c, agent, "run-s", "stale")
        _age_run(db_file, agent, "run-s", minutes=WINDOW * 4)
        assert _state(db_file, agent, "run-q") == "awaiting_input"
        assert _state(db_file, agent, "run-s") == "stale"
        for _ in range(3):
            _beat(c, agent, "run-q")
            _beat(c, agent, "run-s")
        assert _state(db_file, agent, "run-q") == "awaiting_input"
        assert _state(db_file, agent, "run-s") == "pinging"
        # Even real work does not lift an open question: it still waits on a human.
        _comment(c, agent, "run-q", issue["id"])
        assert _state(db_file, agent, "run-q") == "awaiting_input"
        # Positive control: real work does make the stale run active.
        _comment(c, agent, "run-s", quiet["id"])
        assert _state(db_file, agent, "run-s") == "active"


def test_derive_state_precedence_is_fixed():
    now = datetime.now(UTC)
    recent = (now - timedelta(minutes=1)).strftime(_TS)
    old = (now - timedelta(minutes=WINDOW + 1)).strftime(_TS)
    derive = run_state.derive_state
    assert (
        derive(
            awaiting=True,
            completed=True,
            last_work_at=recent,
            last_checkin_at=recent,
            now=now,
        )[0]
        == "awaiting_input"
    )
    assert (
        derive(
            awaiting=False,
            completed=True,
            last_work_at=recent,
            last_checkin_at=recent,
            now=now,
        )[0]
        == "complete"
    )
    assert (
        derive(
            awaiting=False,
            completed=False,
            last_work_at=recent,
            last_checkin_at=None,
            now=now,
        )[0]
        == "active"
    )
    assert (
        derive(
            awaiting=False,
            completed=False,
            last_work_at=old,
            last_checkin_at=recent,
            now=now,
        )[0]
        == "pinging"
    )
    assert (
        derive(
            awaiting=False,
            completed=False,
            last_work_at=old,
            last_checkin_at=old,
            now=now,
        )[0]
        == "stale"
    )
    assert (
        derive(
            awaiting=False,
            completed=False,
            last_work_at=None,
            last_checkin_at=None,
            now=now,
        )[0]
        == "stale"
    )


# ── where it shows ──────────────────────────────────────────────────────────


def test_active_work_and_mission_control_show_the_state(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        busy, _ = _claimed(c, agent, "run-busy", "busy one")
        _comment(c, agent, "run-busy", busy["id"])
        _beat(c, agent, "run-busy")
        idle, _ = _claimed(c, agent, "run-idle", "idle one")
        _beat(c, agent, "run-idle")
        page = c.get("/fleet/active-work", headers=H1).json()
        by_title = {item["issue"]["title"]: item for item in page["items"]}
        assert by_title["busy one"]["run_state"]["state"] == "active"
        assert by_title["idle one"]["run_state"]["state"] == "pinging"
        assert by_title["busy one"]["run_state"]["window_minutes"] == WINDOW
        assert by_title["busy one"]["run_state"]["scope"] == "run"
        # Attention reasons are unchanged by the new field.
        assert "checkin_missing" not in by_title["idle one"]["attention_reasons"]
        _login(c)
        html = " ".join(c.get("/admin/agents/runs").text.split())
        busy_row = html.split("busy one", 1)[1].split("</tr>", 1)[0]
        idle_row = html.split("idle one", 1)[1].split("</tr>", 1)[0]
        assert 'data-run-state="active"' in busy_row
        assert 'data-run-state="pinging"' in idle_row


def test_awaiting_input_ranks_above_stale_and_quiet_signals(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        asked, lease = _claimed(c, agent, "run-ask", "asked first")
        _yield(c, agent, "run-ask", asked["id"], lease["generation"])
        # A stale claim (no check-in, expired lease) is a high-severity signal.
        stale, _ = _claimed(c, agent, "run-old", "stale claim")
        _sql(
            db_file,
            "UPDATE issue_leases SET expires_at = ? WHERE issue_id = ?",
            (_ago(hours=2), stale["id"]),
        )
        # The stale claim's signal is the older one (oldest-first within a
        # severity), so only the severity can put the question above it.
        _sql(
            db_file,
            "UPDATE issue_leases SET claimed_at = ? WHERE issue_id = ?",
            (_ago(days=3), stale["id"]),
        )
        conn = db.connect(db_file)
        ranking = fleet_attention.build_attention_ranking(conn, actor=ADMIN)
        conn.close()
        signals = [item.signal for item in ranking["items"]]
        assert "awaiting_input" in signals and "claim_needs_attention" in signals
        assert signals.index("awaiting_input") < signals.index("claim_needs_attention")
        top = ranking["items"][0]
        assert top.signal == "awaiting_input" and top.severity == "critical"
        assert top.source_id == asked["id"]
        assert "Which path?" in top.reason
        assert "awaiting_input" in fleet_attention.RANKING_SIGNALS


def test_questions_lane_sits_beside_approvals_for_the_operator(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue, lease = _claimed(c, agent, "run-ask", "needs you")
        # One pending approval, so the approval queue renders on the page too.
        _sql(
            db_file,
            "INSERT INTO approval_requests (action_kind, target_kind, target_id, "
            "requested_by) VALUES ('issue.close', 'issue', ?, 1)",
            (issue["id"],),
        )
        _login(c)
        # Positive control: before anyone asks, the lane says it is empty.
        empty = " ".join(c.get("/admin/agents").text.split())
        assert 'id="questions"' in empty
        assert "No open questions" in empty.split('id="questions"', 1)[1]
        _yield(c, agent, "run-ask", issue["id"], lease["generation"], "Ship or hold?")
        html = " ".join(c.get("/admin/agents").text.split())
        lane = html.split('id="questions"', 1)[1]
        assert "Ship or hold?" in lane and "needs you" in lane and "Sol" in lane
        assert f'href="/aegis/issues/{issue["id"]}"' in lane
        assert "No open questions" not in lane.split("</section>", 1)[0]
        assert "Nothing enforces a response time" in html
        # It sits beside the approval queue on the same page.
        assert html.index('id="questions"') > html.index("Actions an agent asked")
        conn = db.connect(db_file)
        lane_data = run_state.open_questions(conn, actor=ADMIN)
        conn.close()
        assert lane_data["total"] == 1


def test_the_agents_desk_lists_its_own_open_questions(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        mine = _agent(c, "me@e.com", "Me")
        other = _agent(c, "you@e.com", "You")
        issue, lease = _claimed(c, mine, "run-me", "my question")
        _yield(c, mine, "run-me", issue["id"], lease["generation"], "Mine?")
        theirs, their_lease = _claimed(c, other, "run-you", "their question")
        _yield(c, other, "run-you", theirs["id"], their_lease["generation"], "Theirs?")
        _sql(
            db_file,
            "INSERT INTO approval_requests (action_kind, target_kind, target_id, "
            "requested_by, run_id) VALUES ('issue.close', 'issue', ?, ?, 'run-me')",
            (issue["id"], mine["user"]["id"]),
        )
        desk = c.get(
            "/desk", headers={"Authorization": f"Bearer {mine['token']['token']}"}
        ).json()
        lane = desk["work"]["waiting_on_operator"]
        kinds = sorted(row["kind"] for row in lane["items"])
        assert kinds == ["approval", "question"]
        assert lane["total"] == 2
        question = next(r for r in lane["items"] if r["kind"] == "question")
        assert question["blocking_question"] == "Mine?"
        # Positive control: the other agent's question is on its desk, not mine.
        assert "Theirs?" not in str(lane)
        their_desk = c.get(
            "/desk", headers={"Authorization": f"Bearer {other['token']['token']}"}
        ).json()
        assert [
            r["blocking_question"]
            for r in their_desk["work"]["waiting_on_operator"]["items"]
        ] == ["Theirs?"]
        assert "response time" in lane["meaning"]


# ── visibility and K1 ───────────────────────────────────────────────────────


def test_hidden_projects_stay_hidden(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        outsider = c.post(
            "/users",
            json={"email": "out@e.com", "name": "Out", "password": "pw"},
            headers=H1,
        ).json()
        project = c.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=H1
        ).json()
        hidden, lease = _claimed(
            c, agent, "run-h", "hidden question", project_id=project["id"]
        )
        _yield(c, agent, "run-h", hidden["id"], lease["generation"], "Secret?")
        public, public_lease = _claimed(c, agent, "run-p", "public question")
        _yield(c, agent, "run-p", public["id"], public_lease["generation"], "Open?")
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        conn = db.connect(db_file)
        out = users.get_user(conn, outsider["id"])
        theirs = run_state.open_questions(conn, actor=out)
        ranking = fleet_attention.build_attention_ranking(
            conn, actor=out, signals={"awaiting_input"}
        )
        mine = run_state.open_questions(conn, actor=ADMIN)
        conn.close()
        assert [r["issue"]["id"] for r in theirs["items"]] == [public["id"]]
        assert theirs["total"] == 1
        assert [i.source_id for i in ranking["items"]] == [public["id"]]
        assert ranking["total"] == 1
        assert "Secret?" not in str(theirs) + str(ranking)
        # Positive control: the admin sees both.
        assert {r["issue"]["id"] for r in mine["items"]} == {hidden["id"], public["id"]}
        assert mine["total"] == 2


def test_reading_the_states_writes_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue, lease = _claimed(c, agent, "run-r", "read me")
        _yield(c, agent, "run-r", issue["id"], lease["generation"])
        conn = db.connect(db_file)
        before = conn.total_changes
        run_state.state_for_run(conn, agent_id=agent["user"]["id"], run_id="run-r")
        run_state.open_questions(conn, actor=ADMIN)
        fleet_attention.build_attention_ranking(conn, actor=ADMIN)
        assert conn.total_changes == before
        conn.close()
