"""Per-project status configuration as a command.

Adding or removing a status changes a project's issue lifecycle — what "closed"
means for its board, its dependencies, and its blocked-close policy. Both writes
used to take no actor, open their own commit, and record nothing, so the
append-only trail could not answer who changed the vocabulary. These pin the
migration: the write and its audit event are one transaction, the authorization
is evaluated from live rows inside that transaction, and the refusals REST
already published keep their status codes.

Two are regression tests rather than migration tests. A status name differing
only in case used to pass Python's ``==`` validation and then die on 0024's
``COLLATE NOCASE`` UNIQUE constraint — an unhandled IntegrityError, i.e. a 500.
And the in-use guard counts archived issues while the edit page's hint did not,
so the page offered a Remove button the write then refused.
"""

import sqlite3

from fastapi.testclient import TestClient

from athena.aegis import status_commands, statuses
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="statuscfg.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _login(client, email="a@e.com"):
    """Sign in for the browser surface, and carry the CSRF token the forms need."""
    client.post("/login", data={"email": email, "password": "pw"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


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


def _add(client, project_id, name, category="todo", headers=H1):
    return client.post(
        f"/projects/{project_id}/statuses",
        json={"name": name, "category": category},
        headers=headers,
    )


def _remove(client, project_id, name, headers=H1):
    return client.delete(f"/projects/{project_id}/statuses/{name}", headers=headers)


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, verb, target_kind, target_id, detail FROM activity "
        "WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_adding_a_status_records_who_did_it(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        r = _add(c, project["id"], "shipped", "done")
        assert r.status_code == 201, r.text
        assert [s["name"] for s in r.json()] == [
            "open",
            "in_progress",
            "done",
            "shipped",
        ]

    events = _events(db_file, status_commands.VERB_STATUS_ADDED)
    assert len(events) == 1
    assert events[0]["actor_id"] == 1
    assert events[0]["target_kind"] == "project"
    assert events[0]["target_id"] == project["id"]
    # The detail names the status AND its category, because the category is what
    # actually changes the lifecycle's meaning.
    assert events[0]["detail"] == "shipped (done)"


def test_removing_a_status_records_the_name_the_row_no_longer_has(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        _add(c, project["id"], "shipped", "done")
        r = _remove(c, project["id"], "shipped")
        assert r.status_code == 200, r.text
        assert "shipped" not in [s["name"] for s in r.json()]

    events = _events(db_file, status_commands.VERB_STATUS_REMOVED)
    assert len(events) == 1
    # Captured before the DELETE: the event outlives the row it names, which is the
    # entire reason a removal is worth auditing.
    assert events[0]["detail"] == "shipped"


def test_a_case_differing_duplicate_is_a_conflict_not_a_crash(tmp_path):
    """0024 declares UNIQUE (project_id, name COLLATE NOCASE); is_valid compared
    with Python ==. "Open" therefore passed validation and hit the constraint as an
    unhandled IntegrityError — a 500 on both transports."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        r = _add(c, project["id"], "Open", "todo")
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == statuses.REASON_DUPLICATE
        # And nothing was written on the way to the refusal.
        listed = c.get(f"/projects/{project['id']}/statuses", headers=H1).json()
        assert [s["name"] for s in listed] == ["open", "in_progress", "done"]


def test_the_exact_status_validator_stays_case_sensitive(tmp_path):
    """The NOCASE probe is deliberately local to the add write.

    Making ``statuses.is_valid`` case-insensitive would let "Open" pass an ISSUE
    status write and then fall off ``category_of``, which still compares exactly —
    turning a 500 at configuration time into a 500 inside an issue's transaction.
    """
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
    conn = db.connect(db_file)
    assert statuses.is_valid(conn, project["id"], "open") is True
    assert statuses.is_valid(conn, project["id"], "Open") is False
    assert statuses.category_of(conn, project["id"], "Open") is None
    conn.close()


def test_every_refusal_keeps_the_status_code_rest_already_published(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)

        assert _add(c, project["id"], "   ", "todo").status_code == 422
        assert _add(c, project["id"], "x", "nonsense").status_code == 422
        assert _add(c, project["id"], "open", "todo").status_code == 409
        assert _remove(c, project["id"], "nope").status_code == 404

        # An issue sitting in a status pins it in place.
        issue = c.post(
            "/issues", json={"title": "t", "project_id": project["id"]}, headers=H1
        ).json()
        assert issue["status"] == "open"
        assert _remove(c, project["id"], "open").status_code == 409

        # And a project may never be left with no lifecycle at all.
        c.patch(f"/issues/{issue['id']}", json={"status": "done"}, headers=H1)
        assert _remove(c, project["id"], "open").status_code == 200
        assert _remove(c, project["id"], "in_progress").status_code == 200
        assert _remove(c, project["id"], "done").status_code == 409


def test_an_archived_issue_still_pins_its_status(tmp_path):
    """The guard counts archived issues because an archived issue can be RESTORED,
    and restoring one into a status the project no longer has would leave a row
    that fails is_valid and so cannot be written by any issue command."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        project = _project(c)
        _add(c, project["id"], "shipped", "done")
        issue = c.post(
            "/issues", json={"title": "t", "project_id": project["id"]}, headers=H1
        ).json()
        c.patch(f"/issues/{issue['id']}", json={"status": "shipped"}, headers=H1)
        assert c.post(f"/issues/{issue['id']}/archive", headers=H1).status_code == 200

        r = _remove(c, project["id"], "shipped")
        assert r.status_code == 409
        assert r.json()["detail"] == statuses.REASON_IN_USE

        # The edit page's hint has to agree with the guard, or it offers a Remove
        # button the write then refuses — which reads as a bug rather than as the
        # guard doing its job. The page counts the archived issue too.
        page = c.get(f"/aegis/projects/{project['id']}/edit")
        assert page.status_code == 200, page.text
        assert "1 issue" in page.text
        # No Remove button for a status the command would refuse to remove.
        assert f"/projects/{project['id']}/statuses/shipped/delete" not in page.text
        # An unused status still offers one, so the absence above is the archived
        # issue being counted rather than the whole control disappearing.
        assert f"/projects/{project['id']}/statuses/done/delete" in page.text


def test_a_non_creator_is_refused_and_a_hidden_project_is_never_confirmed(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        other = _user(c, "b@e.com")
        h_other = {"X-Athena-Actor": str(other["id"])}

        public = _project(c, "Public", "PUB")
        # Visible, but not theirs: 403.
        assert _add(c, public["id"], "x", "todo", headers=h_other).status_code == 403

        private = _project(c, "Private", "PRV")
        c.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        # Invisible: 404, indistinguishable from a project that does not exist, so
        # the write path is not an existence oracle for names the reads hide.
        assert _add(c, private["id"], "x", "todo", headers=h_other).status_code == 404
        assert _add(c, 9999, "x", "todo", headers=h_other).status_code == 404


def test_a_viewer_and_a_scopeless_token_are_both_refused(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        viewer = _user(c, "v@e.com", role="viewer")
        assert (
            _add(
                c,
                project["id"],
                "x",
                "todo",
                headers={"X-Athena-Actor": str(viewer["id"])},
            ).status_code
            == 403
        )

        # A read-only token belonging to an actor who otherwise could write.
        onboarded = _agent(c, scopes=("read",))
        assert (
            _add(c, project["id"], "x", "todo", headers=_bearer(onboarded)).status_code
            == 403
        )


def test_a_credential_revoked_after_the_request_cannot_still_write(tmp_path):
    """The transport authorized before the write lock; the command re-resolves the
    actor from live rows inside it. A pause landing in between must refuse."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        agent = _agent(c)
        # Hand the project to the agent so ownership is not what refuses it.
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE projects SET created_by = ? WHERE id = ?",
            (agent["user"]["id"], project["id"]),
        )
        conn.commit()
        conn.close()
        assert (
            _add(c, project["id"], "triage", "todo", headers=_bearer(agent)).status_code
            == 201
        )

        c.put(
            f"/users/{agent['user']['id']}/paused",
            json={"paused": True},
            headers=H1,
        )
        r = _add(c, project["id"], "later", "todo", headers=_bearer(agent))
        assert r.status_code in (403, 423), r.text


def test_a_failed_write_leaves_neither_the_row_nor_an_event(tmp_path):
    """The row and its event are one transaction. Force the audit insert to fail and
    the status must not survive — otherwise a crash could leave a permanently
    unaudited lifecycle change, which is the whole defect being migrated away."""
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)

    conn = db.connect(db_file)
    actor = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
    conn.execute(
        "CREATE TRIGGER refuse_status_events BEFORE INSERT ON activity "
        "WHEN NEW.verb = 'project_status_added' "
        "BEGIN SELECT RAISE(ABORT, 'no'); END"
    )
    conn.commit()
    try:
        status_commands.add_status(
            conn, actor=actor, project_id=project["id"], name="shipped", category="done"
        )
        raise AssertionError("the audit failure should have propagated")
    except sqlite3.IntegrityError:
        pass
    conn.execute("DROP TRIGGER refuse_status_events")
    conn.commit()
    assert statuses.is_valid(conn, project["id"], "shipped") is False
    conn.close()


def test_the_browser_form_and_rest_share_the_command(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        project = _project(c)

        r = c.post(
            f"/aegis/projects/{project['id']}/statuses",
            data={"name": "shipped", "category": "done"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

        # Same refusal, same reason, through the form.
        dup = c.post(
            f"/aegis/projects/{project['id']}/statuses",
            data={"name": "SHIPPED", "category": "done"},
        )
        assert dup.status_code == 400
        assert statuses.REASON_DUPLICATE in dup.text

    # The browser write is audited exactly like the REST one.
    events = _events(db_file, status_commands.VERB_STATUS_ADDED)
    assert [e["detail"] for e in events] == ["shipped (done)"]
