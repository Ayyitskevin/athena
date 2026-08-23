"""One request, one SQLite connection.

A request used to open several — the session middleware for the cookie, the route
dependency for the handler, and an idempotent write three more (identity, reserve,
publish). That is not free, and the cost is not where you would guess:
`sqlite3.connect` is lazy and costs ~0.03 ms, but the FIRST statement that touches
the database file pays ~2.2 ms to attach. Per connection — holding another open
does not help — while reusing an already-open one costs 0.004 ms. So a browser page
spent ~4.4 ms and an idempotent write ~11 ms just reaching the data, against a whole
fleet-attention rollup that measures ~0.5 ms.

These tests pin the count, because a count is the only thing that stays true: the
next middleware to need the database will reach for `db.connect` unless something
fails when it does.

They also pin the invariant that makes sharing safe at all. `db.transaction` decides
between a real transaction and a savepoint by reading `conn.in_transaction`, so a
layer that returned the connection mid-transaction would silently turn the next
writer's commit into a savepoint that is released without ever committing — data
loss, no error, no log. `RequestConnection` checks that on every handoff.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

import athena.core.db as db_module
from athena.core.deps import RequestConnection
from athena.main import create_app

BROWSER = {"Accept": "text/html"}
ACTOR = {"X-Athena-Actor": "1"}


def _opened_during(call):
    """How many real connections one request opens."""
    opened: list[int] = []
    real = db_module.connect

    def counting(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    with patch.object(db_module, "connect", counting):
        response = call()
    return len(opened), response


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "one.db")
    with TestClient(app) as signed_in:
        signed_in.post(
            "/users", json={"email": "a@e.com", "name": "A", "password": "pw"}
        )
        signed_in.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        yield signed_in


def test_a_browser_page_opens_exactly_one(client):
    """The session middleware and the route used to open one each — every page and
    every REST call in the app paid that, which is what makes this the broadest of
    the wave's fixes."""
    count, response = _opened_during(
        lambda: client.get("/aegis/dashboard", headers=BROWSER)
    )
    assert response.status_code == 200
    assert count == 1


@pytest.mark.parametrize(
    "label,call_name",
    [("read", "get"), ("write", "post")],
)
def test_a_rest_call_opens_exactly_one(client, label, call_name):
    call = (
        (lambda: client.get("/issues", headers=ACTOR))
        if call_name == "get"
        else (lambda: client.post("/issues", json={"title": label}, headers=ACTOR))
    )
    count, response = _opened_during(call)
    assert response.status_code in (200, 201)
    assert count == 1


def test_an_idempotent_write_opens_exactly_one(client):
    """The dearest case before this change: identity, reserve and publish each
    opened their own, on top of the session middleware's and the route's. The
    publish is also why the route dependency cannot own the connection's lifetime —
    it runs after the route has already returned."""
    count, response = _opened_during(
        lambda: client.post(
            "/issues",
            json={"title": "idempotent"},
            headers={**ACTOR, "Idempotency-Key": "key-one-connection"},
        )
    )
    assert response.status_code == 201
    assert count == 1


def test_a_static_fetch_still_opens_none(client):
    """F-0.4's skip, which this must not undo. The holder opens lazily precisely so
    that sitting above the session middleware costs a request that never reaches the
    database nothing at all."""
    count, response = _opened_during(lambda: client.get("/static/styles.css"))
    assert response.status_code == 200
    assert count == 0


def test_a_replayed_idempotent_request_also_opens_one(client):
    """The replay path reads the stored receipt and never reaches the route, so it
    exercises a different set of layers than the first call did."""
    headers = {**ACTOR, "Idempotency-Key": "key-replayed"}
    first = client.post("/issues", json={"title": "once"}, headers=headers)
    assert first.status_code == 201
    count, replay = _opened_during(
        lambda: client.post("/issues", json={"title": "once"}, headers=headers)
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"], "not actually a replay"
    assert count == 1


def test_a_borrower_may_not_return_the_connection_mid_transaction(tmp_path, caplog):
    """The invariant that makes one shared connection safe.

    `db.transaction` reads `conn.in_transaction` to choose between a real
    transaction and a savepoint. A layer that left one open would make the next
    writer take a savepoint, release it, and never commit — the write vanishes with
    no error raised anywhere. So the handoff verifies rather than trusts."""
    holder = RequestConnection(tmp_path / "dirty.db")
    conn = holder.get()
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES (1)")
    assert conn.in_transaction

    with caplog.at_level("ERROR"):
        again = holder.get()
    assert again is conn, "the same connection is handed on, not a new one"
    assert not again.in_transaction, "the stale transaction was not cleared"
    assert "left its transaction open" in caplog.text
    # Rolled back, not committed: the uncommitted row is gone.
    assert again.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 0
    holder.close()


def test_the_holder_opens_lazily_and_closes_once(tmp_path):
    holder = RequestConnection(tmp_path / "lazy.db")
    assert holder.opens == 0, "constructing a holder must not touch the database"
    holder.get()
    holder.get()
    holder.get()
    assert holder.opens == 1
    holder.close()
    # Closing twice is harmless — the middleware's finally may race a handler that
    # already finished.
    holder.close()
    # ...and it can be reopened, which is what makes the fallback path safe.
    holder.get()
    assert holder.opens == 2
    holder.close()
