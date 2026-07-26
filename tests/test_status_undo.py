"""Undoing a status change.

`docs/UNDO.md` and `docs/COMMAND_MIGRATION.md` both said status was unreversible
"until prior state is recorded structurally". It already was: migration 0055
records ``before_status``, ``before_category``, and ``before_project_scope_key``
into ``issue_lifecycle_facts``, keyed 1:1 to the activity event, immutable, and
written in the same transaction — since well before undo existed. So this slice
adds an inverse and no migration.

What makes it non-trivial is that a scalar field has no domain idempotency. The
engine's safety net (``core/undo.undo``) refuses when the compensating command
records nothing, which works for archive and labels because re-archiving an
archived issue is a no-op. Writing a status is never a no-op. Without an explicit
still-in-force gate, undoing a stale event happily overwrites a NEWER value and
stamps that as a reversal — the trail would claim it reversed ``open →
in_progress`` while actually discarding ``done``. The first test below is exactly
that case, and it fails against the naive implementation.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="statusundo.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _user(client, email, *, role="member"):
    return client.post(
        "/users",
        json={
            "email": email,
            "name": email.split("@")[0],
            "password": "pw",
            "role": role,
        },
        headers=H1,
    ).json()


def _agent(client, email="sol@e.com", scopes=("read", "issue:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _project(client, name="P", key="P", headers=H1):
    return client.post(
        "/projects", json={"name": name, "key": key}, headers=headers
    ).json()


def _issue(client, *, project_id=None, headers=H1):
    payload = {"title": "t"}
    if project_id is not None:
        payload["project_id"] = project_id
    return client.post("/issues", json=payload, headers=headers).json()


def _set_status(client, issue_id, status, headers=H1):
    return client.patch(f"/issues/{issue_id}", json={"status": status}, headers=headers)


def _status_events(client, headers=H1):
    """Every changed_status event the caller can see, oldest first."""
    feed = client.get("/activity", params={"verb": "changed_status"}, headers=headers)
    return list(reversed(feed.json()))


def _undo(client, event_id, headers=H1):
    return client.post(f"/activity/{event_id}/undo", headers=headers)


def _rows(db_file, verb="changed_status"):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT id, verb, detail, reverses_event_id FROM activity "
        "WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_a_status_change_round_trips_and_both_rows_survive(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        _set_status(c, issue["id"], "in_progress")

        event = _status_events(c)[0]
        assert event["detail"] == "open → in_progress"

        undone = _undo(c, event["id"])
        assert undone.status_code == 201, undone.text
        body = undone.json()
        assert body["reversed_event_id"] == event["id"]
        assert body["reversal"]["verb"] == "changed_status"
        assert body["reversal"]["reverses_event_id"] == event["id"]

        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["status"] == "open"

    # Append-only: the original stands, the reversal sits beside it.
    rows = _rows(db_file)
    assert [(r["detail"], r["reverses_event_id"]) for r in rows] == [
        ("open → in_progress", None),
        ("in_progress → open", rows[0]["id"]),
    ]


def test_undoing_a_superseded_change_refuses_instead_of_overwriting(tmp_path):
    """The test that fails against a naive compensator.

    open → in_progress → done, then undo the FIRST transition. Writing
    ``before_status`` back would set the issue to 'open', discarding 'done', and
    record that as a reversal of a transition into 'in_progress' — a false entry on
    an append-only trail. The engine's own no-effect net cannot catch it, because
    the write is a real change and does record an event.
    """
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        _set_status(c, issue["id"], "in_progress")
        first = _status_events(c)[0]
        _set_status(c, issue["id"], "done")

        r = _undo(c, first["id"])
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "undo_no_effect"
        assert "done" in r.json()["detail"]

        # The newer value stands, untouched.
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["status"] == "done"

    # And crucially: no third row was written, so nothing claims a reversal.
    rows = _rows(db_file)
    assert [r["detail"] for r in rows] == ["open → in_progress", "in_progress → done"]
    assert all(r["reverses_event_id"] is None for r in rows)


def test_undoing_the_latest_change_works_after_a_superseded_one_was_refused(tmp_path):
    """The gate is about staleness, not about giving up on the issue entirely."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        _set_status(c, issue["id"], "in_progress")
        _set_status(c, issue["id"], "done")
        events = _status_events(c)

        assert _undo(c, events[0]["id"]).status_code == 409
        assert _undo(c, events[1]["id"]).status_code == 201
        assert (
            c.get(f"/issues/{issue['id']}", headers=H1).json()["status"]
            == "in_progress"
        )


def test_a_move_to_another_project_refuses_the_undo(tmp_path):
    """Statuses are per-project (0024) and a project move REMAPS the status, so a
    ``before_status`` captured under one project is not meaningful under another."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        source = _project(c, "Source", "SRC")
        destination = _project(c, "Dest", "DST")
        issue = _issue(c, project_id=source["id"])
        _set_status(c, issue["id"], "in_progress")
        event = _status_events(c)[0]

        c.patch(
            f"/issues/{issue['id']}",
            json={"project_id": destination["id"]},
            headers=H1,
        )
        r = _undo(c, event["id"])
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "undo_no_effect"
        assert "another project" in r.json()["detail"]


def test_a_backlog_issue_round_trips(tmp_path):
    """A backlog issue has no project, so both scope keys are NULL. The gate must
    read NULL == NULL as 'same envelope' rather than as a mismatch."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        assert issue["project_id"] is None
        _set_status(c, issue["id"], "done")
        event = _status_events(c)[0]

        assert _undo(c, event["id"]).status_code == 201
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["status"] == "open"


