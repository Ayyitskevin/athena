"""Slow requests are visible; fast ones are silent; nothing sensitive is logged.

WHY THIS EXISTS
Athena measured request latency nowhere. Uvicorn's access log records method,
path, and status but no timing, so the F-0.1 read regression — a feed page that
grew from sub-millisecond to 229ms — was indistinguishable from a healthy one in
the only per-request record the system produced. tests/test_activity_feed_scaling.py
catches that class of regression in CI; this catches it in production.

The privacy assertions are not decoration. A latency log is exactly the kind of
"just add some observability" change that quietly starts writing page titles,
search terms, and bearer tokens into the journal, so the route-template contract
is pinned by a test rather than by a comment.
"""

import asyncio
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.main import SlowRequestLogMiddleware

# Long enough that the measured duration clears the threshold on any machine,
# short enough that six tests cost a third of a second. Do NOT replace this with
# a 1ms threshold and a trivial handler: that "works" on a cold interpreter and
# then goes green-red at random once the process is warm. A flaky perf test gets
# deleted rather than fixed.
_SLEEP_SECONDS = 0.05
_THRESHOLD_MS = 10


class _Sleeper:
    """Inner middleware that makes every request take a known, floor-guaranteed time.

    Registered BEFORE the timer, so it sits INSIDE it — which also means requests
    that never reach a route (404s) are slow too, and the unmatched-route contract
    is testable at all.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await asyncio.sleep(_SLEEP_SECONDS)
        await self.app(scope, receive, send)


def _app(threshold_ms, handler=None, slow=True):
    app = FastAPI()

    @app.get("/items/{item_id}")
    def read_item(item_id: int):
        if handler is not None:
            handler()
        return {"item_id": item_id}

    if slow:
        app.add_middleware(_Sleeper)
    app.add_middleware(SlowRequestLogMiddleware, threshold_ms=threshold_ms)
    return app


def test_slow_request_is_logged_with_route_template(caplog):
    with caplog.at_level(logging.WARNING, logger="athena"):
        response = TestClient(_app(_THRESHOLD_MS)).get("/items/7")
    assert response.status_code == 200
    records = [r for r in caplog.records if "slow request" in r.getMessage()]
    assert len(records) == 1, caplog.text
    message = records[0].getMessage()
    # The ROUTE TEMPLATE, not the concrete path: low cardinality, and it cannot
    # carry a page title, a search term, or an id an attacker chose.
    assert "/items/{item_id}" in message
    assert "/items/7" not in message
    assert "GET" in message and "200" in message


def test_fast_request_logs_nothing(caplog):
    # WHY: a warning per page load is a warning nobody reads. The threshold has to
    # actually gate, or the signal is worthless.
    with caplog.at_level(logging.WARNING, logger="athena"):
        TestClient(_app(60_000, slow=False)).get("/items/7")
    assert not [r for r in caplog.records if "slow request" in r.getMessage()]


def test_threshold_zero_disables_the_timer(caplog):
    with caplog.at_level(logging.WARNING, logger="athena"):
        TestClient(_app(0)).get("/items/7")
    assert not [r for r in caplog.records if "slow request" in r.getMessage()]


def test_query_string_never_reaches_the_log(caplog):
    # WHY: query strings carry search terms — user content, and on some routes the
    # closest thing Athena has to a private note. A latency log must never be the
    # thing that copies them into the journal.
    with caplog.at_level(logging.WARNING, logger="athena"):
        TestClient(_app(_THRESHOLD_MS)).get(
            "/items/7?q=my-secret-search&token=ath_notreal"
        )
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "slow request" in message
    assert "my-secret-search" not in message
    assert "ath_notreal" not in message
    assert "q=" not in message


def test_a_slow_failure_is_still_timed(caplog):
    # WHY: the interesting slow request is often the one that blew up. Timing in a
    # `finally` is what makes that true; without it the exception path is silent
    # exactly when an operator most needs the number.
    def boom():
        raise RuntimeError("handler exploded")

    client = TestClient(
        _app(_THRESHOLD_MS, handler=boom), raise_server_exceptions=False
    )
    with caplog.at_level(logging.WARNING, logger="athena"):
        client.get("/items/7")
    records = [r for r in caplog.records if "slow request" in r.getMessage()]
    assert len(records) == 1, caplog.text
    # No response was started before the exception propagated, so there is no
    # status to report — and saying so beats printing a misleading 0.
    assert "no response" in records[0].getMessage()


def test_unmatched_route_reports_no_attacker_supplied_path(caplog):
    # WHY: a 404 path is chosen by the caller. It must not become a log line an
    # attacker controls the contents (or length) of. Uvicorn's access log already
    # records the raw path for these.
    with caplog.at_level(logging.WARNING, logger="athena"):
        TestClient(_app(_THRESHOLD_MS)).get("/etc-passwd-lookalike-probe")
    for record in caplog.records:
        message = record.getMessage()
        if "slow request" in message:
            assert "passwd-lookalike-probe" not in message
            assert "<unmatched>" in message
