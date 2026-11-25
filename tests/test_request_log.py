"""One log line per request, and an id on every response (H15).

WHY (OPS-7): the log could not answer what an agent did and what failed. The
access line had no time, request id, account or duration; a 500 carried no
id to find it by and printed its traceback twice; a failed webhook delivery
was not logged at all.

Now every request leaves one line on ``athena.request`` (id, actor, method,
route template, status, refusal code, duration), every response carries
``X-Request-Id``, an unhandled error is logged once with that id, and a
failed delivery is logged once.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re

import pytest
from fastapi.testclient import TestClient
import uvicorn

from support import ADMIN, bearer, bootstrap, login, make_app, onboard_agent

from athena import ops, request_log
from athena.core import activity, db, webhooks
from athena.main import create_app

LINE = re.compile(
    r"request id=(?P<id>\S+) actor=(?P<actor>\S+) method=(?P<method>\S+) "
    r"route=(?P<route>\S+) status=(?P<status>\S+) code=(?P<code>\S+) "
    r"ms=(?P<ms>\d+\.\d)"
)


def _requests(caplog) -> list[dict[str, str]]:
    """Every request line logged so far, parsed; anything else on the logger
    fails the test."""
    lines = []
    for record in caplog.records:
        if record.name != "athena.request" or record.levelno != logging.INFO:
            continue
        match = LINE.fullmatch(record.getMessage())
        assert match is not None, record.getMessage()
        lines.append(match.groupdict())
    return lines


@pytest.fixture
def served(tmp_path, caplog):
    app, db_file = make_app(tmp_path)
    caplog.set_level(logging.INFO, logger="athena")
    with TestClient(app, raise_server_exceptions=False) as client:
        bootstrap(client)
        caplog.clear()
        yield app, client, db_file


def _only(caplog) -> dict[str, str]:
    (line,) = _requests(caplog)
    return line


# --- the line ---------------------------------------------------------------------


def test_one_line_names_the_request_and_its_id_is_the_responses_header(served, caplog):
    _app, client, _db = served

    response = client.get("/healthz")

    line = _only(caplog)
    assert re.fullmatch(r"[0-9a-f]{16}", line["id"])
    assert response.headers["x-request-id"] == line["id"]
    assert line | {"id": "", "ms": ""} == {
        "id": "",
        "actor": "-",
        "method": "GET",
        "route": "/healthz",
        "status": "200",
        "code": "-",
        "ms": "",
    }
    # Each request gets its own.
    assert client.get("/healthz").headers["x-request-id"] != line["id"]


def test_the_route_is_its_template_and_never_the_path_or_the_query(served, caplog):
    _app, client, _db = served
    project = client.post("/projects", json={"name": "P", "key": "PRJ"}, headers=ADMIN)
    issue = client.post(
        "/issues",
        json={"title": "A secret title", "project_id": project.json()["id"]},
        headers=ADMIN,
    ).json()
    caplog.clear()

    assert (
        client.get(f"/issues/{issue['id']}?note=secret-term", headers=ADMIN).status_code
        == 200
    )
    assert client.get("/no/such/place-secret-term").status_code == 404

    found, missing = _requests(caplog)
    assert found["route"] == "/issues/{ref}"
    assert (missing["route"], missing["status"], missing["code"]) == (
        "-",
        "404",
        "not_found",
    )
    assert "secret" not in caplog.text


def test_the_actor_is_named_however_the_request_authenticated(served, caplog):
    app, client, _db = served
    agent = onboard_agent(client)
    browser = TestClient(app)
    login(browser, "a@e.com")
    caplog.clear()

    client.get("/users/me", headers=ADMIN)  # the trusted actor header
    client.get("/users/me", headers=bearer(agent))  # a bearer token
    browser.get("/issues")  # a browser session
    client.get("/users/me")  # nobody

    by_header, by_token, by_session, nobody = _requests(caplog)
    assert by_header["actor"] == "1"
    assert by_token["actor"] == str(agent["user"]["id"])
    assert (by_session["actor"], by_session["status"]) == ("1", "200")
    assert (nobody["actor"], nobody["status"], nobody["code"]) == (
        "-",
        "401",
        "unauthenticated",
    )


def test_a_refusal_names_its_code_and_the_account_refused(served, caplog):
    _app, client, _db = served
    agent = onboard_agent(client, scopes=("read",))
    caplog.clear()

    refused = client.post("/projects", json={"name": "P"}, headers=bearer(agent))

    line = _only(caplog)
    assert refused.status_code == 403
    assert (line["actor"], line["status"], line["code"]) == (
        str(agent["user"]["id"]),
        "403",
        refused.json()["code"],
    )
    assert refused.headers["x-request-id"] == line["id"]


def test_a_request_the_boundary_refuses_is_named_too(tmp_path, caplog):
    app = create_app(
        tmp_path / "athena.db",
        network_mode="local",
        allowed_authorities=("localhost:8000",),
    )
    caplog.set_level(logging.INFO, logger="athena")
    with TestClient(app, base_url="http://elsewhere.example:8000") as client:
        caplog.clear()
        refused = client.get("/healthz")

    line = _only(caplog)
    assert refused.status_code >= 400
    assert line["status"] == str(refused.status_code)
    assert refused.headers["x-request-id"] == line["id"]


# --- the id -----------------------------------------------------------------------


def test_a_callers_own_id_is_kept(served, caplog):
    _app, client, _db = served
    own = "run-42.step_7-" + "a" * 50

    response = client.get("/healthz", headers={"X-Request-Id": own})

    assert len(own) == 64
    assert response.headers["x-request-id"] == own
    assert _only(caplog)["id"] == own


@pytest.mark.parametrize(
    "unusable",
    ["", "two words", "a" * 65, "id=1 status=200", "tab\tbed", "café", "a/b"],
)
def test_an_id_the_log_cannot_carry_is_replaced(served, caplog, unusable):
    _app, client, _db = served

    response = client.get(
        "/healthz", headers={"X-Request-Id": unusable.encode("latin-1")}
    )

    line = _only(caplog)
    assert re.fullmatch(r"[0-9a-f]{16}", line["id"])
    assert response.headers["x-request-id"] == line["id"]
    assert response.headers.get_list("x-request-id") == [line["id"]]


def test_an_id_a_route_set_itself_is_not_sent_twice(served, caplog):
    app, client, _db = served

    @app.get("/sets-its-own")
    def sets_its_own():
        from fastapi.responses import JSONResponse

        return JSONResponse({}, headers={"X-Request-Id": "the-routes"})

    response = client.get("/sets-its-own")

    assert response.headers.get_list("x-request-id") == [_only(caplog)["id"]]


# --- the refusal code -------------------------------------------------------------


def test_an_error_body_that_is_not_a_refusal_has_no_code(served, caplog):
    app, client, _db = served
    from fastapi.responses import JSONResponse, PlainTextResponse

    @app.get("/plain")
    def plain():
        return PlainTextResponse("gone away", status_code=410)

    @app.get("/list")
    def as_list():
        return JSONResponse(["not", "an", "object"], status_code=409)

    @app.get("/huge")
    def huge():
        return JSONResponse({"code": "too_big", "pad": "x" * 70_000}, status_code=409)

    @app.get("/number")
    def number():
        return JSONResponse({"code": 7}, status_code=409)

    @app.get("/words")
    def words():
        return JSONResponse({"code": "two words status=200"}, status_code=409)

    @app.get("/fine")
    def fine():
        # A 2xx body's own "code" field is not a refusal.
        return JSONResponse({"code": "ok_not_a_refusal"})

    for path in ("/plain", "/list", "/huge", "/number", "/words", "/fine"):
        client.get(path)

    assert [(line["status"], line["code"]) for line in _requests(caplog)] == [
        ("410", "-"),
        ("409", "-"),
        ("409", "-"),
        ("409", "-"),
        ("409", "-"),
        ("200", "-"),
    ]


# --- an unhandled error -----------------------------------------------------------


def test_an_unhandled_error_is_logged_once_with_the_id_of_its_500(served, caplog):
    app, client, _db = served

    @app.get("/boom/{thing_id}")
    def boom(thing_id: int):
        raise RuntimeError("kaboom")

    response = client.get("/boom/7")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    line = _only(caplog)
    assert (line["status"], line["code"], line["route"]) == (
        "500",
        "internal_error",
        "/boom/{thing_id}",
    )
    assert response.headers["x-request-id"] == line["id"]
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    (error,) = errors
    text = error.getMessage()
    assert text.startswith(
        f"unhandled error id={line['id']} method=GET route=/boom/{{thing_id}}\n"
    )
    # One traceback, of the route's own error: not the task group's as well.
    assert text.count("Traceback (most recent call last)") == 1
    assert text.count("RuntimeError: kaboom") == 1
    assert "ExceptionGroup" not in text
    assert error.exc_info is None


def test_an_error_still_reaches_a_test_that_wants_it(tmp_path):
    app, _db = make_app(tmp_path)

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    with TestClient(app) as client, pytest.raises(RuntimeError, match="kaboom"):
        client.get("/boom")


def test_a_cause_the_code_named_is_kept_in_the_one_traceback():
    try:
        try:
            raise KeyError("the cause")
        except KeyError as cause:
            raise RuntimeError("the error") from cause
    except RuntimeError as exc:
        text = request_log.format_error(exc)

    assert "KeyError: 'the cause'" in text
    assert "RuntimeError: the error" in text


def test_an_error_after_the_answer_began_sends_no_second_answer(served, caplog):
    app, client, _db = served
    from fastapi.responses import StreamingResponse

    def half_a_body():
        yield b"half"
        raise RuntimeError("broke mid-stream")

    @app.get("/half")
    def half():
        return StreamingResponse(half_a_body(), media_type="text/plain")

    response = client.get("/half")

    # The 200 had already gone out; the error is still logged, once.
    assert (response.status_code, response.text) == (200, "half")
    assert _only(caplog)["status"] == "200"
    (error,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "RuntimeError: broke mid-stream" in error.getMessage()


def test_the_server_drops_its_own_copy_of_an_error_athena_logged(tmp_path, monkeypatch):
    server_log = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(server_log, "filters", [])
    monkeypatch.setattr(uvicorn.Server, "run", lambda server, sockets=None: None)

    with pytest.raises(SystemExit):
        ops._serve_bound(create_app(tmp_path / "athena.db"), host="127.0.0.1", port=1)

    def passes(message: str) -> bool:
        record = logging.LogRecord("uvicorn.error", 40, "", 0, message, None, None)
        return bool(server_log.filter(record))

    assert not passes("Exception in ASGI application\n")
    assert passes("[Errno 98] error while attempting to bind on address")


# --- a failed delivery ------------------------------------------------------------


def test_a_failed_delivery_is_logged_once_without_its_url(tmp_path, caplog):
    conn = db.connect(tmp_path / "athena.db")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name, role) VALUES ('a@e.com','A','admin')")
    conn.commit()
    hook = webhooks.create_webhook(
        conn,
        url="https://93.184.216.34/hook?secret=in-the-url",
        created_by=1,
        start_cursor=0,
        commit=True,
    )
    events = [
        activity.record(
            conn,
            actor_id=1,
            verb="created",
            target_kind="issue",
            target_id=number,
            commit=True,
        )
        for number in (1, 2)
    ]
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    caplog.set_level(logging.INFO, logger="athena")

    def refuses(url, body, headers):
        return False, "http 503\nfrom the receiver"

    assert webhooks.deliver_pending(conn, poster=refuses, now=now) == 0
    conn.close()

    # One line for the one attempt: the second event waits behind the first.
    (record,) = [r for r in caplog.records if r.name == "athena.core.webhooks"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"webhook delivery failed: webhook={hook['id']} event={events[0]['id']} "
        "failures=1 error=http 503 from the receiver"
    )
    assert "in-the-url" not in caplog.text


def test_a_delivery_that_succeeds_logs_nothing(tmp_path, caplog):
    conn = db.connect(tmp_path / "athena.db")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name, role) VALUES ('a@e.com','A','admin')")
    conn.commit()
    webhooks.create_webhook(
        conn,
        url="https://93.184.216.34/hook",
        created_by=1,
        start_cursor=0,
        commit=True,
    )
    activity.record(
        conn, actor_id=1, verb="created", target_kind="issue", target_id=1, commit=True
    )
    caplog.set_level(logging.INFO, logger="athena")

    delivered = webhooks.deliver_pending(
        conn,
        poster=lambda url, body, headers: (True, None),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    conn.close()

    assert delivered == 1
    assert [r for r in caplog.records if r.name == "athena.core.webhooks"] == []