def test_an_event_with_no_structured_fact_is_refused_not_guessed(tmp_path):
    """0055 is deliberately not backfilled, so a pre-0055 event has no fact. The
    detail says 'open → in_progress' in prose, and the compensator must refuse
    rather than parse it — driving a write from a human-readable string is exactly
    what makes a compensator untrustworthy."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        _set_status(c, issue["id"], "in_progress")

        # The pre-0055 shape, written directly: a changed_status event whose prose
        # detail names a transition, with no lifecycle fact beside it. The facts
        # table is append-only (0055 triggers), so this is built rather than
        # produced by deleting a real one. A backlog issue keeps the visibility
        # envelope trivial, so the row is genuinely readable.
        conn = db.connect(db_file)
        legacy_id = conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, visibility_restricted) "
            "VALUES (1, 'changed_status', 'issue', ?, 'open → in_progress', 0)",
            (issue["id"],),
        ).lastrowid
        conn.commit()
        conn.close()

        r = _undo(c, legacy_id)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "undo_not_reversible"
        # The issue is untouched — no guess was made from the prose.
        assert (
            c.get(f"/issues/{issue['id']}", headers=H1).json()["status"]
            == "in_progress"
        )


def test_authorization_is_re_evaluated_as_the_undoing_actor(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        viewer = _user(c, "v@e.com", role="viewer")
        issue = _issue(c)
        _set_status(c, issue["id"], "in_progress")
        event = _status_events(c)[0]

        r = _undo(c, event["id"], headers={"X-Athena-Actor": str(viewer["id"])})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "undo_refused_by_command"


def test_a_token_without_the_issue_scope_cannot_undo_a_status(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        _set_status(c, issue["id"], "in_progress")
        event = _status_events(c)[0]

        read_only = _agent(c, email="ro@e.com", scopes=("read",))
        r = _undo(c, event["id"], headers=_bearer(read_only))
        assert r.status_code == 403, r.text


def test_the_same_event_cannot_be_undone_twice(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        _set_status(c, issue["id"], "in_progress")
        event = _status_events(c)[0]

        assert _undo(c, event["id"]).status_code == 201
        # Put it back by hand so the still-in-force gate is NOT what refuses the
        # second attempt — the already-reversed check is.
        _set_status(c, issue["id"], "in_progress")
        second = _undo(c, event["id"])
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "undo_already_reversed"


def test_undoing_a_close_is_metered_and_gated_like_any_status_write(tmp_path):
    """Re-opening is an ordinary status write performed by the undoing actor, so a
    budget ceiling refuses it exactly as it refuses a direct edit. Undo is not a
    side door around the levers."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue = _issue(c)
        c.post(
            f"/issues/{issue['id']}/contributors",
            json={"user_id": agent["user"]["id"]},
            headers=H1,
        )
        assert (
            _set_status(c, issue["id"], "done", headers=_bearer(agent)).status_code
            == 200
        )
        event = _status_events(c)[0]

        # Give the agent a one-action window and spend it on an ordinary edit.
        assert (
            c.put(
                f"/users/{agent['user']['id']}/budget",
                json={"window": "day", "action_limit": 1},
                headers=H1,
            ).status_code
            == 200
        )
        assert (
            c.patch(
                f"/issues/{issue['id']}",
                json={"title": "spend it"},
                headers=_bearer(agent),
            ).status_code
            == 200
        )

        r = _undo(c, event["id"], headers=_bearer(agent))
        assert r.status_code == 429, r.text
        assert r.json()["code"] == "agent_budget_exhausted"
        # And the refusal is real: the issue stayed closed.
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["status"] == "done"


def test_the_removal_of_a_status_surfaces_as_the_commands_own_refusal(tmp_path):
    """Composition with slice A: removing a status the project no longer has makes
    the inverse unwritable. That must arrive as the issue command's refusal, not as
    a crash."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        c.post(
            f"/projects/{project['id']}/statuses",
            json={"name": "triage", "category": "todo"},
            headers=H1,
        )
        issue = _issue(c, project_id=project["id"])
        _set_status(c, issue["id"], "triage")
        _set_status(c, issue["id"], "in_progress")
        # The 'triage → in_progress' event: its before_status is about to vanish.
        event = _status_events(c)[-1]
        assert event["detail"] == "triage → in_progress"

        assert (
            c.delete(
                f"/projects/{project['id']}/statuses/triage", headers=H1
            ).status_code
            == 200
        )
        r = _undo(c, event["id"])
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "undo_refused_by_command"


def test_undo_is_offered_for_a_status_change_on_the_run_view(tmp_path):
    """The affordance and the engine agree that changed_status is now two-way."""
    from athena.core import undo

    assert undo.reversal_for("changed_status").action_class == undo.TWO_WAY, (
        "register() must have run"
    )
    assert undo.is_undoable({"verb": "changed_status", "imported_at": None})
    # ...and an imported row stays out of reach, as for every other verb.
    assert not undo.is_undoable(
        {"verb": "changed_status", "imported_at": "2020-01-01T00:00:00Z"}
    )
