"""Undo by compensation.

VISION promises the operator can undo what an agent did; ARCHITECTURE promises the
trail is append-only. These pin the only reading that satisfies both: undoing
event N runs the inverse as a NEW audited command whose event points back at N,
history keeps both rows, and authorization is re-evaluated as the undoing actor
rather than inherited from whoever acted first.

The sharp edges are the interesting ones — an undo that would change nothing is a
refusal rather than a cheerful no-op, a second undo of the same event is refused
by a database constraint rather than a read-then-write check, and an event nobody
may see cannot be undone by naming its id.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena.core import activity, db, undo
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="undo.db"):
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


def _agent(client, email="sol@e.com", scopes=("read", "issue:write", "docs:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _event(client, verb, *, headers=H1):
    """The newest event with this verb, as the caller sees it."""
    feed = client.get("/activity", params={"verb": verb}, headers=headers).json()
    return feed[0] if feed else None


def _undo(client, event_id, headers=H1):
    return client.post(f"/activity/{event_id}/undo", headers=headers)


def _issue(client, title="t", headers=H1):
    return client.post("/issues", json={"title": title}, headers=headers).json()


def _space_and_page(client, *, visibility="public"):
    space = client.post("/spaces", json={"key": "S", "name": "S"}, headers=H1).json()
    if visibility != "public":
        client.put(
            f"/spaces/{space['id']}/visibility",
            json={"visibility": visibility},
            headers=H1,
        )
    page = client.post(
        f"/spaces/{space['id']}/pages", json={"title": "P"}, headers=H1
    ).json()
    return space, page


def test_archive_round_trips_and_history_keeps_both_rows(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        assert c.post(f"/issues/{issue['id']}/archive", headers=H1).status_code == 200
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["archived_at"]

        archived = _event(c, "archived")
        undone = _undo(c, archived["id"])
        assert undone.status_code == 201, undone.text
        body = undone.json()
        assert body["reversed_event_id"] == archived["id"]
        assert body["reversal"]["verb"] == "unarchived"
        assert body["reversal"]["reverses_event_id"] == archived["id"]
        # The effect is actually gone.
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["archived_at"] is None

    # Append-only: the original event is untouched and the reversal sits beside it.
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT id, verb, reverses_event_id FROM activity "
        "WHERE verb IN ('archived', 'unarchived') ORDER BY id"
    ).fetchall()
    conn.close()
    assert [(r["verb"], r["reverses_event_id"]) for r in rows] == [
        ("archived", None),
        ("unarchived", archived["id"]),
    ]


def test_label_round_trips_on_issues_and_pages(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        label = c.post("/labels", json={"name": "bug"}, headers=H1).json()
        issue = _issue(c)
        _space, page = _space_and_page(c)

        c.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": label["id"]},
            headers=H1,
        )
        c.post(
            f"/pages/{page['id']}/labels",
            json={"label_id": label["id"]},
            headers=H1,
        )
        # The label event carries the label's NAME; names are unique, so resolving
        # it back to an id is a lookup rather than an interpretation of prose.
        assert _event(c, "labeled")["detail"] == "bug"

        assert _undo(c, _event(c, "labeled")["id"]).status_code == 201
        assert _undo(c, _event(c, "page_labeled")["id"]).status_code == 201
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["labels"] == []
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["labels"] == []

        # And the inverse of the inverse: undoing the removal re-attaches it. Undo
        # is an ordinary forward command, so undoing one is simply a redo.
        assert _undo(c, _event(c, "unlabeled")["id"]).status_code == 201
        assert _undo(c, _event(c, "page_unlabeled")["id"]).status_code == 201
        labels_now = c.get(f"/issues/{issue['id']}", headers=H1).json()["labels"]
        assert [label["name"] for label in labels_now] == ["bug"]
        page_labels = c.get(f"/pages/{page['id']}", headers=H1).json()["labels"]
        assert [label["name"] for label in page_labels] == ["bug"]


def test_page_archive_round_trips(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _space, page = _space_and_page(c)
        c.post(f"/pages/{page['id']}/archive", headers=H1)
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["archived_at"]

        reversal = _undo(c, _event(c, "page_archived")["id"])
        assert reversal.status_code == 201
        assert reversal.json()["reversal"]["verb"] == "page_unarchived"
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["archived_at"] is None


def test_the_restore_direction_reverses_too(tmp_path):
    # Both directions of a two-way pair are inverses; only testing one would leave
    # half the registry unexercised.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        _space, page = _space_and_page(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        c.post(f"/issues/{issue['id']}/unarchive", headers=H1)
        c.post(f"/pages/{page['id']}/archive", headers=H1)
        c.post(f"/pages/{page['id']}/unarchive", headers=H1)

        # Undoing the RESTORE puts each back in the archive.
        assert _undo(c, _event(c, "unarchived")["id"]).status_code == 201
        assert _undo(c, _event(c, "page_unarchived")["id"]).status_code == 201
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["archived_at"]
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["archived_at"]


def test_a_label_event_naming_a_vanished_label_is_refused(tmp_path):
    # The inverse is resolved from the label's unique NAME. If that label is gone,
    # there is no honest way to guess which one was meant.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        label = c.post("/labels", json={"name": "bug"}, headers=H1).json()
        issue = _issue(c)
        _space, page = _space_and_page(c)
        c.post(
            f"/issues/{issue['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        c.post(
            f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        labeled = _event(c, "labeled")["id"]
        page_labeled = _event(c, "page_labeled")["id"]

        conn = db.connect(db_file)
        conn.execute("DELETE FROM labels WHERE id = ?", (label["id"],))
        conn.commit()
        conn.close()

        for event_id in (labeled, page_labeled):
            refused = _undo(c, event_id)
            assert refused.status_code == 409
            assert refused.json()["code"] == "undo_no_effect"
            assert "bug" in refused.json()["detail"]


def test_a_command_refusal_surfaces_as_the_command_s_own_answer(tmp_path):
    # Undo is an ordinary write, so the inverse can be refused on its own terms.
    # Detaching a label nobody attached any more is the command's 404, not the
    # engine's — reported as such rather than flattened into a generic error.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        label = c.post("/labels", json={"name": "bug"}, headers=H1).json()
        _space, page = _space_and_page(c)
        c.post(
            f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        page_labeled = _event(c, "page_labeled")["id"]
        # Someone detaches it by hand first.
        c.delete(f"/pages/{page['id']}/labels/{label['id']}", headers=H1)

        refused = _undo(c, page_labeled)
        assert refused.status_code == 404
        assert refused.json()["code"] == "undo_refused_by_command"
        assert "label not on this page" in refused.json()["detail"]


def test_an_event_can_only_be_undone_once(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        archived = _event(c, "archived")
        assert _undo(c, archived["id"]).status_code == 201

        second = _undo(c, archived["id"])
        assert second.status_code == 409
        assert second.json()["code"] == "undo_already_reversed"

    # The read-then-write check above races; the constraint underneath does not.
    conn = db.connect(db_file)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO activity (actor_id, verb, target_kind, target_id, "
            "reverses_event_id) VALUES (1, 'unarchived', 'issue', ?, ?)",
            (issue["id"], archived["id"]),
        )
    conn.close()


def test_an_undo_that_would_change_nothing_is_refused(tmp_path):
    # The whole point: reporting success for a no-op would tell the operator their
    # world changed when it did not.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        archived = _event(c, "archived")
        # Someone restores it by hand first — the effect is no longer in force.
        c.post(f"/issues/{issue['id']}/unarchive", headers=H1)

        refused = _undo(c, archived["id"])
        assert refused.status_code == 409
        assert refused.json()["code"] == "undo_no_effect"
        # And the refusal left nothing behind: no event on the trail reverses the
        # one it refused to undo.
        feed = c.get("/activity", headers=H1).json()
        assert all(e["reverses_event_id"] != archived["id"] for e in feed)
        assert _event(c, "unarchived")["reverses_event_id"] is None


def test_one_way_and_trapdoor_verbs_are_refused_with_a_reason(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/comments", json={"body": "read me"}, headers=H1)

        refused = _undo(c, _event(c, "commented")["id"])
        assert refused.status_code == 422
        assert refused.json()["code"] == "undo_not_reversible"
        # The refusal names the class and says why, so a surface can explain itself.
        assert "one_way" in refused.json()["detail"]
        assert "delete the comment" in refused.json()["detail"]

        # A verb nobody has classified is honest about that rather than claiming
        # irreversibility it has not established.
        created = _undo(c, _event(c, "created")["id"])
        assert created.status_code == 422
        assert "unclassified" in created.json()["detail"]


def test_imported_history_cannot_be_undone(tmp_path):
    # An imported row describes something that happened in ANOTHER system (0041).
    # Compensating it here would invent a local write nobody made.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        archived = _event(c, "archived")
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE activity SET imported_at = datetime('now') WHERE id = ?",
            (archived["id"],),
        )
        conn.commit()
        conn.close()

        refused = _undo(c, archived["id"])
        assert refused.status_code == 422
        assert refused.json()["code"] == "undo_imported_event"


def test_authorization_is_re_evaluated_not_inherited(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _user(c, "viewer@e.com", role="viewer")
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        archived = _event(c, "archived")

        # A viewer may READ the trail but may not write, so they may not undo —
        # even though the admin who acted plainly could.
        refused = _undo(c, archived["id"], headers={"X-Athena-Actor": "2"})
        assert refused.status_code == 403
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["archived_at"]

        # A token without the scope the inverse needs is refused for the same
        # reason: undo is an ordinary write and obeys every rule ordinary writes do.
        agent = _agent(c, scopes=("read", "docs:write"))
        scoped = _undo(c, archived["id"], headers=_bearer(agent))
        assert scoped.status_code == 403
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["archived_at"]

        # The admin's own undo still works, so the refusals above were about the
        # actor and not about the event.
        assert _undo(c, archived["id"]).status_code == 201


def test_each_layer_demands_its_own_scope(tmp_path):
    # Undoing a page write needs docs:write; undoing an issue write needs
    # issue:write. Holding one is not holding the other, in either direction.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        label = c.post("/labels", json={"name": "bug"}, headers=H1).json()
        docs_only = _bearer(_agent(c, scopes=("read", "docs:write")))
        issues_only = _bearer(
            _agent(c, email="luna@e.com", scopes=("read", "issue:write"))
        )
        # The issue's creator is the agent that will undo it, so the only thing
        # left to refuse the other one is its scope. (Page writes carry no creator
        # lock, so the page half needs no such setup.)
        issue = _issue(c, headers=issues_only)
        _space, page = _space_and_page(c)
        c.post(
            f"/issues/{issue['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        c.post(
            f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        labeled = _event(c, "labeled")["id"]
        page_labeled = _event(c, "page_labeled")["id"]
        assert _undo(c, labeled, headers=docs_only).status_code == 403
        assert _undo(c, page_labeled, headers=issues_only).status_code == 403
        # Each with the right scope succeeds, so the refusals were about the scope.
        assert _undo(c, labeled, headers=issues_only).status_code == 201
        assert _undo(c, page_labeled, headers=docs_only).status_code == 201

        # The re-attaching direction is gated identically — a compensator that
        # checked only on the way out would be a hole on the way back.
        assert (
            _undo(c, _event(c, "unlabeled")["id"], headers=docs_only).status_code == 403
        )
        assert (
            _undo(c, _event(c, "page_unlabeled")["id"], headers=issues_only).status_code
            == 403
        )


def test_undo_cannot_reach_an_event_the_actor_may_not_see(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _space, page = _space_and_page(c, visibility="private")
        c.post(f"/pages/{page['id']}/archive", headers=H1)
        hidden = _event(c, "page_archived")

        # A member outside the private space cannot see the event, so undoing it by
        # id is the SAME 404 a nonexistent id gives — never an oracle.
        outsider = {"X-Athena-Actor": str(_user(c, "out@e.com")["id"])}
        refused = _undo(c, hidden["id"], headers=outsider)
        assert refused.status_code == 404
        assert refused.json()["code"] == "undo_event_not_found"
        assert (
            _undo(c, 999999, headers=outsider).json()["code"] == "undo_event_not_found"
        )
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["archived_at"]


def test_the_page_compensator_carries_its_own_gate(tmp_path):
    # mentor.page_commands deliberately leaves visibility and scope to its transport
    # boundary (mentor/api.py calls _page_for_read and depends on docs_write_actor).
    # Undo is a SECOND boundary onto those same commands, so the compensator applies
    # them itself. The engine's visibility gate would refuse this outsider first, so
    # to prove the compensator is not merely relying on that, it is invoked directly
    # here — a defence that only works when the layer above it happens to fire is
    # not a defence.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _space, page = _space_and_page(c, visibility="private")
        c.post(f"/pages/{page['id']}/archive", headers=H1)
        event = _event(c, "page_archived")
        outsider = _user(c, "out@e.com")

    conn = db.connect(db_file)
    compensator = undo.reversal_for("page_archived").compensator
    assert compensator is not None
    outsider_actor = {"id": outsider["id"], "role": "member", "is_agent": False}
    with pytest.raises(undo.UndoRefused) as hidden:
        compensator(conn, outsider_actor, event)
    assert hidden.value.status_code == 404
    # Anonymous is refused before any page lookup at all.
    with pytest.raises(undo.UndoRefused) as anonymous:
        compensator(conn, None, event)
    assert anonymous.value.status_code == 401
    assert conn.execute(
        "SELECT archived_at FROM pages WHERE id = ?", (page["id"],)
    ).fetchone()["archived_at"]
    conn.close()


def test_the_reversal_joins_the_undoing_actor_s_run(tmp_path):
    # Replay integrity: a reversal is an ordinary event of the run that performed
    # it, so a replay shows the action AND its later reversal.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        archived = _event(c, "archived")

        reversal = _undo(c, archived["id"], headers={**H1, "X-Athena-Run": "cleanup-1"})
        assert reversal.status_code == 201
        assert reversal.json()["reversal"]["run_id"] == "cleanup-1"

        replay = c.get("/activity/runs/cleanup-1/replay", headers=H1).json()
        assert [e["verb"] for e in replay["events"]] == ["unarchived"]
        assert replay["events"][0]["reverses_event_id"] == archived["id"]


def test_mcp_and_the_cockpit_reach_the_same_engine(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        # Sign in for the browser half; the feed's Undo control is session-authed.
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        archived = _event(c, "archived")

        feed = c.get("/aegis/activity")
        assert "Undo" in feed.text

        undone = c.post(
            f"/aegis/activity/{archived['id']}/undo", follow_redirects=False
        )
        assert undone.status_code == 303
        assert "notice=" in undone.headers["location"]
        assert c.get(f"/issues/{issue['id']}", headers=H1).json()["archived_at"] is None

        # An already-undone row offers no button and answers a retry with a notice.
        feed = c.get("/aegis/activity")
        assert "undone" in feed.text
        assert "undoes #" in feed.text
        retry = c.post(f"/aegis/activity/{archived['id']}/undo", follow_redirects=False)
        assert "error=" in retry.headers["location"]


def test_registration_is_idempotent_but_refuses_a_conflicting_inverse(tmp_path):
    # Every test builds its own app in one process, so wiring the same layer twice
    # must be a no-op — while a verb quietly acquiring a SECOND inverse is a bug.
    app, _ = _app(tmp_path)
    create_app(tmp_path / "second.db")
    assert undo.reversal_for("archived").action_class == undo.TWO_WAY
    assert undo.reversal_for("nonexistent_verb").action_class == undo.UNCLASSIFIED

    with pytest.raises(ValueError):
        undo.register("archived", action_class=undo.TRAPDOOR, reason="no")
    with pytest.raises(ValueError):
        # A class and a compensator must agree: only two-way verbs carry one.
        undo.register(
            "whatever", action_class=undo.ONE_WAY, compensator=lambda *a: None
        )
    assert app is not None


def test_undoable_shape_drives_the_affordance_not_the_authority(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        assert undo.is_undoable(_event(c, "archived")) is True
        assert undo.is_undoable(_event(c, "created")) is False
        # An imported row is never offered, whatever its verb.
        assert (
            undo.is_undoable({**_event(c, "archived"), "imported_at": "now"}) is False
        )


def test_reversed_event_ids_answers_a_whole_feed_at_once(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        issue = _issue(c)
        c.post(f"/issues/{issue['id']}/archive", headers=H1)
        archived = _event(c, "archived")
        _undo(c, archived["id"])

    conn = db.connect(db_file)
    assert activity.reversed_event_ids(conn, []) == set()
    assert activity.reversed_event_ids(conn, [archived["id"], 999999]) == {
        archived["id"]
    }
    conn.close()
