"""An agent's notebook: one Mentor page per agent, shown on its desk.

An agent that restarts loses what it learned in the last session unless it wrote
it somewhere it will read next time. The notebook is that somewhere: one page per
agent, in a private space the agent owns, so only that agent and the operator (an
admin) can read it. It is created on first use by an explicit call
(``POST /desk/notebook``), never by reading the desk. The desk's identity lane
carries the page id, its last-edited time and the first ``NOTEBOOK_EXCERPT_LINES``
lines.

The agent appends through the ordinary page edit with ``If-Match``, so two
sessions of one seat cannot clobber each other: the second edit against the old
tag is refused, and the page's version history keeps every revision. Nothing
summarises it automatically, and it is not a runbook.
"""

from fastapi.testclient import TestClient

from athena.aegis import desk
from athena.core import db, users
from athena.mentor import notebook_commands
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="notebook.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", name="Sol"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "docs:write"]},
        headers=H1,
    ).json()


def _bearer(agent):
    return {"Authorization": f"Bearer {agent['token']['token']}"}


def _open(client, agent):
    response = client.post("/desk/notebook", headers=_bearer(agent))
    assert response.status_code in (200, 201), response.text
    return response


def _notebook_lane(client, agent):
    return client.get("/desk", headers=_bearer(agent)).json()["identity"]["notebook"]


# ── created once ────────────────────────────────────────────────────────────


