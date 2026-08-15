"""Shared FastAPI dependencies for the API layer.

`get_conn` is the one place a request gets a database connection. Every module's
HTTP layer (aegis, users, later mentor) depends on this instead of opening its
own — so connection handling lives in exactly one spot. Kept out of db.py so
that module stays framework-free (pure SQLite, importable without FastAPI).

**One connection per request.** A request used to open several: the session
middleware opened one to resolve the cookie, the route dependency opened another,
and an idempotent write opened three more (identity, reserve, publish). That is not
free. `sqlite3.connect` itself is lazy and costs ~0.03 ms, but the first statement
that touches the database file pays ~2.2 ms to attach — measured on this codebase,
per connection, and holding another connection open does not help. So a browser page
spent ~4.4 ms and an idempotent write ~11 ms just getting to the data. For scale:
the whole fleet-attention rollup measures ~0.5 ms.

`RequestConnection` below is the fix. It is created empty by the request-connection
middleware, opens **lazily** on first use — a `/static` fetch still opens nothing —
and is closed once, at the end of the request, by the middleware that made it. Every
layer that used to open its own now borrows this one, so the attach is paid once.

The invariant that makes sharing safe is that a borrower returns the connection with
no transaction open. `db.transaction` decides between a real transaction and a
savepoint by reading `conn.in_transaction`, so a layer that left one open would
silently turn the next writer's commit into a savepoint that never commits — data
loss with no error. `RequestConnection.get` therefore checks that on every handoff
rather than trusting it (see `_ensure_clean`).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from fastapi import Request

from athena.core import db

logger = logging.getLogger("athena")


class RequestConnection:
    """One lazily-opened SQLite connection, shared by everything in one request.

    Not thread-safe, and it does not need to be: a request touches this serially,
    even though FastAPI may run its parts on different worker threads (which is why
    `db.connect` passes `check_same_thread=False`).
    """

    __slots__ = ("_conn", "_source", "opens")

    def __init__(self, source: str | Path | Callable[[], str | Path]) -> None:
        #: A path, or a callable returning one. The callable form defers even
        #: *finding* the database until something asks for it, which matters
        #: because `app.state.db_path` is set during lifespan startup: a raw ASGI
        #: request that never touches the database (`/healthz`) must not fail
        #: merely because this holder was constructed.
        self._source = source
        self._conn: sqlite3.Connection | None = None
        #: How many times this request actually opened a connection. Exists so a
        #: test can assert "one per request" against a number rather than a guess.
        self.opens = 0

    def get(self) -> sqlite3.Connection:
        if self._conn is None:
            source = self._source
            self._conn = db.connect(source() if callable(source) else source)
            self.opens += 1
        else:
            self._ensure_clean(self._conn)
        return self._conn

    @staticmethod
    def _ensure_clean(conn: sqlite3.Connection) -> None:
        """Refuse to hand on a connection with a transaction still open.

        A previous borrower leaving one open is a bug in that borrower, and one that
        is invisible at the point it does damage: the next `db.transaction` would
        read `in_transaction` as True, take a savepoint instead of a real
        transaction, and release it without ever committing. Rolling back here keeps
        the damage to the layer that caused it, and the log line names it.
        """
        if conn.in_transaction:
            logger.error(
                "a request layer left its transaction open; rolling back before "
                "handing the connection on (this is a bug in that layer)"
            )
            conn.rollback()

    def close(self) -> None:
        if self._conn is not None:
            # An open transaction here means nobody committed it; closing would
            # discard it anyway, so roll back explicitly rather than by accident.
            self._ensure_clean(self._conn)
            self._conn.close()
            self._conn = None


def request_connection(request: Request) -> RequestConnection:
    """This request's connection holder, or a standalone one if the middleware is
    absent — a raw ASGI mount or a test that builds a bare app still works, it just
    goes back to opening per borrower."""
    holder = getattr(request.state, "db_holder", None)
    if holder is None:
        holder = RequestConnection(request.app.state.db_path)
        request.state.db_holder = holder
    return holder


def get_conn(request: Request):
    """Per-request DB connection, opened from the path the app booted with.

    A FastAPI dependency: it runs before the handler and cleans up after. Cleanup no
    longer means closing — the connection outlives this dependency, because layers
    outside the route (the idempotency publish, an exception handler recording a
    refusal) run after it and need the same one. The middleware that created the
    holder closes it.
    """
    conn = request_connection(request).get()
    try:
        yield conn
    finally:
        # A handler that returns mid-transaction would otherwise poison every later
        # borrower; the holder's own check catches it, this one names the route.
        RequestConnection._ensure_clean(conn)
