"""Admin agents page redirects carry their error and notice text as one encoded query value.

The web handlers send operators back to /admin/agents?error=... or ?notice=... with
the text placed in the query string. Unencoded, text containing & # ? or = could add
a query key or a fragment. These tests drive real handlers with a message that
contains those characters, and a positive control shows an ordinary message still
round-trips to the page unchanged.
"""

from html import escape
from urllib.parse import parse_qsl, urlsplit

from fastapi.testclient import TestClient

from athena.core import agent_commands, worker_commands
from athena.core.worker_commands import WorkerCommandError
from athena.main import create_app

HOSTILE = "x&decided=approve#frag"


def _app(tmp_path):
    return create_app(tmp_path / "redirects.db")


def _bootstrap_admin(client):
    r = client.post(
        "/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}
    )
    assert r.status_code == 201, r.text


def _login(client):
    r = client.post(
        "/login",
        data={"email": "admin@e.com", "password": "pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _location_parts(response, agent_id=None):
    """The redirect's query. An action made on an agent also names that agent
    and returns to its section (U10): the id is the server's own number, so
    it is the one fragment a redirect may carry."""
    assert response.status_code == 303, response.text
    parts = urlsplit(response.headers["location"])
    assert parts.path == "/admin/agents"
    assert parts.fragment == ("" if agent_id is None else f"agent-{agent_id}")
    return parse_qsl(parts.query, keep_blank_values=True)


def test_error_text_with_query_and_fragment_characters_stays_one_value(
    tmp_path, monkeypatch
):
    def refuse(*_args, **_kwargs):
        raise agent_commands.AgentCommandError("conflict", HOSTILE)

    monkeypatch.setattr(agent_commands, "set_user_paused", refuse)
    app = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)

        r = client.post("/admin/agents/2/pause", follow_redirects=False)
        assert _location_parts(r, 2) == [("error", HOSTILE), ("agent", "2")]

        page = client.get(r.headers["location"])
        assert page.status_code == 200
        assert escape(HOSTILE) in page.text


def test_worker_error_detail_with_query_characters_stays_one_value(
    tmp_path, monkeypatch
):
    def refuse(*_args, **_kwargs):
        raise WorkerCommandError("invalid", HOSTILE)

    monkeypatch.setattr(worker_commands, "request_kill", refuse)
    app = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)

        r = client.post("/admin/workers/7/kill", follow_redirects=False)
        assert _location_parts(r) == [("error", HOSTILE)]


def test_ordinary_error_text_still_round_trips_and_displays(tmp_path, monkeypatch):
    ordinary = "Agent is busy; try again after the current run."

    def refuse(*_args, **_kwargs):
        raise agent_commands.AgentCommandError("conflict", ordinary)

    monkeypatch.setattr(agent_commands, "set_user_paused", refuse)
    app = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)

        r = client.post("/admin/agents/2/pause", follow_redirects=False)
        assert _location_parts(r, 2) == [("error", ordinary), ("agent", "2")]

        page = client.get(r.headers["location"])
        assert page.status_code == 200
        assert escape(ordinary) in page.text


def test_notice_text_with_punctuation_stays_one_value(tmp_path, monkeypatch):
    # The worker-kill success notice contains ";" and "." and names the worker,
    # whose label is the worker's own text. None of it may split the value.
    asked = {"agent_id": 2, "node_label": "lab&top#1", "worker_key": "w-1"}
    monkeypatch.setattr(worker_commands, "request_kill", lambda *a, **k: asked)
    notice = "lab&top#1 asked to stop; it will be told on its next heartbeat."
    app = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _login(client)

        r = client.post("/admin/workers/7/kill", follow_redirects=False)
        assert _location_parts(r, 2) == [("notice", notice), ("agent", "2")]

        page = client.get(r.headers["location"])
        assert page.status_code == 200
        assert escape(notice) in page.text
