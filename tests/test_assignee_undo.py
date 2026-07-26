"""Undoing an assignment.

`assigned` events record only the NEW assignee's display name — names are not
unique, `unassigned` records nothing, and a re-assign (Bob → Cyd) reads
identically to a first assign (nobody → Cyd). So unlike status (whose prior
state migration 0055 had recorded all along), assignment genuinely had nothing
trustworthy to restore, and migration 0068 adds it: typed before/after ids,
written in the same transaction as the event, keyed 1:1 to it, immutable.

The compensator inherits the scalar discipline `test_status_undo.py` pinned: a
still-in-force gate (an assignee has no domain idempotency, so the engine's
no-effect net cannot catch a stale undo), refusal for pre-0068 events rather
than a guess parsed from prose, and authorization owned by the ordinary command
run as the undoing actor.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="assigneeundo.db"):
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


def _issue(client, headers=H1):
    return client.post("/issues", json={"title": "t"}, headers=headers).json()


def _assign(client, issue_id, user_id, headers=H1):
    return client.put(
        f"/issues/{issue_id}/assignee", json={"assignee_id": user_id}, headers=headers
    )


def _events(client, verb, headers=H1):
    """Matching events the caller can see, oldest first."""
    feed = client.get("/activity", params={"verb": verb}, headers=headers)
    return list(reversed(feed.json()))


def _undo(client, event_id, headers=H1):
    return client.post(f"/activity/{event_id}/undo", headers=headers)


def _assignee(client, issue_id):
    return client.get(f"/issues/{issue_id}", headers=H1).json()["assignee_id"]


def test_an_assignment_round_trips_and_both_rows_survive(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        issue = _issue(c)
        assert _assign(c, issue["id"], bob["id"]).status_code == 200

        event = _events(c, "assigned")[0]
        undone = _undo(c, event["id"])
        assert undone.status_code == 201, undone.text
        # The inverse of a first assignment is an unassignment, recorded as the
        # ordinary event that write always records, linked back to what it undoes.
        assert undone.json()["reversal"]["verb"] == "unassigned"
        assert undone.json()["reversal"]["reverses_event_id"] == event["id"]
        assert _assignee(c, issue["id"]) is None

    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT verb, reverses_event_id FROM activity "
        "WHERE verb IN ('assigned', 'unassigned') ORDER BY id"
    ).fetchall()
    conn.close()
    assert [(r["verb"], r["reverses_event_id"]) for r in rows] == [
        ("assigned", None),
        ("unassigned", event["id"]),
    ]


def test_undoing_an_unassignment_restores_the_previous_holder(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        issue = _issue(c)
        _assign(c, issue["id"], bob["id"])
        _assign(c, issue["id"], None)
        assert _assignee(c, issue["id"]) is None

        event = _events(c, "unassigned")[0]
        assert _undo(c, event["id"]).status_code == 201
        # Restored from the recorded id, not from a name in prose: the
        # unassignment event carries no detail at all.
        assert event["detail"] == ""
        assert _assignee(c, issue["id"]) == bob["id"]


def test_undoing_a_superseded_assignment_refuses_instead_of_overwriting(tmp_path):
    """The scalar trap, assignee edition: nobody → Bob → Cyd, then undo the Bob
    event. Writing its before (nobody) back would silently unseat Cyd and stamp
    that as a reversal of an event about Bob."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        cyd = _user(c, "cyd@e.com")
        issue = _issue(c)
        _assign(c, issue["id"], bob["id"])
        first = _events(c, "assigned")[0]
        _assign(c, issue["id"], cyd["id"])

        r = _undo(c, first["id"])
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "undo_no_effect"
        assert _assignee(c, issue["id"]) == cyd["id"]

    # No third row, so nothing claims a reversal happened.
    conn = db.connect(db_file)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE verb IN ('assigned', 'unassigned')"
    ).fetchone()["n"]
    conn.close()
    assert count == 2