def test_the_notebook_is_created_once_on_first_use(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        assert _notebook_lane(c, agent) is None  # not created by reading
        first = _open(c, agent)
        assert first.status_code == 201 and first.json()["created"] is True
        again = _open(c, agent)
        assert again.status_code == 200 and again.json()["created"] is False
        assert again.json()["page"]["id"] == first.json()["page"]["id"]
        page = first.json()["page"]
        assert notebook_commands.NOTEBOOK_LABEL in [l["name"] for l in page["labels"]]
        conn = db.connect(db_file)
        spaces = conn.execute(
            "SELECT COUNT(*) AS n FROM spaces WHERE created_by = ?",
            (agent["user"]["id"],),
        ).fetchone()["n"]
        pages = conn.execute(
            "SELECT COUNT(*) AS n FROM pages WHERE created_by = ?",
            (agent["user"]["id"],),
        ).fetchone()["n"]
        visibility = conn.execute(
            "SELECT visibility FROM spaces WHERE id = ?", (page["space_id"],)
        ).fetchone()["visibility"]
        conn.close()
        assert (spaces, pages, visibility) == (1, 1, "private")
        # Positive control: a second agent gets its own, separate notebook.
        other = _agent(c, "two@e.com", "Two")
        assert _open(c, other).json()["page"]["id"] != page["id"]


def test_a_taken_space_key_does_not_block_the_notebook(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        key = notebook_commands.notebook_space_key(agent["user"]["id"])
        c.post("/spaces", json={"key": key, "name": "Someone else's"}, headers=H1)
        opened = _open(c, agent).json()
        assert opened["created"] is True
        space = c.get(
            f"/spaces/{opened['page']['space_id']}", headers=_bearer(agent)
        ).json()
        assert space["key"] != key and space["created_by"] == agent["user"]["id"]


def test_only_an_agent_seat_opens_a_notebook(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        refused = c.post("/desk/notebook", headers=H1)
        assert refused.status_code == 403
        # Positive control: an agent is served.
        assert _open(c, _agent(c)).status_code == 201


# ── the desk lane: bounded, and reading writes nothing ──────────────────────


def test_the_desk_excerpt_is_bounded(tmp_path):
    app, _ = _app(tmp_path)
    n = notebook_commands.NOTEBOOK_EXCERPT_LINES
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        page = _open(c, agent).json()["page"]
        tag = c.get(f"/pages/{page['id']}", headers=_bearer(agent)).headers["etag"]
        body = "\n".join(f"line {i}" for i in range(n + 25))
        edited = c.patch(
            f"/pages/{page['id']}",
            json={"body": body},
            headers={**_bearer(agent), "If-Match": tag},
        )
        assert edited.status_code == 200, edited.text
        lane = _notebook_lane(c, agent)
        assert lane["page_id"] == page["id"]
        assert lane["excerpt"].splitlines() == [f"line {i}" for i in range(n)]
        assert lane["excerpt_truncated"] is True
        assert lane["total_lines"] == n + 25
        assert lane["updated_at"] is not None
        # Positive control: a short notebook is shown whole and not marked truncated.
        tag = c.get(f"/pages/{page['id']}", headers=_bearer(agent)).headers["etag"]
        c.patch(
            f"/pages/{page['id']}",
            json={"body": "only\ntwo"},
            headers={**_bearer(agent), "If-Match": tag},
        )
        short = _notebook_lane(c, agent)
        assert short["excerpt"] == "only\ntwo" and short["excerpt_truncated"] is False


def test_the_desk_read_writes_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        conn = db.connect(db_file)
        me = users.get_user(conn, agent["user"]["id"])
        before = conn.total_changes
        assert desk.build_desk(conn, actor=me)["identity"]["notebook"] is None
        assert conn.total_changes == before  # no notebook appears by reading
        conn.close()
        _open(c, agent)
        conn = db.connect(db_file)
        before = conn.total_changes
        assert desk.build_desk(conn, actor=me)["identity"]["notebook"] is not None
        assert conn.total_changes == before
        conn.close()


# ── visibility ──────────────────────────────────────────────────────────────


def test_another_agent_cannot_read_it_and_the_operator_can(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        owner = _agent(c)
        other = _agent(c, "two@e.com", "Two")
        page = _open(c, owner).json()["page"]
        assert c.get(f"/pages/{page['id']}", headers=_bearer(other)).status_code == 404
        assert (
            c.get(f"/pages/{page['id']}/versions", headers=_bearer(other)).status_code
            == 404
        )
        assert (
            c.get(f"/spaces/{page['space_id']}", headers=_bearer(other)).status_code
            == 404
        )
        # Positive controls: the owner and the operator (an admin) can read it.
        assert c.get(f"/pages/{page['id']}", headers=_bearer(owner)).status_code == 200
        assert c.get(f"/pages/{page['id']}", headers=H1).status_code == 200


# ── appending: the ETag guards, the versions keep history ───────────────────


def test_a_stale_etag_edit_is_refused(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        page = _open(c, agent).json()["page"]
        # Two sessions of one seat read the same revision.
        tag = c.get(f"/pages/{page['id']}", headers=_bearer(agent)).headers["etag"]
        first = c.patch(
            f"/pages/{page['id']}",
            json={"body": "session one learned X"},
            headers={**_bearer(agent), "If-Match": tag},
        )
        assert first.status_code == 200
        second = c.patch(
            f"/pages/{page['id']}",
            json={"body": "session two learned Y"},
            headers={**_bearer(agent), "If-Match": tag},
        )
        assert second.status_code == 412
        body = c.get(f"/pages/{page['id']}", headers=_bearer(agent)).json()["body"]
        assert body == "session one learned X"  # not clobbered
        # Positive control: the second session re-reads, merges, and appends.
        fresh = c.get(f"/pages/{page['id']}", headers=_bearer(agent)).headers["etag"]
        merged = c.patch(
            f"/pages/{page['id']}",
            json={"body": "session one learned X\nsession two learned Y"},
            headers={**_bearer(agent), "If-Match": fresh},
        )
        assert merged.status_code == 200


def test_page_versions_keep_history(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        page = _open(c, agent).json()["page"]
        bodies = ["first entry", "first entry\nsecond entry"]
        for body in bodies:
            tag = c.get(f"/pages/{page['id']}", headers=_bearer(agent)).headers["etag"]
            assert (
                c.patch(
                    f"/pages/{page['id']}",
                    json={"body": body},
                    headers={**_bearer(agent), "If-Match": tag},
                ).status_code
                == 200
            )
        versions = c.get(f"/pages/{page['id']}/versions", headers=_bearer(agent)).json()
        kept = [v["body"] for v in versions]
        # Every earlier revision is kept, including the one before the first entry.
        assert "first entry" in kept
        assert len(versions) >= 2
        # Positive control: the live page is the newest body, not a version.
        assert (
            c.get(f"/pages/{page['id']}", headers=_bearer(agent)).json()["body"]
            == bodies[-1]
        )


def test_nothing_summarises_and_it_is_not_a_runbook(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        page = _open(c, agent).json()["page"]
        # Reading the desk many times leaves the body exactly as written.
        for _ in range(3):
            c.get("/desk", headers=_bearer(agent))
        assert (
            c.get(f"/pages/{page['id']}", headers=_bearer(agent)).json()["body"]
            == page["body"]
        )
        conn = db.connect(db_file)
        runbooks = conn.execute(
            "SELECT COUNT(*) AS n FROM issue_runbooks WHERE page_id = ?", (page["id"],)
        ).fetchone()["n"]
        conn.close()
        assert runbooks == 0