def test_undoing_a_reassignment_restores_the_previous_holder(tmp_path):
    """A re-assign's event (detail: 'Cyd') is indistinguishable in prose from a
    first assign — the fact's before_assignee_id is what knows Bob held it."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        cyd = _user(c, "cyd@e.com")
        issue = _issue(c)
        _assign(c, issue["id"], bob["id"])
        _assign(c, issue["id"], cyd["id"])

        latest = _events(c, "assigned")[-1]
        undone = _undo(c, latest["id"])
        assert undone.status_code == 201, undone.text
        assert undone.json()["reversal"]["verb"] == "assigned"
        assert _assignee(c, issue["id"]) == bob["id"]


def test_every_assignment_event_carries_its_fact(tmp_path):
    """The writer invariant: 0068's trigger refuses a fact that cannot bind to
    its exact event, and the recorder runs inside the command's transaction — so
    after any mix of assigns and unassigns, events and facts are 1:1."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        cyd = _user(c, "cyd@e.com")
        issue = _issue(c)
        _assign(c, issue["id"], bob["id"])
        _assign(c, issue["id"], cyd["id"])
        _assign(c, issue["id"], None)

    conn = db.connect(db_file)
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM activity a "
        "WHERE a.verb IN ('assigned', 'unassigned') "
        "AND NOT EXISTS (SELECT 1 FROM issue_assignee_facts f WHERE f.event_id = a.id)"
    ).fetchone()["n"]
    facts = conn.execute(
        "SELECT before_assignee_id, after_assignee_id "
        "FROM issue_assignee_facts ORDER BY event_id"
    ).fetchall()
    conn.close()
    assert orphans == 0
    assert [(f["before_assignee_id"], f["after_assignee_id"]) for f in facts] == [
        (None, bob["id"]),
        (bob["id"], cyd["id"]),
        (cyd["id"], None),
    ]


def test_a_pre_0068_event_is_refused_not_guessed(tmp_path):
    """A legacy event has a display name in its detail and no fact. The name is
    not restorable evidence (it is not unique, and it names the NEW assignee
    anyway), so the undo refuses rather than interprets."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        issue = _issue(c)
        _assign(c, issue["id"], bob["id"])

        conn = db.connect(db_file)
        legacy_id = conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, visibility_restricted) "
            "VALUES (1, 'assigned', 'issue', ?, 'bob', 0)",
            (issue["id"],),
        ).lastrowid
        conn.commit()
        conn.close()

        r = _undo(c, legacy_id)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "undo_not_reversible"
        assert _assignee(c, issue["id"]) == bob["id"]


def test_authorization_is_the_commands_not_the_registrys(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        viewer = _user(c, "v@e.com", role="viewer")
        bob = _user(c, "bob@e.com")
        issue = _issue(c)
        _assign(c, issue["id"], bob["id"])
        event = _events(c, "assigned")[0]

        r = _undo(c, event["id"], headers={"X-Athena-Actor": str(viewer["id"])})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "undo_refused_by_command"
        assert _assignee(c, issue["id"]) == bob["id"]


def test_the_same_assignment_cannot_be_undone_twice(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        issue = _issue(c)
        _assign(c, issue["id"], bob["id"])
        event = _events(c, "assigned")[0]

        assert _undo(c, event["id"]).status_code == 201
        # Put the world back by hand so staleness is not what refuses the retry.
        _assign(c, issue["id"], bob["id"])
        second = _undo(c, event["id"])
        assert second.status_code == 409
        assert second.json()["code"] == "undo_already_reversed"


def test_the_affordance_agrees_assignment_is_two_way(tmp_path):
    from athena.core import undo

    assert undo.reversal_for("assigned").action_class == undo.TWO_WAY
    assert undo.reversal_for("unassigned").action_class == undo.TWO_WAY
    assert not undo.is_undoable(
        {"verb": "assigned", "imported_at": "2020-01-01T00:00:00Z"}
    )
